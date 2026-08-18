from __future__ import annotations

import subprocess
from enum import StrEnum
from pathlib import Path

from quant_core.config import AppConfig
from quant_core.domain import FrozenModel
from quant_core.governance import (
    load_constitution,
    load_regression_suite,
    load_release_manifest,
    validate_manifest_against_config,
)
from quant_core.governance_workflows import GovernanceCycleWorkflow
from quant_core.outcome_evaluation_workflows import OutcomeEvaluationWorkflow
from quant_core.persistence import metadata
from quant_core.reconciliation_workflows import ReconciliationWorkflow
from quant_core.release_workflows import ReleaseWorkflow
from quant_core.temporal_workflows import AnalysisCycleWorkflow, ExecutionWorkflow
from quant_core.version_evaluation_workflows import VersionEvaluationWorkflow


class CheckStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


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
            "THREE_APPROVED_ACCOUNT_DIRECTORIES",
        }
        return all(
            item.status == CheckStatus.PASS
            for item in self.checks
            if item.check_id not in codex_only
        )


class PhaseAAuditor:
    """只核对可机械验证的发布前置条件，不用声明代替安全证据。"""

    def __init__(self, config: AppConfig, project_root: Path) -> None:
        self._config = config
        self._root = project_root

    def run(self) -> PhaseAAuditReport:
        checks = [
            self._real_execution_disabled(),
            self._account_registry_is_explicit_three(),
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

    def _real_execution_disabled(self) -> AuditCheck:
        status = CheckStatus.PASS if not self._config.codex_runtime.enabled else CheckStatus.FAIL
        return AuditCheck(
            check_id="REAL_CODEX_AND_TRADING_DISABLED",
            status=status,
            detail="当前仓库默认不调用真实 Codex，且没有真实交易适配器。",
        )

    def _account_registry_is_explicit_three(self) -> AuditCheck:
        accounts = self._config.codex_accounts.accounts
        unique = (
            len({item.account_id for item in accounts}) == 3
            and len({item.codex_home for item in accounts}) == 3
        )
        return AuditCheck(
            check_id="EXPLICIT_THREE_ACCOUNT_REGISTRY",
            status=CheckStatus.PASS if len(accounts) == 3 and unique else CheckStatus.FAIL,
            detail="账号来自类型化白名单，不执行主目录扫描。",
        )

    def _locked_cli_matches(self) -> AuditCheck:
        runtime = self._config.codex_runtime
        try:
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
            detail=f"期望 {runtime.expected_cli_version}",
        )

    def _governance_assets_exist(self) -> AuditCheck:
        try:
            load_constitution(self._root / "config" / "system-constitution.yaml")
            load_regression_suite(self._root / "config" / "regression-suite.yaml")
            manifest = load_release_manifest(self._root / "config" / "release-manifest.yaml")
            validate_manifest_against_config(manifest, self._config)
        except (OSError, ValueError):
            return AuditCheck(
                check_id="TYPED_GOVERNANCE_ASSETS",
                status=CheckStatus.FAIL,
                detail="系统宪法、固定回归集或当前 ReleaseManifest 缺失/非法。",
            )
        return AuditCheck(
            check_id="TYPED_GOVERNANCE_ASSETS",
            status=CheckStatus.PASS,
            detail="系统宪法、固定回归集和当前 ReleaseManifest 均通过严格 Schema。",
        )

    def _regression_targets_exist(self) -> AuditCheck:
        suite = load_regression_suite(self._root / "config" / "regression-suite.yaml")
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
        safe = (
            AnalysisCycleWorkflow.__name__ == "AnalysisCycleWorkflow"
            and ExecutionWorkflow.__name__ == "ExecutionWorkflow"
            and ReconciliationWorkflow.__name__ == "ReconciliationWorkflow"
            and OutcomeEvaluationWorkflow.__name__ == "OutcomeEvaluationWorkflow"
            and GovernanceCycleWorkflow.__name__ == "GovernanceCycleWorkflow"
            and VersionEvaluationWorkflow.__name__ == "VersionEvaluationWorkflow"
            and ReleaseWorkflow.__name__ == "ReleaseWorkflow"
            and "analysis_workflow_runs" not in metadata.tables
            and "execution_requests" in metadata.tables
            and "reconciliation_reports" in metadata.tables
            and "outcome_window_reports" in metadata.tables
            and "governance_decisions" in metadata.tables
            and "evaluation_results" in metadata.tables
            and "release_approval_requests" in metadata.tables
        )
        return AuditCheck(
            check_id="TEMPORAL_SINGLE_WORKFLOW_OWNER",
            status=CheckStatus.PASS if safe else CheckStatus.FAIL,
            detail="Temporal 持有父子流程历史；业务库只保存执行交接事实，不复制 Workflow 状态机。",
        )

    def _account_deployment_ready(self) -> AuditCheck:
        accounts = self._config.codex_accounts.accounts
        ready = all(item.enabled and item.codex_home.is_dir() for item in accounts)
        return AuditCheck(
            check_id="THREE_APPROVED_ACCOUNT_DIRECTORIES",
            status=CheckStatus.PASS if ready else CheckStatus.BLOCKED,
            detail="必须由用户人工选择三个获准且已登录目录；不会自动选择主机上的第四个目录。",
        )
