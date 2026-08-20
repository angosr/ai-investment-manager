from __future__ import annotations

import subprocess
from enum import StrEnum
from pathlib import Path

import yaml

from investment_manager.analyst import codex_runtime_integrity_matches
from investment_manager.config import AiMode, AppConfig, DeploymentStage
from investment_manager.domain import FrozenModel
from investment_manager.governance import (
    load_constitution,
    load_regression_suite,
    load_release_manifest,
    validate_manifest_against_config,
    validate_manifest_code_version,
)
from investment_manager.schema import compose_metadata


class CheckStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


class AuditProfile(StrEnum):
    PUBLIC_READONLY = "PUBLIC_READONLY"
    PRIVATE_CODEX_CHALLENGER = "PRIVATE_CODEX_CHALLENGER"


class AuditCheck(FrozenModel):
    check_id: str
    status: CheckStatus
    detail: str


class PhaseAAuditReport(FrozenModel):
    phase: str = "A"
    checks: tuple[AuditCheck, ...]

    @property
    def ready(self) -> bool:
        return all(item.status == CheckStatus.PASS for item in self.checks)

    @property
    def shadow_ready(self) -> bool:
        """公开只读 Shadow 不依赖尚未授权的真实 Codex 部署条件。"""

        codex_only = {
            "MALICIOUS_READ_ISOLATION_GATE",
            "ENABLED_ACCOUNT_DIRECTORIES_READY",
        }
        return all(
            item.status == CheckStatus.PASS
            for item in self.checks
            if item.check_id not in codex_only
        )


class PhaseAAuditor:
    """只核对可机械验证的发布前置条件，不用声明代替安全证据。"""

    def __init__(
        self,
        config: AppConfig,
        project_root: Path,
        *,
        profile: AuditProfile = AuditProfile.PUBLIC_READONLY,
        runtime_manifest: Path | None = None,
    ) -> None:
        self._config = config
        self._root = project_root
        self._profile = profile
        self._runtime_manifest = runtime_manifest

    def run(self) -> PhaseAAuditReport:
        checks = [
            self._runtime_boundary(),
            self._account_registry_is_explicit_whitelist(),
            self._locked_cli_matches(),
            self._governance_assets_exist(),
            self._regression_targets_exist(),
            self._database_migration_exists(),
            self._temporal_is_single_workflow_owner(),
            self._deployment_is_fail_closed(),
            self._isolation_verified(),
            self._account_deployment_ready(),
        ]
        return PhaseAAuditReport(checks=tuple(checks))

    def _runtime_boundary(self) -> AuditCheck:
        deployment = self._config.deployment
        if self._profile == AuditProfile.PRIVATE_CODEX_CHALLENGER:
            safe = (
                deployment.stage == DeploymentStage.SHADOW
                and self._config.pipeline.ai_mode == AiMode.PROPOSE
                and self._config.codex_runtime.enabled
                and not deployment.testnet_order_submission_enabled
                and not deployment.live_order_submission_enabled
            )
            return AuditCheck(
                check_id="REAL_CODEX_PROPOSE_AND_TRADING_DISABLED",
                status=CheckStatus.PASS if safe else CheckStatus.FAIL,
                detail="私有 Challenger 必须启用隔离 Codex PROPOSE，且交易仍保持关闭。",
            )
        return AuditCheck(
            check_id="REAL_CODEX_AND_TRADING_DISABLED",
            status=(
                CheckStatus.PASS
                if not self._config.codex_runtime.enabled
                else CheckStatus.FAIL
            ),
            detail="当前仓库默认不调用真实 Codex，且没有真实交易适配器。",
        )

    def _account_registry_is_explicit_whitelist(self) -> AuditCheck:
        accounts = self._config.codex_accounts.accounts
        unique = len({item.account_id for item in accounts}) == len(accounts) and len(
            {item.codex_home for item in accounts}
        ) == len(accounts)
        return AuditCheck(
            check_id="EXPLICIT_ACCOUNT_WHITELIST",
            status=CheckStatus.PASS if accounts and unique else CheckStatus.FAIL,
            detail="账号逐项来自类型化白名单且身份等于目录名，不执行主目录扫描。",
        )

    def _locked_cli_matches(self) -> AuditCheck:
        runtime = self._config.codex_runtime
        try:
            if runtime.enabled:
                matches = codex_runtime_integrity_matches(runtime)
            else:
                completed = subprocess.run(
                    [str(runtime.binary), "--version"],
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                matches = (
                    completed.returncode == 0
                    and completed.stdout.strip() == runtime.expected_cli_version
                )
        except (OSError, subprocess.TimeoutExpired):
            matches = False
        return AuditCheck(
            check_id="LOCKED_CODEX_CLI_VERSION",
            status=CheckStatus.PASS if matches else CheckStatus.FAIL,
            detail=(
                f"期望 {runtime.expected_cli_version}"
                + (
                    f" / sha256:{runtime.expected_binary_sha256[:12]}"
                    if runtime.expected_binary_sha256 is not None
                    else ""
                )
            ),
        )

    def _governance_assets_exist(self) -> AuditCheck:
        try:
            load_constitution(self._root / "config" / "system-constitution.yaml")
            load_regression_suite(self._root / "config" / "regression-suite.yaml")
            manifest_path = self._runtime_manifest or (
                self._root / "config" / "release-manifest.yaml"
            )
            manifest = load_release_manifest(manifest_path)
            strict_runtime = (
                self._profile == AuditProfile.PRIVATE_CODEX_CHALLENGER
            )
            if strict_runtime and self._runtime_manifest is None:
                raise ValueError("私有 Challenger 必须显式指定运行 Manifest")
            validate_manifest_against_config(
                manifest,
                self._config,
                require_configuration_hash=strict_runtime,
            )
            if strict_runtime:
                validate_manifest_code_version(
                    manifest,
                    repository_root=self._root,
                )
        except (OSError, ValueError, yaml.YAMLError):
            return AuditCheck(
                check_id="TYPED_GOVERNANCE_ASSETS",
                status=CheckStatus.FAIL,
                detail=(
                    "系统宪法、固定回归集或运行 ReleaseManifest "
                    "缺失、非法或与代码/配置不一致。"
                ),
            )
        return AuditCheck(
            check_id="TYPED_GOVERNANCE_ASSETS",
            status=CheckStatus.PASS,
            detail=(
                "系统宪法、固定回归集和运行 ReleaseManifest "
                "均通过严格 Schema 与发布一致性校验。"
            ),
        )

    def _regression_targets_exist(self) -> AuditCheck:
        try:
            suite = load_regression_suite(
                self._root / "config" / "regression-suite.yaml"
            )
        except (OSError, ValueError, yaml.YAMLError):
            return AuditCheck(
                check_id="FIXED_REGRESSION_TARGETS",
                status=CheckStatus.FAIL,
                detail="固定回归集缺失或非法。",
            )
        missing = [
            target
            for item in suite.cases
            for target in (item.fixture or item.test,)
            if target is None or not (self._root / target).is_file()
        ]
        return AuditCheck(
            check_id="FIXED_REGRESSION_TARGETS",
            status=CheckStatus.FAIL if missing else CheckStatus.PASS,
            detail="missing=" + ",".join(str(item) for item in missing)
            if missing
            else suite.version,
        )

    def _isolation_verified(self) -> AuditCheck:
        return AuditCheck(
            check_id="MALICIOUS_READ_ISOLATION_GATE",
            status=(
                CheckStatus.PASS
                if self._config.codex_runtime.isolation_verified
                else CheckStatus.BLOCKED
            ),
            detail="必须用 OS/Profile 实测拒绝读取账号目录、环境和 /proc；提示词声明不算。",
        )

    def _database_migration_exists(self) -> AuditCheck:
        revisions = tuple((self._root / "migrations" / "versions").glob("*.py"))
        return AuditCheck(
            check_id="VERSIONED_DATABASE_MIGRATION",
            status=CheckStatus.PASS if revisions else CheckStatus.FAIL,
            detail="Alembic revision=" + ",".join(item.stem for item in revisions),
        )

    def _deployment_is_fail_closed(self) -> AuditCheck:
        deployment = self._config.deployment
        safe = (
            deployment.stage.value in {"MOCK", "SHADOW"}
            and not deployment.testnet_order_submission_enabled
            and not deployment.live_order_submission_enabled
            and deployment.credential_profile is None
            and (deployment.shadow_market_data_enabled == (deployment.stage.value == "SHADOW"))
        )
        return AuditCheck(
            check_id="DEPLOYMENT_FAIL_CLOSED",
            status=CheckStatus.PASS if safe else CheckStatus.FAIL,
            detail="Mock/公开只读 Shadow 不含凭据或订单权限；LIVE 由 Schema 无条件拒绝。",
        )

    def _temporal_is_single_workflow_owner(self) -> AuditCheck:
        tables = compose_metadata().tables
        safe = (
            "analysis_workflow_runs" not in tables
            and "execution_requests" in tables
            and "reconciliation_reports" in tables
            and "outcome_window_reports" in tables
            and "governance_decisions" in tables
            and "evaluation_results" in tables
            and "release_approval_requests" in tables
        )
        return AuditCheck(
            check_id="TEMPORAL_SINGLE_WORKFLOW_OWNER",
            status=CheckStatus.PASS if safe else CheckStatus.FAIL,
            detail=(
                "业务库不复制 Analysis Workflow 状态机，只保存执行交接、对账、"
                "结果评价与治理发布事实；未接线域不以类名存在冒充运行就绪。"
            ),
        )

    def _account_deployment_ready(self) -> AuditCheck:
        accounts = self._config.codex_accounts.accounts
        enabled = tuple(item for item in accounts if item.enabled)
        ready = bool(enabled) and all(item.codex_home.is_dir() for item in enabled)
        return AuditCheck(
            check_id="ENABLED_ACCOUNT_DIRECTORIES_READY",
            status=CheckStatus.PASS if ready else CheckStatus.BLOCKED,
            detail=(
                "显式白名单中至少一个账号须经人工启用且目录存在；未登记目录不会被自动发现或调用。"
            ),
        )
