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

from investment_manager.entrypoints.dashboard import serializers as ser
from investment_manager.entrypoints.dashboard.capital import CapitalOverview
from investment_manager.entrypoints.dashboard.evaluation import (
    serialize_capital_choice_evidence,
    serialize_trading_cost_evidence,
)
from investment_manager.entrypoints.dashboard.health import assemble_health
from investment_manager.entrypoints.dashboard.producer_capital import (
    ProducerCapitalEvidence,
    serialize_forecast_stability_evidence,
    serialize_producer_capital_evidence,
)
from investment_manager.entrypoints.dashboard.read_models import (
    AccountStatus,
    AnalysisRuntimeStatus,
    AnalysisScopeRuntimeStatus,
    AssessmentQualityStatus,
    WorldEvent,
)
from investment_manager.entrypoints.dashboard.resources import sample_host_resources
from investment_manager.forecast.models import ExposureDirection
from investment_manager.portfolio.evaluation import (
    CapitalChoiceCandidateOutcome,
    CapitalChoiceEvidence,
    CapitalChoiceExposureOutcome,
    evaluate_trading_cost,
)


def test_producer_capital_separates_material_trigger_cost_from_producer_paths() -> None:
    at = datetime(2026, 8, 28, 14, 30, tzinfo=UTC)

    def path(label: str, *, equity: str, fee: str, turnover: str, decisions: int):
        accounting = SimpleNamespace(
            net_pnl=Decimal(equity) - Decimal("10000"),
            price_pnl=Decimal(equity) - Decimal("10000") + Decimal(fee),
            funding_pnl=Decimal("0"),
            fee_cost=Decimal(fee),
        )
        account = SimpleNamespace(
            equity=Decimal(equity),
            accounting=accounting,
            drawdown_fraction=Decimal("0"),
            positions=(),
        )
        return SimpleNamespace(
            label=label,
            producer_id=label.lower(),
            producer_behavior_id=f"{label.lower()}-behavior",
            panel_ids=tuple(f"panel-{index}" for index in range(decisions)),
            steps=tuple(SimpleNamespace(execution_groups=()) for _ in range(decisions)),
            path=SimpleNamespace(
                account=account,
                gross_turnover=Decimal(turnover),
            ),
        )

    cadence = SimpleNamespace(
        shared_decision_slot_sets=(("cadence",),),
        paths=(path("AI_QUANT", equity="10005", fee="1", turnover="2000", decisions=1),),
    )
    all_slots = SimpleNamespace(
        comparison_id="comparison",
        evaluation_version="v1",
        as_of=at,
        initial_cash=Decimal("10000"),
        shared_decision_slot_sets=(("cadence",), ("material",)),
        paths=(path("AI_QUANT", equity="10003", fee="2.5", turnover="5000", decisions=2),),
    )

    payload = serialize_producer_capital_evidence(
        ProducerCapitalEvidence(all_slots=all_slots, cadence_only=cadence)  # type: ignore[arg-type]
    )["producer_capital_evidence"]

    assert payload["shared_panel_count"] == 2
    assert payload["trigger_policy"] == {
        "cadence_panel_count": 1,
        "material_panel_count": 1,
        "paths": [
            {
                "label": "AI_QUANT",
                "final_equity_delta": "-2",
                "fee_cost_delta": "1.5",
                "gross_turnover_delta": "3000",
                "decision_count_delta": 1,
            }
        ],
    }


def test_capital_choice_evidence_has_a_plain_cost_after_projection() -> None:
    decision_at = datetime(2026, 8, 26, 19, tzinfo=UTC)
    best = CapitalChoiceCandidateOutcome(
        projection_id="btc-perp-long",
        instrument_key="BINANCE:USD_M_PERPETUAL:BTCUSDT",
        direction=ExposureDirection.LONG,
        predicted_net_bps=Decimal("-9.04"),
        realized_net_bps=Decimal("14.65"),
    )
    evidence = CapitalChoiceEvidence(
        evaluation_version="capital-choice-outcome-v4",
        capital_behavior_id="capital-v1",
        decision_id="target-1",
        decision_at=decision_at,
        evaluation_at=decision_at + timedelta(hours=4),
        candidate_count=3,
        exposures=(
            CapitalChoiceExposureOutcome(
                economic_exposure_id="CRYPTO_NETWORK:BTC:USDT",
                selected=None,
                best_realized=best,
                opportunity_gap_bps=Decimal("14.65"),
                missed_profitable_exposure=True,
                selected_unprofitable_exposure=False,
            ),
        ),
    )

    assert serialize_capital_choice_evidence(evidence) == {
        "capital_choice_evidence": {
            "evaluation_version": "capital-choice-outcome-v4",
            "capital_behavior_id": "capital-v1",
            "decision_id": "target-1",
            "decision_at": decision_at.isoformat(),
            "evaluation_at": (decision_at + timedelta(hours=4)).isoformat(),
            "candidate_count": 3,
            "missed_profitable_exposure_count": 1,
            "selected_unprofitable_exposure_count": 0,
            "exposures": [
                {
                    "economic_exposure_id": "CRYPTO_NETWORK:BTC:USDT",
                    "selected": None,
                    "best_realized": {
                        "projection_id": "btc-perp-long",
                        "instrument_key": "BINANCE:USD_M_PERPETUAL:BTCUSDT",
                        "direction": "LONG",
                        "predicted_net_bps": "-9.04",
                        "realized_net_bps": "14.65",
                    },
                    "opportunity_gap_bps": "14.65",
                    "missed_profitable_exposure": True,
                    "selected_unprofitable_exposure": False,
                }
            ],
        }
    }


def test_empty_trading_cost_evidence_is_explicit_and_non_judgmental() -> None:
    evidence = evaluate_trading_cost(())

    assert serialize_trading_cost_evidence(evidence) == {
        "trading_cost_evidence": {
            "evaluation_version": "trading-cost-evidence-v1",
            "fill_count": 0,
            "round_trip_count": 0,
            "open_lot_count": 0,
            "gross_turnover": "0",
            "realized_gross_pnl": "0",
            "closed_fee_cost": "0",
            "open_fee_cost": "0",
            "realized_net_pnl": "0",
            "positive_gross_pnl": "0",
            "cost_reversal_round_trip_count": 0,
            "accounting_reconciled": None,
            "closed_fee_to_realized_gross_pnl": None,
            "closed_fee_to_positive_gross_pnl": None,
            "minimum_holding_seconds": None,
            "median_holding_seconds": None,
            "maximum_holding_seconds": None,
        }
    }


def test_forecast_stability_projection_is_bounded_to_decision_relevant_totals() -> None:
    evidence = SimpleNamespace(
        sources=(
            SimpleNamespace(
                label="CONTEXT_AI",
                role="CAPITAL_CANDIDATE",
                forecast=SimpleNamespace(
                    assignment_count=4,
                    successful_replica_count=3,
                    failed_replica_count=0,
                    complete_sample_count=3,
                    mean_max_expected_gross_difference_bps=Decimal("4.25"),
                    maximum_expected_gross_difference_bps=Decimal("7.80"),
                    canonical_direction_flip_count=0,
                ),
                capital=SimpleNamespace(
                    replayable_case_count=3,
                    unreplayable_case_count=0,
                    cash_flip_count=0,
                    expression_flip_count=0,
                    target_change_count=1,
                    maximum_allocation_fraction_delta=Decimal("0.04"),
                    maximum_absolute_final_equity_delta=Decimal("1.20"),
                    maximum_absolute_fee_cost_delta=Decimal("0.40"),
                    maximum_absolute_turnover_delta=Decimal("800"),
                ),
            ),
            SimpleNamespace(
                label="AI_QUANT",
                role="RESEARCH",
                forecast=SimpleNamespace(
                    assignment_count=2,
                    successful_replica_count=2,
                    failed_replica_count=0,
                    complete_sample_count=2,
                    mean_max_expected_gross_difference_bps=Decimal("1.50"),
                    maximum_expected_gross_difference_bps=Decimal("2.75"),
                    canonical_direction_flip_count=1,
                ),
                capital=SimpleNamespace(
                    replayable_case_count=2,
                    unreplayable_case_count=0,
                    cash_flip_count=0,
                    expression_flip_count=0,
                    target_change_count=0,
                    maximum_allocation_fraction_delta=Decimal("0"),
                    maximum_absolute_final_equity_delta=Decimal("0"),
                    maximum_absolute_fee_cost_delta=Decimal("0"),
                    maximum_absolute_turnover_delta=Decimal("0"),
                ),
            ),
        ),
    )

    assert serialize_forecast_stability_evidence(evidence) == {
        "forecast_stability_evidence": {
            "sources": [
                {
                    "label": "CONTEXT_AI",
                    "role": "CAPITAL_CANDIDATE",
                    "assignment_count": 4,
                    "successful_replica_count": 3,
                    "failed_replica_count": 0,
                    "complete_sample_count": 3,
                    "mean_expected_gross_difference_bps": "4.25",
                    "maximum_expected_gross_difference_bps": "7.80",
                    "direction_flip_count": 0,
                    "capital": {
                        "replayable_case_count": 3,
                        "unreplayable_case_count": 0,
                        "cash_flip_count": 0,
                        "expression_flip_count": 0,
                        "target_change_count": 1,
                        "maximum_allocation_fraction_delta": "0.04",
                        "maximum_absolute_final_equity_delta": "1.20",
                        "maximum_absolute_fee_cost_delta": "0.40",
                        "maximum_absolute_turnover_delta": "800",
                    },
                },
                {
                    "label": "AI_QUANT",
                    "role": "RESEARCH",
                    "assignment_count": 2,
                    "successful_replica_count": 2,
                    "failed_replica_count": 0,
                    "complete_sample_count": 2,
                    "mean_expected_gross_difference_bps": "1.50",
                    "maximum_expected_gross_difference_bps": "2.75",
                    "direction_flip_count": 1,
                    "capital": {
                        "replayable_case_count": 2,
                        "unreplayable_case_count": 0,
                        "cash_flip_count": 0,
                        "expression_flip_count": 0,
                        "target_change_count": 0,
                        "maximum_allocation_fraction_delta": "0",
                        "maximum_absolute_final_equity_delta": "0",
                        "maximum_absolute_fee_cost_delta": "0",
                        "maximum_absolute_turnover_delta": "0",
                    },
                },
            ]
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


def _health_policy_extras() -> dict:
    return {
        "assessment": SimpleNamespace(review_trigger_symbol="BTCUSDT"),
        "trigger": SimpleNamespace(
            heartbeat_minutes=15,
            outbox_fallback_poll_seconds=1,
        ),
        "shadow": SimpleNamespace(analysis_deadline_seconds=300),
    }


def _healthy_capital_health_context(
    now: datetime,
    *,
    analysis: AnalysisRuntimeStatus | None = None,
) -> tuple[CapitalOverview, SimpleNamespace, SimpleNamespace]:
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
    )
    reader = SimpleNamespace(
        latest_market_observed_at=lambda: now,
        analysis_runtime_status=lambda *, now: analysis or _analysis_status(now),
    )
    config = SimpleNamespace(
        capital=SimpleNamespace(
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
    return overview, reader, config


def test_health_exposes_structural_output_failure_without_reclassifying_history() -> None:
    now = datetime(2026, 8, 21, 14, tzinfo=UTC)
    overview, reader, config = _healthy_capital_health_context(now)
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
        capital_overview=overview,
        assessment_quality=rejected,
    )
    check = next(item for item in result["checks"] if item["key"] == "ai_output_quality")

    assert check["state"] == "warn"
    assert check["name"] == "世界认知 AI"
    assert check["detail"] == ("最近一次模型调用未产生可持久化结果：AI 输出格式不符合契约")

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
        capital_overview=overview,
        assessment_quality=clean,
    )
    clean_check = next(
        item for item in clean_result["checks"] if item["key"] == "ai_output_quality"
    )
    assert clean_check["state"] == "ok"

    recovered = replace(
        rejected,
        latest_attempt_status="SUCCEEDED",
        latest_attempt_reason=None,
        execution_count_24h=13,
        final_success_count_24h=12,
    )
    recovered_result = assemble_health(
        reader,
        config,
        now=now,
        capital_overview=overview,
        assessment_quality=recovered,
    )
    recovered_check = next(
        item for item in recovered_result["checks"] if item["key"] == "ai_output_quality"
    )
    assert recovered_check == {
        "key": "ai_output_quality",
        "name": "世界认知 AI",
        "state": "ok",
        "detail": "最近一次输出有效 · 过去 24 小时成功 12/13 次 · 拒绝 1 次",
    }


def test_world_event_serializes_injection_flag():
    event = WorldEvent(
        event_id="NEWS:test-event",
        kind="NEWS",
        at=datetime(2026, 8, 18, 11, 40, tzinfo=UTC),
        source="TrendRadar",
        title="疑似夹带指令的推广内容",
        symbols=("BTCUSDT",),
        attention_priority=0.12,
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
                        symbol="SPYUSDT",
                        product=SimpleNamespace(value="TRADFI_PERPETUAL"),
                    )
                ),
                SimpleNamespace(
                    instrument=SimpleNamespace(
                        symbol="BTCUSDT",
                        product=SimpleNamespace(value="USD_M_PERPETUAL"),
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


def test_health_surfaces_control_plane_backlog_and_release_drift() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    overview, reader, config = _healthy_capital_health_context(
        now,
        analysis=_analysis_status(
            now,
            pending_outbox_count=3,
            oldest_pending_outbox_at=now - timedelta(seconds=10),
            release_aligned=False,
            overdue_forecast_count=2,
            oldest_overdue_analysis_at=now - timedelta(hours=5),
        ),
    )

    result = assemble_health(reader, config, now=now, capital_overview=overview)

    checks = {item["key"]: item for item in result["checks"]}
    assert checks["trigger_delivery"]["state"] == "bad"
    assert checks["forecast_settlement"]["state"] == "bad"
    assert checks["release_alignment"]["state"] == "bad"
    assert result["overall"] == "bad"


def test_health_reads_temporal_coordinator_pending_state() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    overview, reader, config = _healthy_capital_health_context(now)

    result = assemble_health(
        reader,
        config,
        now=now,
        capital_overview=overview,
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
    overview, reader, config = _healthy_capital_health_context(now)

    result = assemble_health(
        reader,
        config,
        now=now,
        capital_overview=overview,
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
    overview, reader, config = _healthy_capital_health_context(now)

    result = assemble_health(
        reader,
        config,
        now=now,
        capital_overview=overview,
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
    overview, reader, config = _healthy_capital_health_context(now)

    result = assemble_health(
        reader,
        config,
        now=now,
        capital_overview=overview,
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
