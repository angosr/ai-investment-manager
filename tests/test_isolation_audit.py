from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from investment_manager.analyst import IsolationAuditCheck
from investment_manager.governance import load_release_manifest
from investment_manager.isolation_audit import (
    CodexIsolationAuditCatalog,
    build_codex_isolation_audit_artifact,
)
from investment_manager.kernel.identity import content_hash


def test_isolation_audit_artifact_binds_release_and_rejects_tampering(
    app_config,
    tmp_path,
    monkeypatch,
) -> None:
    first, second, *remaining = app_config.codex_accounts.accounts
    loaded = app_config.model_copy(
        update={
            "codex_runtime": app_config.codex_runtime.model_copy(
                update={
                    "enabled": True,
                    "isolation_verified": True,
                    "expected_binary_sha256": "a" * 64,
                }
            ),
            "codex_accounts": app_config.codex_accounts.model_copy(
                update={
                    "accounts": (
                        first.model_copy(update={"enabled": True}),
                        second.model_copy(update={"enabled": True}),
                        *remaining,
                    )
                }
            ),
        }
    )
    manifest = load_release_manifest("config/release-manifest.yaml").model_copy(
        update={"configuration_hash": content_hash(loaded)}
    )
    checks = tuple(
        IsolationAuditCheck(
            account_id=item.account_id,
            ready=True,
            effective_headroom="90",
            reason_code="OK",
        )
        for item in loaded.codex_accounts.accounts
        if item.enabled
    )
    monkeypatch.setattr(
        "investment_manager.isolation_audit.codex_runtime_integrity_matches",
        lambda runtime: True,
    )

    artifact = build_codex_isolation_audit_artifact(
        config=loaded,
        manifest=manifest,
        checks=checks,
        audited_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    assert artifact.ready
    assert artifact.manifest_id == manifest.manifest_id
    assert artifact.codex_binary_sha256 == "a" * 64
    assert artifact.enabled_account_ids == tuple(item.account_id for item in checks)

    catalog = CodexIsolationAuditCatalog(tmp_path / "audits")
    path = catalog.store(artifact)
    assert catalog.load(artifact.artifact_id) == artifact
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["artifact"]["model"] = "tampered-model"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="内容哈希不匹配"):
        catalog.load(artifact.artifact_id)
