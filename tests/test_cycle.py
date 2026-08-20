from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from quant_core.analyst import AnalystResult
from quant_core.config import AiMode
from quant_core.cycle import AnalysisCycle, CycleInput
from quant_core.decision import (
    FrequencyController,
    FrequencyState,
    estimate_round_trip_cost_amount,
    estimate_round_trip_cost_bps,
)
from quant_core.domain import (
    Action,
    AnalysisProposal,
    CycleOutcome,
    DirectionalForecast,
    DirectionalView,
    OrderStatus,
    OrderType,
    PriceCondition,
    RiskOutcome,
    Side,
)
from quant_core.trigger import TriggerDecision, TriggerReason


class StaticAnalyst:
    def __init__(self, result: AnalystResult) -> None:
        self.result = result
        self.calls = 0
        self.triggers = []

    def analyze(self, panel, *, trigger=None):
        self.calls += 1
        self.triggers.append(trigger)
        return self.result


class NoCandidateStrategy:
    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, *, market, account, features, events=()):
        self.calls += 1
        return ()


def _propose_config(app_config):
    return app_config.model_copy(
        update={
            "pipeline": app_config.pipeline.model_copy(
                update={
                    "version": "propose-pipeline-v1",
                    "ai_mode": AiMode.PROPOSE,
                }
            )
        }
    )


def test_off_pipeline_executes_complete_mock_cycle(app_config, replay_input) -> None:
    cycle = AnalysisCycle.create(app_config)

    result = cycle.run(replay_input)

    assert result.outcome == CycleOutcome.EXECUTED
    assert len(result.candidates) == 1
    assert result.intent is not None
    assert result.risk_decision is not None
    assert result.risk_decision.outcome == RiskOutcome.APPROVED
    assert result.risk_decision.reservation is not None
    assert result.order is not None
    assert result.order.status == OrderStatus.FILLED
    assert result.order.fills[0].fee > 0
    assert {dict(item.dimensions)["metric"] for item in result.metrics} == {
        "market_data_age_seconds",
        "account_data_age_seconds",
        "account_reconciled",
        "signal_count",
        "expected_net_edge_bps",
        "remaining_gross_edge_bps",
        "price_move_consumed_bps",
        "signal_age_seconds",
        "risk_approved",
        "executed_order_count",
        "execution_handoff_age_seconds",
        "position_protected",
    }


def test_disabled_program_strategy_emits_no_candidate(app_config, replay_input) -> None:
    config = app_config.model_copy(
        update={
            "strategy": app_config.strategy.model_copy(
                update={"version": "price-trend-retired-v1", "enabled": False}
            )
        }
    )

    result = AnalysisCycle.create(config).run(replay_input)

    assert result.outcome == CycleOutcome.NO_ACTION
    assert result.candidates == ()


def test_uncalibrated_shadow_candidate_is_labeled_but_cannot_reach_composition(
    base_app_config, replay_input
) -> None:
    result = AnalysisCycle.create(base_app_config).run(replay_input)

    assert result.outcome == CycleOutcome.NO_ACTION
    assert result.reason_code == "NO_VALID_CANDIDATE"
    assert len(result.candidates) == 1
    assert result.candidates[0].unknowns == ("EDGE_CALIBRATION_MISSING",)
    assert result.intent is None
    assert result.order is None


def test_cycle_uses_injected_programmatic_strategy_without_price_trend_hardcode(
    app_config, replay_input
) -> None:
    strategy = NoCandidateStrategy()

    result = AnalysisCycle.create(app_config, strategy=strategy).run(replay_input)

    assert strategy.calls == 1
    assert not result.candidates
    assert result.outcome == CycleOutcome.NO_ACTION
    assert result.reason_code == "NO_VALID_CANDIDATE"


def test_uncalibrated_ai_candidate_has_zero_edge_and_cannot_trade(
    base_app_config, replay_input
) -> None:
    completed_at = replay_input.market.as_of + timedelta(seconds=12)
    proposal = AnalysisProposal(
        proposal_id="proposal-open-uncalibrated",
        suggested_action=Action.OPEN,
        symbol=replay_input.market.symbol,
        side=Side.BUY,
        horizon_minutes=60,
        thesis="仅作为未校准 Shadow 假设记录",
        entry_condition=PriceCondition(order_type=OrderType.MARKET),
        invalidation_price=replay_input.market.last * Decimal("0.99"),
        valid_until=replay_input.market.as_of + timedelta(minutes=10),
        confidence=Decimal("0.90"),
        forecasts=(
            DirectionalForecast(
                horizon_minutes=60,
                directional_view=DirectionalView.UP,
                confidence=Decimal("0.90"),
            ),
            DirectionalForecast(
                horizon_minutes=240,
                directional_view=DirectionalView.UNCERTAIN,
                confidence=Decimal("0.55"),
            ),
        ),
    )
    analyst = StaticAnalyst(
        AnalystResult(
            True,
            proposal,
            "CODEX_ANALYSIS_SUCCEEDED",
            "codex_b",
            1,
            completed_at=completed_at,
        )
    )

    trigger = TriggerDecision(
        should_run=True,
        reason=TriggerReason.EVENT_BATCH,
        evidence_ids=(replay_input.events[0].evidence_id,),
    )
    result = AnalysisCycle.create(_propose_config(base_app_config), analyst=analyst).prepare(
        replay_input,
        trigger=trigger,
    )

    ai_candidate = next(item for item in result.candidates if item.producer_id == "codex-analyst")
    assert ai_candidate.expected_gross_bps == 0
    assert ai_candidate.unknowns == ("EDGE_CALIBRATION_MISSING",)
    assert ai_candidate.signal_observed_at == completed_at
    assert result.outcome == CycleOutcome.NO_ACTION
    assert result.reason_code == "NO_VALID_CANDIDATE"
    assert result.risk_decision is None
    assert result.order is None
    assert analyst.triggers == [trigger]


def test_cycle_reserves_direct_trigger_evidence_before_general_ranking(
    app_config, replay_input
) -> None:
    original = replay_input.events[0]
    trigger_copy = original.model_copy(
        update={
            "evidence_id": "direct-trigger-copy",
            "source": "lower-ranked-source",
            "relevance": Decimal("0"),
            "impact": Decimal("0"),
            "source_reliability": Decimal("0"),
            "novelty": Decimal("0"),
        }
    )
    one_item_panel = app_config.model_copy(
        update={
            "panel": app_config.panel.model_copy(update={"max_evidence": 1}),
        }
    )
    cycle_input = replay_input.model_copy(
        update={"events": (original, trigger_copy)}
    )
    trigger = TriggerDecision(
        should_run=True,
        reason=TriggerReason.EVENT_BATCH,
        evidence_ids=(trigger_copy.evidence_id,),
    )

    result = AnalysisCycle.create(one_item_panel).prepare(
        cycle_input,
        trigger=trigger,
    )

    assert [item.evidence_id for item in result.panel.evidence] == [
        trigger_copy.evidence_id
    ]


def test_replay_is_idempotent_and_does_not_duplicate_order(app_config, replay_input) -> None:
    cycle = AnalysisCycle.create(app_config)

    first = cycle.run(replay_input)
    second = cycle.run(replay_input)

    assert first == second
    assert cycle.ledger.count == 1
    assert len(cycle.exchange.orders) == 1


def test_stale_market_is_rejected_by_risk(app_config, replay_input) -> None:
    stale_market = replay_input.market.model_copy(
        update={"observed_at": replay_input.market.as_of - timedelta(minutes=10)}
    )
    stale_input = CycleInput(
        market=stale_market,
        account=replay_input.account,
        events=replay_input.events,
    )

    result = AnalysisCycle.create(app_config).run(stale_input)

    assert result.outcome == CycleOutcome.RISK_REJECTED
    assert result.reason_code == "DATA_STALE"
    assert result.order is None


def test_frequency_gate_rejects_unprofitable_edge(app_config, replay_input) -> None:
    strict_frequency = app_config.frequency.model_copy(
        update={"minimum_net_edge_bps": Decimal("999")}
    )
    strict_config = app_config.model_copy(update={"frequency": strict_frequency})

    result = AnalysisCycle.create(strict_config).run(replay_input)

    assert result.outcome == CycleOutcome.NO_TRADE
    assert result.reason_code == "INSUFFICIENT_NET_EDGE"
    assert result.risk_decision is None
    assert result.order is None


def test_remaining_edge_gate_rejects_signal_after_price_has_already_moved(
    app_config, replay_input
) -> None:
    result = AnalysisCycle.create(app_config).run(replay_input)
    assert result.intent is not None
    intent = result.intent

    decision = FrequencyController(app_config.frequency, app_config.execution).evaluate(
        intent,
        as_of=replay_input.market.as_of,
        spread_bps=Decimal("1"),
        current_price=intent.reference_price * Decimal("1.01"),
        state=FrequencyState(),
    )

    assert not decision.allowed
    assert decision.reason_code == "ALPHA_ALREADY_CONSUMED"
    assert decision.price_move_consumed_bps >= Decimal("100")


def test_frequency_cost_uses_round_trip_execution_policy(app_config, replay_input) -> None:
    result = AnalysisCycle.create(app_config).run(replay_input)
    assert result.intent is not None
    intent = result.intent
    arguments = {
        "as_of": replay_input.market.as_of,
        "spread_bps": Decimal("0"),
        "current_price": intent.reference_price,
        "state": FrequencyState(),
    }
    without_execution_cost = FrequencyController(
        app_config.frequency,
        app_config.execution.model_copy(
            update={"fee_bps": Decimal("0"), "market_slippage_bps": Decimal("0")}
        ),
    ).evaluate(intent, **arguments)
    with_execution_cost = FrequencyController(app_config.frequency, app_config.execution).evaluate(
        intent, **arguments
    )

    expected_round_trip = Decimal("2") * (
        app_config.execution.fee_bps + app_config.execution.market_slippage_bps
    )
    assert (
        without_execution_cost.expected_net_edge_bps - with_execution_cost.expected_net_edge_bps
        == expected_round_trip
    )

    limit_intent = intent.model_copy(
        update={
            "entry": PriceCondition(
                order_type=OrderType.LIMIT,
                price=intent.reference_price,
            )
        }
    )
    limit_cost = FrequencyController(app_config.frequency, app_config.execution).evaluate(
        limit_intent, **arguments
    )
    assert (
        limit_cost.expected_net_edge_bps - with_execution_cost.expected_net_edge_bps
        == app_config.execution.market_slippage_bps
    )


def test_realized_round_trip_cost_uses_each_legs_notional(app_config) -> None:
    cost = estimate_round_trip_cost_amount(
        entry_price=Decimal("100"),
        exit_price=Decimal("150"),
        quantity=Decimal("2"),
        entry_order_type=OrderType.MARKET,
        spread_bps=Decimal("1"),
        frequency=app_config.frequency,
        execution=app_config.execution,
    )
    expected = (
        Decimal("500") * app_config.execution.fee_bps
        + Decimal("500") * app_config.execution.market_slippage_bps
        + Decimal("500") * Decimal("0.5")
        + Decimal("200")
        * (
            app_config.frequency.funding_bps
            + app_config.frequency.latency_bps
            + app_config.frequency.adverse_selection_bps
            + app_config.frequency.uncertainty_buffer_bps
        )
    ) / Decimal("10000")
    assert cost == expected

    flat_cost = estimate_round_trip_cost_amount(
        entry_price=Decimal("100"),
        exit_price=Decimal("100"),
        quantity=Decimal("2"),
        entry_order_type=OrderType.MARKET,
        spread_bps=Decimal("1"),
        frequency=app_config.frequency,
        execution=app_config.execution,
    )
    estimated_bps = estimate_round_trip_cost_bps(
        entry_order_type=OrderType.MARKET,
        spread_bps=Decimal("1"),
        frequency=app_config.frequency,
        execution=app_config.execution,
    )
    assert flat_cost / Decimal("200") * Decimal("10000") == estimated_bps


def test_cycle_input_rejects_naive_last_entry_time(replay_input) -> None:
    naive = replay_input.market.as_of.replace(tzinfo=None)

    with pytest.raises(ValueError, match="时间必须包含时区"):
        CycleInput.model_validate(
            {
                **replay_input.model_dump(mode="python"),
                "frequency_last_entry_order_at": naive,
            }
        )


def test_daily_order_budget_is_enforced(app_config, replay_input) -> None:
    at_limit = replay_input.model_copy(
        update={"frequency_orders_today": app_config.frequency.maximum_orders_per_day}
    )

    result = AnalysisCycle.create(app_config).run(at_limit)

    assert result.outcome == CycleOutcome.NO_TRADE
    assert result.reason_code == "DAILY_ORDER_BUDGET_EXHAUSTED"


def test_propose_failure_does_not_block_independent_program_signal(
    app_config, replay_input
) -> None:
    analyst = StaticAnalyst(AnalystResult(False, None, "CODEX_SCHEMA_INVALID"))

    result = AnalysisCycle.create(_propose_config(app_config), analyst=analyst).run(replay_input)

    assert result.outcome == CycleOutcome.EXECUTED
    assert len(result.candidates) == 1
    assert result.order is not None
    codex_metric = next(
        item
        for item in result.metrics
        if dict(item.dimensions).get("metric") == "codex_analysis_success"
    )
    assert codex_metric.value == 0
    assert dict(codex_metric.dimensions)["reason"] == "CODEX_SCHEMA_INVALID"


def test_codex_latency_is_counted_before_program_signal_risk(
    app_config, replay_input
) -> None:
    completed_at = replay_input.market.as_of + timedelta(
        seconds=app_config.risk.maximum_account_age_seconds + 1
    )
    analyst = StaticAnalyst(
        AnalystResult(
            False,
            None,
            "CODEX_SCHEMA_INVALID",
            completed_at=completed_at,
        )
    )

    result = AnalysisCycle.create(
        _propose_config(app_config), analyst=analyst
    ).prepare(replay_input)

    assert result.outcome == CycleOutcome.RISK_REJECTED
    assert result.reason_code == "DATA_STALE"
    assert result.risk_decision is not None
    freshness = {
        item.rule_id: item for item in result.risk_decision.rule_results
    }
    assert freshness["account-freshness"].observed == str(
        int((completed_at - replay_input.account.observed_at).total_seconds())
    )
    assert next(
        item
        for item in result.metrics
        if dict(item.dimensions).get("metric") == "codex_analysis_success"
    ).observed_at == completed_at


def test_successful_ai_no_action_keeps_independent_program_baseline(
    app_config, replay_input
) -> None:
    proposal = AnalysisProposal(
        proposal_id="proposal-no-action",
        suggested_action=Action.NO_ACTION,
        symbol=replay_input.market.symbol,
        thesis="信息证据不足，不独立提出交易候选",
        confidence=Decimal("0.60"),
        forecasts=(
            DirectionalForecast(
                horizon_minutes=60,
                directional_view=DirectionalView.UNCERTAIN,
                confidence=Decimal("0.60"),
            ),
            DirectionalForecast(
                horizon_minutes=240,
                directional_view=DirectionalView.UNCERTAIN,
                confidence=Decimal("0.55"),
            ),
        ),
    )
    analyst = StaticAnalyst(AnalystResult(True, proposal, "CODEX_ANALYSIS_SUCCEEDED", "codex_b", 1))

    result = AnalysisCycle.create(_propose_config(app_config), analyst=analyst).run(replay_input)

    assert result.outcome == CycleOutcome.EXECUTED
    assert result.analysis_proposal is not None
    assert result.analysis_proposal.proposal_id.startswith("proposal_")
    assert result.analysis_proposal.thesis == proposal.thesis
    assert len(result.candidates) == 1
    assert result.candidates[0].producer_id == app_config.strategy.strategy_id
    assert analyst.calls == 1


def test_off_pipeline_never_calls_injected_analyst(app_config, replay_input) -> None:
    analyst = StaticAnalyst(AnalystResult(False, None, "SHOULD_NOT_RUN"))

    result = AnalysisCycle.create(app_config, analyst=analyst).run(replay_input)

    assert result.outcome == CycleOutcome.EXECUTED
    assert analyst.calls == 0
