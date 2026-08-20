from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from investment_manager.analyst import (
    IsolationAuditCheck,
    analysis_behavior_hash,
    codex_runtime_integrity_matches,
)
from investment_manager.config import AppConfig
from investment_manager.governance.models import ReleaseManifest, validate_manifest_against_config
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.platform.artifacts import write_json_artifact


class CodexIsolationAuditArtifact(FrozenModel):
    """Point-in-time proof for one exact release and its enabled account set."""

    version: Literal["codex-isolation-audit-artifact-v1"] = (
        "codex-isolation-audit-artifact-v1"
    )
    artifact_id: str
    audited_at: datetime
    manifest_id: str
    code_version: str = Field(pattern=r"^[0-9a-f]{40}$")
    configuration_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    analysis_behavior_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_policy_version: str
    codex_cli_version: str
    codex_binary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model: str
    reasoning_effort: str
    enabled_account_ids: tuple[str, ...] = Field(min_length=1)
    runtime_integrity_verified: bool
    ready: bool
    checks: tuple[IsolationAuditCheck, ...] = Field(min_length=1)

    _utc_audited_at = field_validator("audited_at")(require_utc)

    @model_validator(mode="after")
    def identity_and_scope_match(self):
        if tuple(item.account_id for item in self.checks) != self.enabled_account_ids:
            raise ValueError("隔离审计账号集合与逐项结果不一致")
        if len(set(self.enabled_account_ids)) != len(self.enabled_account_ids):
            raise ValueError("隔离审计账号集合不得重复")
        expected_ready = self.runtime_integrity_verified and all(
            item.ready for item in self.checks
        )
        if self.ready != expected_ready:
            raise ValueError("隔离审计汇总状态与逐项结果不一致")
        if self.artifact_id != stable_id(
            "codex_isolation_audit_artifact",
            self.model_dump(exclude={"artifact_id"}, mode="json"),
        ):
            raise ValueError("隔离审计制品身份不一致")
        return self


class _CodexIsolationAuditEnvelope(FrozenModel):
    artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact: CodexIsolationAuditArtifact


class CodexIsolationAuditCatalog:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def store(self, artifact: CodexIsolationAuditArtifact) -> Path:
        target = self._root / f"{artifact.artifact_id}.json"
        if target.exists():
            if self.load(artifact.artifact_id) != artifact:
                raise ValueError("同一隔离审计制品 ID 的内容不一致")
            return target
        envelope = _CodexIsolationAuditEnvelope(
            artifact_hash=content_hash(artifact),
            artifact=artifact,
        )
        return write_json_artifact(
            root=self._root, target=target, prefix=".codex-isolation-audit-", payload=envelope
        )

    def load(self, artifact_id: str) -> CodexIsolationAuditArtifact:
        raw = json.loads(
            (self._root / f"{artifact_id}.json").read_text(encoding="utf-8")
        )
        if not isinstance(raw, dict) or raw.get("artifact_hash") != content_hash(
            raw.get("artifact")
        ):
            raise ValueError("隔离审计制品内容哈希不匹配")
        envelope = _CodexIsolationAuditEnvelope.model_validate(raw)
        if envelope.artifact.artifact_id != artifact_id:
            raise ValueError("隔离审计文件名与制品 ID 不一致")
        return envelope.artifact


def build_codex_isolation_audit_artifact(
    *,
    config: AppConfig,
    manifest: ReleaseManifest,
    checks: tuple[IsolationAuditCheck, ...],
    audited_at: datetime,
) -> CodexIsolationAuditArtifact:
    validate_manifest_against_config(
        manifest,
        config,
        require_configuration_hash=True,
    )
    if manifest.configuration_hash is None:
        raise ValueError("隔离审计必须绑定完整配置哈希")
    expected_digest = config.codex_runtime.expected_binary_sha256
    if expected_digest is None:
        raise ValueError("隔离审计必须绑定 Codex binary SHA-256")
    account_ids = tuple(
        item.account_id for item in config.codex_accounts.accounts if item.enabled
    )
    runtime_integrity_verified = codex_runtime_integrity_matches(
        config.codex_runtime
    )
    values = {
        "audited_at": require_utc(audited_at),
        "manifest_id": manifest.manifest_id,
        "code_version": manifest.code_version,
        "configuration_hash": manifest.configuration_hash,
        "analysis_behavior_hash": analysis_behavior_hash(config),
        "runtime_policy_version": config.codex_runtime.version,
        "codex_cli_version": config.codex_runtime.expected_cli_version,
        "codex_binary_sha256": expected_digest,
        "model": config.codex_runtime.model,
        "reasoning_effort": config.codex_runtime.reasoning_effort,
        "enabled_account_ids": account_ids,
        "runtime_integrity_verified": runtime_integrity_verified,
        "ready": runtime_integrity_verified and all(item.ready for item in checks),
        "checks": checks,
    }
    artifact_id = stable_id(
        "codex_isolation_audit_artifact",
        CodexIsolationAuditArtifact.model_construct(
            artifact_id="pending",
            **values,
        ).model_dump(exclude={"artifact_id"}, mode="json"),
    )
    return CodexIsolationAuditArtifact(artifact_id=artifact_id, **values)
