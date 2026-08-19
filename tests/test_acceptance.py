from pathlib import Path

from quant_core.acceptance import CheckStatus, PhaseAAuditor


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
