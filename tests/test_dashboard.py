"""观测台投影层单元测试：措辞、摘要拼装、权益累加、健康合成与资源采样。

只测「事实 → DTO」的纯逻辑；DB 取数是既有已测 Repository 的薄封装，不在此重复覆盖。
用轻量替身（SimpleNamespace）隔离领域构造细节，聚焦本层行为。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from investment_manager.entrypoints.dashboard import formatting as fmt
from investment_manager.entrypoints.dashboard import serializers as ser
from investment_manager.entrypoints.dashboard.capital import (
    CapitalOverview,
    serialize_forecast_evidence,
    serialize_world_model_ablation_evidence,
)
from investment_manager.entrypoints.dashboard.health import assemble_health
from investment_manager.entrypoints.dashboard.read_models import (
    AccountStatus,
    AnalysisRuntimeStatus,
    AnalysisScopeRuntimeStatus,
    AssessmentQualityStatus,
    EquityWindow,
    WorldEvent,
)
from investment_manager.entrypoints.dashboard.resources import sample_host_resources
from investment_manager.execution.models import Side
from investment_manager.forecast.context.evaluation import evaluate_forecast_evidence


def _intent(side: Side) -> SimpleNamespace:
    return SimpleNamespace(
        side=side,
        entry=SimpleNamespace(price=Decimal("63140")),
        stop_price=Decimal("61980"),
    )


def test_forecast_evidence_has_an_explicit_audit_projection() -> None:
    evidence = evaluate_forecast_evidence(
        (),
        due_slot_count=1,
        forecast_count=1,
        no_estimate_count=0,
        required_non_overlapping_samples=30,
    )

    assert serialize_forecast_evidence(evidence) == {
        "forecast_evidence": {
            "evaluation_version": "context-forecast-evidence-v4",
            "status": "NO_SETTLED_SAMPLES",
            "terminal_result_count": 1,
            "due_slot_count": 1,
            "forecast_count": 1,
            "no_estimate_count": 0,
            "settled_forecast_count": 0,
            "non_overlapping_sample_count": 0,
            "required_non_overlapping_samples": 30,
            "permission_evidence_eligible": True,
            "mean_brier_score": None,
            "benchmark_mean_brier_score": None,
            "brier_skill": None,
            "rolling_benchmark_mean_brier_score": None,
            "rolling_brier_skill": None,
            "rolling_brier_skill_lower_bound": None,
            "rolling_brier_skill_upper_bound": None,
            "rolling_baseline_ready_count": 0,
            "market_benchmark_mean_brier_score": None,
            "market_brier_skill": None,
            "market_brier_skill_lower_bound": None,
            "market_brier_skill_upper_bound": None,
            "market_baseline_ready_count": 0,
            "mean_expected_gross_bps": None,
            "mean_realized_gross_bps": None,
            "result_coverage": "1",
        }
    }


def test_world_model_ablation_has_a_compact_audit_projection() -> None:
    at = datetime(2026, 8, 25, 12, tzinfo=UTC)
    report = SimpleNamespace(
        plan_id="world-model-ablation-forward-v7",
        as_of=at,
        formal_forecast_count=3,
        formal_no_estimate_count=1,
        assignments=3,
        pending_controls=1,
        successful_controls=2,
        failed_controls=0,
        settled_pairs=1,
        conservative_sample_count=2,
        mean_brier_improvement=Decimal("0.125"),
        conservative_improvement_lower_bound=None,
        minimum_sample_size=30,
        evidence_sufficient=False,
    )

    assert serialize_world_model_ablation_evidence(report) == {
        "world_model_ablation": {
            "plan_id": "world-model-ablation-forward-v7",
            "as_of": at.isoformat(),
            "formal_forecast_count": 3,
            "formal_no_estimate_count": 1,
            "assignments": 3,
            "pending_controls": 1,
            "successful_controls": 2,
            "failed_controls": 0,
            "settled_pairs": 1,
            "conservative_sample_count": 2,
            "mean_brier_improvement": "0.125",
            "conservative_improvement_lower_bound": None,
            "minimum_sample_size": 30,
            "evidence_sufficient": False,
        }
    }


def _analysis_status(now: datetime, **updates) -> AnalysisRuntimeStatus:
    latest = updates.pop("latest_success_at", now)
    values = {
        "recent_attempts": 1,
        "recent_successes": 1,
        "pending_outbox_count": 0,
        "oldest_pending_outbox_at": None,
        "release_aligned": True,
        "overdue_forecast_count": 0,
        "oldest_overdue_analysis_at": None,
        "scopes": (
            AnalysisScopeRuntimeStatus(
                symbol="BTCUSDT",
                latest_success_at=latest,
                heartbeat_seconds=900,
            ),
        ),
    }
    values.update(updates)
    return AnalysisRuntimeStatus(**values)


def test_assessment_evidence_catalog_resolves_derivative_state() -> None:
    now = datetime(2026, 8, 21, 23, tzinfo=UTC)
    evidence_ref = "d" * 64
    state = SimpleNamespace(
        evidence_ref=evidence_ref,
        asset="BTC",
        observed_at=now,
        mark_index_premium_bps=Decimal("1.2"),
        executable_short_basis_bps=Decimal("2.3"),
        last_funding_rate_bps=Decimal("0.4"),
        spot_taker_buy_sell_ratio=Decimal("1.1"),
        open_interest_change_fraction=Decimal("0.02"),
        global_long_account_fraction=Decimal("0.53"),
        taker_buy_sell_ratio=Decimal("0.9"),
    )
    packet = SimpleNamespace(
        facts=(),
        intelligence_events=(),
        derivative_states=(state,),
        deltas=(),
        previous_context=None,
    )

    catalog = ser._assessment_evidence_catalog(packet)

    assert catalog[evidence_ref]["kind"] == "MARKET_STRUCTURE"
    assert catalog[evidence_ref]["title"] == "BTC 现货与衍生品结构"
    assert "OI 变化 0.02" in catalog[evidence_ref]["detail"]


def test_world_cognition_summary_is_the_highest_value_mechanism() -> None:
    assessment = SimpleNamespace(
        synthesis="完整联合认知不应该在列表中被截成前缀。",
        mechanisms=(
            SimpleNamespace(claim="流动性改善，但信用走阔仍限制风险偏好。"),
            SimpleNamespace(claim="政策路径是下一阶段最大的反转风险。"),
        ),
    )

    summary = ser._assessment_summary(assessment)

    assert summary == assessment.mechanisms[0].claim
    assert summary not in assessment.synthesis


def test_health_uses_authoritative_per_scope_heartbeat() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    report = SimpleNamespace(status="MATCHED", freeze_new_risk=False, as_of=now)
    healthy_reader = SimpleNamespace(
        latest_reconciliation=lambda *, now: report,
        latest_market_observed_at=lambda: now,
        portfolio_protection_active=lambda: False,
        analysis_runtime_status=lambda *, now: _analysis_status(
            now,
            scopes=(
                AnalysisScopeRuntimeStatus(
                    symbol="BTCUSDT",
                    latest_success_at=now - timedelta(minutes=40),
                    heartbeat_seconds=3600,
                ),
            ),
        ),
    )
    reader = SimpleNamespace(
        latest_reconciliation=lambda *, now: report,
        latest_market_observed_at=lambda: now,
        portfolio_protection_active=lambda: False,
        analysis_runtime_status=lambda *, now: _analysis_status(
            now,
            latest_success_at=now - timedelta(minutes=40),
            scopes=(
                AnalysisScopeRuntimeStatus(
                    symbol="BTCUSDT",
                    latest_success_at=now - timedelta(minutes=40),
                    heartbeat_seconds=3600,
                ),
                AnalysisScopeRuntimeStatus(
                    symbol="ETHUSDT",
                    latest_success_at=None,
                    heartbeat_seconds=3600,
                ),
            ),
        ),
    )
    config = SimpleNamespace(
        reconciliation=SimpleNamespace(maximum_report_age_seconds=180),
        risk=SimpleNamespace(
            maximum_market_age_seconds=60,
            maximum_account_age_seconds=60,
            kill_switch=False,
        ),
        **_health_policy_extras(),
    )

    healthy = assemble_health(healthy_reader, config, now=now)
    result = assemble_health(reader, config, now=now)
    healthy_check = next(
        item for item in healthy["checks"] if item["key"] == "ai_analysis"
    )
    check = next(item for item in result["checks"] if item["key"] == "ai_analysis")

    assert healthy_check["state"] == "ok"
    assert "2400/3900 秒" in healthy_check["detail"]
    assert check["state"] == "unknown"
    assert "ETHUSDT 等待当前版本首次分析" in check["detail"]


def _health_policy_extras() -> dict:
    return {
        "trigger": SimpleNamespace(
            heartbeat_minutes=15,
            outbox_fallback_poll_seconds=1,
        ),
        "shadow": SimpleNamespace(analysis_deadline_seconds=300),
    }


def test_health_exposes_structural_output_failure_without_reclassifying_history() -> None:
    now = datetime(2026, 8, 21, 14, tzinfo=UTC)
    report = SimpleNamespace(status="MATCHED", freeze_new_risk=False, as_of=now)
    reader = SimpleNamespace(
        latest_reconciliation=lambda *, now: report,
        latest_market_observed_at=lambda: now,
        portfolio_protection_active=lambda: False,
        analysis_runtime_status=lambda *, now: _analysis_status(now),
    )
    config = SimpleNamespace(
        reconciliation=SimpleNamespace(maximum_report_age_seconds=180),
        risk=SimpleNamespace(
            maximum_market_age_seconds=60,
            maximum_account_age_seconds=60,
            kill_switch=False,
        ),
        **_health_policy_extras(),
    )
    rejected = AssessmentQualityStatus(
        latest_attempt_at=now,
        latest_attempt_status="REJECTED",
        latest_attempt_reason="SCHEMA_INVALID",
        latest_valid_at=now - timedelta(minutes=5),
        rejected_attempt_count_24h=1,
        rejection_reason_codes=("CODEX_SCHEMA_INVALID",),
    )

    result = assemble_health(
        reader,
        config,
        now=now,
        assessment_quality=rejected,
    )
    check = next(
        item for item in result["checks"] if item["key"] == "ai_output_quality"
    )

    assert check["state"] == "warn"
    assert check["name"] == "世界认知 AI"
    assert check["detail"] == (
        "最近一次模型调用未产生可持久化结果：AI 输出格式不符合契约"
    )

    clean = replace(
        rejected,
        latest_attempt_status="SUCCEEDED",
        latest_attempt_reason=None,
        rejected_attempt_count_24h=0,
    )
    clean_result = assemble_health(
        reader,
        config,
        now=now,
        assessment_quality=clean,
    )
    clean_check = next(
        item for item in clean_result["checks"] if item["key"] == "ai_output_quality"
    )
    assert clean_check["state"] == "ok"


def test_direction_label_maps_side():
    assert fmt.direction_label(Side.BUY) == "多"
    assert fmt.direction_label(Side.SELL) == "空"
    assert fmt.direction_label(None) is None


def test_reason_plain_falls_back_to_raw_code():
    assert fmt.reason_plain("INSUFFICIENT_NET_EDGE") == "扣掉成本后优势不足"
    assert fmt.reason_plain("SOME_NEW_CODE") == "SOME_NEW_CODE"
    assert (
        fmt.assessment_reason_plain("CODEX_PROMPT_CAPACITY_EXCEEDED")
        == "AI 输入超过容量上限"
    )


def test_thesis_gist_truncates_and_prefixes():
    proposal = SimpleNamespace(thesis="第一行理由\n第二行不该出现")
    gist = fmt.thesis_gist(proposal, limit=10)
    assert gist.startswith("AI：第一行理由")
    assert "第二行" not in gist


def test_compose_summary_per_outcome():
    assert (
        fmt.compose_summary(outcome="EXECUTED", reason_code="", intent=_intent(Side.BUY))
        == "开多仓 @ 63140，止损 61980"
    )
    assert (
        fmt.compose_summary(
            outcome="RISK_REJECTED", reason_code="PORTFOLIO_RISK_BUDGET_EXHAUSTED", intent=None
        )
        == "未开仓 · 风控拒绝：组合风险超限"
    )
    assert (
        fmt.compose_summary(outcome="NO_TRADE", reason_code="INSUFFICIENT_NET_EDGE", intent=None)
        == "未开仓 · 扣掉成本后优势不足"
    )
    assert fmt.compose_summary(outcome="NO_ACTION", reason_code="", intent=None).startswith(
        "未行动"
    )


def test_equity_curve_is_running_sum_of_net_pnl():
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    outcomes = (
        SimpleNamespace(net_pnl=Decimal("22"), closed_at=now, outcome_id="a"),
        SimpleNamespace(net_pnl=Decimal("-14"), closed_at=now, outcome_id="b"),
        SimpleNamespace(net_pnl=Decimal("31"), closed_at=now, outcome_id="c"),
    )
    report = SimpleNamespace(
        window_start=now,
        window_end=now,
        net_pnl=Decimal("39"),
        total_fees=Decimal("0"),
        win_rate=Decimal("0.66"),
        profit_factor=Decimal("2.5"),
        maximum_drawdown=Decimal("14"),
        closed_trade_count=3,
    )
    window = EquityWindow(
        outcomes=outcomes,
        metrics=report,
        lookback_start=now,
        lookback_end=now,
    )
    result = ser.equity(window)
    assert [point["equity"] for point in result["curve"]] == ["22", "8", "39"]
    assert result["trade_count"] == 3
    assert result["summary"]["net_pnl"] == "39"
    assert result["summary"]["closed_trade_count"] == 3


def test_world_event_serializes_injection_flag():
    event = WorldEvent(
        event_id="NEWS:test-event",
        kind="NEWS",
        at=datetime(2026, 8, 18, 11, 40, tzinfo=UTC),
        source="TrendRadar",
        title="疑似夹带指令的推广内容",
        symbols=("BTCUSDT",),
        impact=0.12,
        injection_suspected=True,
    )
    dto = ser.world_event(event)
    assert dto["event_id"] == "NEWS:test-event"
    assert dto["injection_suspected"] is True
    assert dto["symbols"] == ["BTCUSDT"]


@pytest.mark.parametrize(
    ("enabled", "healthy", "leased", "expected"),
    [
        (False, None, False, "DISABLED"),
        (True, None, False, "ENABLED"),
        (True, True, False, "HEALTHY"),
        (True, False, False, "COOLDOWN"),
        (True, None, True, "LEASED"),
    ],
)
def test_account_state_keeps_enabled_distinct_from_unprobed_health(
    enabled: bool,
    healthy: bool | None,
    leased: bool,
    expected: str,
):
    status = AccountStatus(
        account_id=".codex-test",
        enabled=enabled,
        headroom_percent=None,
        healthy=healthy,
        observed_at=None,
        leased=leased,
        recent_failures=0,
    )

    assert ser.account_status(status)["state"] == expected


def test_health_is_unknown_without_data_and_bad_on_mismatch():
    now = datetime.now(UTC)
    empty_reader = SimpleNamespace(
        latest_reconciliation=lambda *, now: None,
        latest_market_observed_at=lambda: None,
        portfolio_protection_active=lambda: False,
        analysis_runtime_status=lambda *, now: _analysis_status(
            now, latest_success_at=None, release_aligned=None
        ),
    )
    config = SimpleNamespace(
        reconciliation=SimpleNamespace(maximum_report_age_seconds=180),
        risk=SimpleNamespace(
            maximum_market_age_seconds=60,
            maximum_account_age_seconds=30,
            kill_switch=False,
        ),
        **_health_policy_extras(),
    )
    result = assemble_health(empty_reader, config, now=now)
    assert result["overall"] == "unknown"

    mismatch = SimpleNamespace(status="MISMATCH", freeze_new_risk=True, as_of=now)
    bad_reader = SimpleNamespace(
        latest_reconciliation=lambda *, now: mismatch,
        latest_market_observed_at=lambda: None,
        portfolio_protection_active=lambda: False,
        analysis_runtime_status=lambda *, now: _analysis_status(now),
    )
    bad = assemble_health(bad_reader, config, now=now)
    assert bad["overall"] == "bad"
    reconciliation = next(c for c in bad["checks"] if c["key"] == "reconciliation")
    assert reconciliation["state"] == "bad"


def test_capital_health_uses_product_ledger_without_legacy_account_checks() -> None:
    now = datetime(2026, 8, 21, 6, 30, tzinfo=UTC)
    overview = CapitalOverview(
        enabled=True,
        account=SimpleNamespace(
            as_of=now,
            equity=Decimal("10000"),
            cash_balance=Decimal("10000"),
            settlement_asset="USDT",
            reconciled=True,
            kill_switch_active=False,
            drawdown_fraction=Decimal("0"),
        ),
        target=SimpleNamespace(
            as_of=now,
            reason_codes=("CASH_SELECTED_NO_POSITIVE_NET_EDGE",),
        ),
    )
    reader = SimpleNamespace(
        latest_market_observed_at=lambda: now,
        latest_perpetual_observed_at=lambda: now,
        analysis_runtime_status=lambda *, now: _analysis_status(now),
    )
    config = SimpleNamespace(
        capital=SimpleNamespace(
            enabled=True,
            execution_specs=(
                SimpleNamespace(
                    instrument=SimpleNamespace(
                        symbol="BTCUSDT",
                        product=SimpleNamespace(value="SPOT"),
                    )
                ),
            ),
            risk=SimpleNamespace(
                kill_switch=False,
                maximum_drawdown_fraction=Decimal("0.2"),
                maximum_quote_age_seconds=60,
            ),
        ),
        **_health_policy_extras(),
    )

    result = assemble_health(
        reader,
        config,
        now=now,
        capital_overview=overview,
    )

    checks = {item["key"]: item for item in result["checks"]}
    assert result["overall"] == "ok"
    assert checks["capital_account"]["state"] == "ok"
    assert checks["capital_decision"]["state"] == "ok"
    assert checks["capital_execution"]["state"] == "ok"
    assert checks["capital_performance"]["state"] == "ok"
    assert "reconciliation" not in checks

    cash_without_action = assemble_health(
        reader,
        config,
        now=now,
        capital_overview=CapitalOverview(enabled=True, account=overview.account),
    )
    cash_checks = {item["key"]: item for item in cash_without_action["checks"]}
    assert cash_checks["capital_decision"] == {
        "key": "capital_decision",
        "name": "资本决策",
        "state": "ok",
        "detail": "当前全现金 · 无待执行资本动作",
    }

    overview.account.revision = 1
    broken = assemble_health(
        reader,
        config,
        now=now,
        capital_overview=overview,
    )
    broken_checks = {item["key"]: item for item in broken["checks"]}
    assert broken["overall"] == "unknown"
    assert broken_checks["capital_performance"]["state"] == "unknown"


def test_health_ages_persisted_freshness_and_uses_real_kill_switch():
    observed_at = datetime(2026, 8, 18, 12, tzinfo=UTC)
    now = observed_at.replace(minute=2)
    report = SimpleNamespace(status="MATCHED", freeze_new_risk=False, as_of=now)
    reader = SimpleNamespace(
        latest_reconciliation=lambda *, now: report,
        latest_market_observed_at=lambda: observed_at,
        portfolio_protection_active=lambda: False,
        analysis_runtime_status=lambda *, now: _analysis_status(now),
    )
    config = SimpleNamespace(
        reconciliation=SimpleNamespace(maximum_report_age_seconds=180),
        risk=SimpleNamespace(
            maximum_market_age_seconds=60,
            maximum_account_age_seconds=60,
            kill_switch=True,
        ),
        **_health_policy_extras(),
    )

    result = assemble_health(reader, config, now=now)

    checks = {item["key"]: item for item in result["checks"]}
    assert checks["data_freshness"]["state"] == "bad"
    assert checks["kill_switch"]["state"] == "bad"
    assert result["overall"] == "bad"


def test_health_includes_perpetual_freshness_when_capability_is_enabled() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    report = SimpleNamespace(status="MATCHED", freeze_new_risk=False, as_of=now)
    reader = SimpleNamespace(
        latest_reconciliation=lambda *, now: report,
        latest_market_observed_at=lambda: now,
        latest_perpetual_observed_at=lambda: now - timedelta(seconds=901),
        portfolio_protection_active=lambda: False,
        analysis_runtime_status=lambda *, now: _analysis_status(now),
    )
    config = SimpleNamespace(
        reconciliation=SimpleNamespace(maximum_report_age_seconds=180),
        risk=SimpleNamespace(
            maximum_market_age_seconds=60,
            maximum_account_age_seconds=60,
            kill_switch=False,
        ),
        market_data=SimpleNamespace(
            perpetual_instruments=("BINANCE:USD_M_PERPETUAL:BTCUSDT",),
            perpetual_poll_seconds=300,
        ),
        **_health_policy_extras(),
    )

    checks = {
        item["key"]: item
        for item in assemble_health(reader, config, now=now)["checks"]
    }

    assert checks["data_freshness"]["state"] == "bad"
    assert "永续 901/900 秒" in checks["data_freshness"]["detail"]


def test_health_reads_persisted_portfolio_kill_switch() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    report = SimpleNamespace(status="MATCHED", freeze_new_risk=False, as_of=now)
    reader = SimpleNamespace(
        latest_reconciliation=lambda *, now: report,
        latest_market_observed_at=lambda: now,
        portfolio_protection_active=lambda: True,
        analysis_runtime_status=lambda *, now: _analysis_status(now),
    )
    config = SimpleNamespace(
        reconciliation=SimpleNamespace(maximum_report_age_seconds=180),
        risk=SimpleNamespace(
            maximum_market_age_seconds=60,
            maximum_account_age_seconds=60,
            kill_switch=False,
        ),
        **_health_policy_extras(),
    )

    result = assemble_health(reader, config, now=now)

    checks = {item["key"]: item for item in result["checks"]}
    assert checks["kill_switch"]["state"] == "bad"
    assert result["overall"] == "bad"


def test_health_surfaces_control_plane_backlog_and_release_drift() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    report = SimpleNamespace(status="MATCHED", freeze_new_risk=False, as_of=now)
    reader = SimpleNamespace(
        latest_reconciliation=lambda *, now: report,
        latest_market_observed_at=lambda: now,
        portfolio_protection_active=lambda: False,
        analysis_runtime_status=lambda *, now: _analysis_status(
            now,
            pending_outbox_count=3,
            oldest_pending_outbox_at=now - timedelta(seconds=10),
            release_aligned=False,
            overdue_forecast_count=2,
            oldest_overdue_analysis_at=now - timedelta(hours=5),
        ),
    )
    config = SimpleNamespace(
        reconciliation=SimpleNamespace(maximum_report_age_seconds=180),
        risk=SimpleNamespace(
            maximum_market_age_seconds=60,
            maximum_account_age_seconds=60,
            kill_switch=False,
        ),
        **_health_policy_extras(),
    )

    result = assemble_health(reader, config, now=now)

    checks = {item["key"]: item for item in result["checks"]}
    assert checks["trigger_delivery"]["state"] == "bad"
    assert checks["forecast_settlement"]["state"] == "bad"
    assert checks["release_alignment"]["state"] == "bad"
    assert result["overall"] == "bad"


def test_health_reads_temporal_coordinator_pending_state() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    report = SimpleNamespace(status="MATCHED", freeze_new_risk=False, as_of=now)
    reader = SimpleNamespace(
        latest_reconciliation=lambda *, now: report,
        latest_market_observed_at=lambda: now,
        portfolio_protection_active=lambda: False,
        analysis_runtime_status=lambda *, now: _analysis_status(now),
    )
    config = SimpleNamespace(
        reconciliation=SimpleNamespace(maximum_report_age_seconds=180),
        risk=SimpleNamespace(
            maximum_market_age_seconds=60,
            maximum_account_age_seconds=60,
            kill_switch=False,
        ),
        **_health_policy_extras(),
    )

    result = assemble_health(
        reader,
        config,
        now=now,
        coordinator_statuses=(
            {"symbol": "BTCUSDT", "pending_count": 0, "active_batch_id": None},
            {"symbol": "ETHUSDT", "pending_count": 4, "active_batch_id": None},
        ),
    )

    check = next(item for item in result["checks"] if item["key"] == "trigger_coordinator")
    assert check["state"] == "warn"
    assert "状态不明 4 条" in check["detail"]
    assert result["overall"] == "warn"


def test_health_treats_policy_deferred_triggers_as_healthy() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    report = SimpleNamespace(status="MATCHED", freeze_new_risk=False, as_of=now)
    reader = SimpleNamespace(
        latest_reconciliation=lambda *, now: report,
        latest_market_observed_at=lambda: now,
        portfolio_protection_active=lambda: False,
        analysis_runtime_status=lambda *, now: _analysis_status(now),
    )
    config = SimpleNamespace(
        reconciliation=SimpleNamespace(maximum_report_age_seconds=180),
        risk=SimpleNamespace(
            maximum_market_age_seconds=60,
            maximum_account_age_seconds=60,
            kill_switch=False,
        ),
        **_health_policy_extras(),
    )

    result = assemble_health(
        reader,
        config,
        now=now,
        coordinator_statuses=(
            {
                "symbol": "BTCUSDT",
                "pending_count": 2,
                "active_batch_id": None,
                "next_reconsider_at": (now + timedelta(minutes=5)).isoformat(),
            },
            {
                "symbol": "ETHUSDT",
                "pending_count": 3,
                "active_batch_id": "batch-active",
                "next_reconsider_at": None,
            },
        ),
    )

    check = next(item for item in result["checks"] if item["key"] == "trigger_coordinator")
    assert check == {
        "key": "trigger_coordinator",
        "name": "触发协调器",
        "state": "ok",
        "detail": "正常等待 5 条 · 执行中 1 批",
    }
    assert result["overall"] == "ok"


def test_health_keeps_terminal_trigger_failure_visible_until_recovery() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    report = SimpleNamespace(status="MATCHED", freeze_new_risk=False, as_of=now)
    reader = SimpleNamespace(
        latest_reconciliation=lambda *, now: report,
        latest_market_observed_at=lambda: now,
        portfolio_protection_active=lambda: False,
        analysis_runtime_status=lambda *, now: _analysis_status(now),
    )
    config = SimpleNamespace(
        reconciliation=SimpleNamespace(maximum_report_age_seconds=180),
        risk=SimpleNamespace(
            maximum_market_age_seconds=60,
            maximum_account_age_seconds=60,
            kill_switch=False,
        ),
        **_health_policy_extras(),
    )

    result = assemble_health(
        reader,
        config,
        now=now,
        coordinator_statuses=(
            {
                "symbol": "BTCUSDT",
                "pending_count": 0,
                "active_batch_id": None,
                "unresolved_failure": True,
            },
        ),
    )

    check = next(item for item in result["checks"] if item["key"] == "trigger_coordinator")
    assert check["state"] == "bad"
    assert "最近一批处理失败" in check["detail"]
    assert result["overall"] == "bad"


@pytest.mark.parametrize(
    ("percent", "expected"),
    ((89.9, "ok"), (90.0, "warn"), (95.0, "bad")),
)
def test_health_surfaces_host_disk_pressure(percent, expected) -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    report = SimpleNamespace(status="MATCHED", freeze_new_risk=False, as_of=now)
    reader = SimpleNamespace(
        latest_reconciliation=lambda *, now: report,
        latest_market_observed_at=lambda: now,
        portfolio_protection_active=lambda: False,
        analysis_runtime_status=lambda *, now: _analysis_status(now),
    )
    config = SimpleNamespace(
        reconciliation=SimpleNamespace(maximum_report_age_seconds=180),
        risk=SimpleNamespace(
            maximum_market_age_seconds=60,
            maximum_account_age_seconds=60,
            kill_switch=False,
        ),
        **_health_policy_extras(),
    )

    result = assemble_health(
        reader,
        config,
        now=now,
        host_resources={"disk": {"percent": percent}},
    )

    disk = next(item for item in result["checks"] if item["key"] == "host_disk")
    assert disk["state"] == expected
    assert result["overall"] == expected


def test_sample_host_resources_shape():
    sample = sample_host_resources()
    assert 0.0 <= sample["cpu_percent"] <= 100.0
    assert set(sample["memory"]) == {"used_bytes", "total_bytes", "percent"}
    assert set(sample["disk"]) == {"used_bytes", "total_bytes", "percent"}
    assert set(sample["load_average"]) == {"1m", "5m", "15m"}
