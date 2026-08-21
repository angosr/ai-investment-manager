from pathlib import Path

import pytest
import yaml

from investment_manager.forecast.policy import AiMode
from investment_manager.governance.audit.acceptance import AuditProfile, CheckStatus, PhaseAAuditor
from investment_manager.governance.models import load_release_manifest
from investment_manager.governance.policy import DeploymentStage
from investment_manager.kernel.identity import content_hash


def test_phase_a_audit_reports_real_deployment_blockers_without_false_success(
    app_config,
) -> None:
    root = Path(__file__).resolve().parents[1]

    report = PhaseAAuditor(app_config, root).run()
    checks = {item.check_id: item for item in report.checks}

    assert not report.ready
    assert report.shadow_ready
    assert checks["REAL_CODEX_AND_TRADING_DISABLED"].status == CheckStatus.PASS
    assert checks["EXPLICIT_ACCOUNT_WHITELIST"].status == CheckStatus.PASS
    assert checks["LOCKED_CODEX_CLI_VERSION"].status == CheckStatus.PASS
    assert checks["TYPED_GOVERNANCE_ASSETS"].status == CheckStatus.PASS
    assert checks["FIXED_REGRESSION_TARGETS"].status == CheckStatus.PASS
    assert checks["VERSIONED_DATABASE_MIGRATION"].status == CheckStatus.PASS
    assert checks["TEMPORAL_SINGLE_WORKFLOW_OWNER"].status == CheckStatus.PASS
    assert checks["DEPLOYMENT_FAIL_CLOSED"].status == CheckStatus.PASS
    assert checks["MALICIOUS_READ_ISOLATION_GATE"].status == CheckStatus.BLOCKED
    assert checks["ENABLED_ACCOUNT_DIRECTORIES_READY"].status == CheckStatus.BLOCKED


def test_public_shadow_audit_binds_exact_runtime_release(
    app_config,
    tmp_path,
    monkeypatch,
) -> None:
    root = Path(__file__).resolve().parents[1]
    loaded = app_config.model_copy(
        update={
            "deployment": app_config.deployment.model_copy(
                update={
                    "stage": DeploymentStage.SHADOW,
                    "shadow_market_data_enabled": True,
                }
            )
        }
    )
    manifest = load_release_manifest(root / "config" / "release-manifest.yaml").model_copy(
        update={"configuration_hash": content_hash(loaded)}
    )
    manifest_path = tmp_path / "release-manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(manifest.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    validated = []
    monkeypatch.setattr(
        "investment_manager.governance.audit.acceptance.validate_manifest_code_version",
        lambda manifest, *, repository_root: validated.append(
            (manifest.manifest_id, repository_root)
        ),
    )

    report = PhaseAAuditor(
        loaded,
        root,
        runtime_manifest=manifest_path,
    ).run()

    checks = {item.check_id: item for item in report.checks}
    assert report.shadow_ready
    assert checks["TYPED_GOVERNANCE_ASSETS"].status == CheckStatus.PASS
    assert validated == [(manifest.manifest_id, root)]

    manifest_path.write_text(
        yaml.safe_dump(
            manifest.model_copy(update={"configuration_hash": "0" * 64}).model_dump(
                mode="json"
            ),
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    failed = PhaseAAuditor(
        loaded,
        root,
        runtime_manifest=manifest_path,
    ).run()
    failed_checks = {item.check_id: item for item in failed.checks}
    assert not failed.shadow_ready
    assert failed_checks["TYPED_GOVERNANCE_ASSETS"].status == CheckStatus.FAIL


@pytest.mark.parametrize(
    ("ai_mode", "assessment_enabled"),
    ((AiMode.PROPOSE, False), (AiMode.OFF, True)),
)
def test_private_challenger_audit_accepts_exact_runtime_release(
    app_config,
    tmp_path,
    monkeypatch,
    ai_mode,
    assessment_enabled,
) -> None:
    root = Path(__file__).resolve().parents[1]
    first, *remaining = app_config.codex_accounts.accounts
    loaded = app_config.model_copy(
        update={
            "deployment": app_config.deployment.model_copy(
                update={
                    "stage": DeploymentStage.SHADOW,
                    "shadow_market_data_enabled": True,
                }
            ),
            "pipeline": app_config.pipeline.model_copy(
                update={"ai_mode": ai_mode}
            ),
            "assessment": app_config.assessment.model_copy(
                update={"enabled": assessment_enabled}
            ),
            "codex_runtime": app_config.codex_runtime.model_copy(
                update={"enabled": True, "isolation_verified": True}
            ),
            "codex_accounts": app_config.codex_accounts.model_copy(
                update={
                    "accounts": (
                        first.model_copy(
                            update={"enabled": True, "codex_home": root}
                        ),
                        *remaining,
                    )
                }
            ),
        }
    )
    manifest = load_release_manifest(root / "config" / "release-manifest.yaml")
    manifest = manifest.model_copy(
        update={"configuration_hash": content_hash(loaded)}
    )
    manifest_path = tmp_path / "release-manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(manifest.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "investment_manager.governance.audit.acceptance.validate_manifest_code_version",
        lambda manifest, *, repository_root: repository_root,
    )
    monkeypatch.setattr(
        "investment_manager.governance.audit.acceptance.codex_runtime_integrity_matches",
        lambda runtime: True,
    )

    report = PhaseAAuditor(
        loaded,
        root,
        profile=AuditProfile.PRIVATE_CODEX_CHALLENGER,
        runtime_manifest=manifest_path,
    ).run()
    checks = {item.check_id: item for item in report.checks}

    assert report.ready
    assert (
        checks["REAL_CODEX_ANALYSIS_AND_TRADING_DISABLED"].status
        == CheckStatus.PASS
    )
    assert checks["TYPED_GOVERNANCE_ASSETS"].status == CheckStatus.PASS


def test_private_challenger_audit_rejects_missing_runtime_manifest(
    app_config,
) -> None:
    root = Path(__file__).resolve().parents[1]

    report = PhaseAAuditor(
        app_config,
        root,
        profile=AuditProfile.PRIVATE_CODEX_CHALLENGER,
    ).run()
    checks = {item.check_id: item for item in report.checks}

    assert not report.ready
    assert checks["TYPED_GOVERNANCE_ASSETS"].status == CheckStatus.FAIL


def test_audit_returns_fail_instead_of_crashing_on_invalid_regression_yaml(
    app_config,
    tmp_path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "regression-suite.yaml").write_text(
        "cases: [invalid",
        encoding="utf-8",
    )

    check = PhaseAAuditor(app_config, tmp_path)._regression_targets_exist()

    assert check.status == CheckStatus.FAIL
    assert check.detail == "固定回归集缺失或非法。"
