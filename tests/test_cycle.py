from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from quant_core.analyst import AnalystResult
from quant_core.cycle import AnalysisCycle, CycleInput
from quant_core.decision import FrequencyController, FrequencyState
from quant_core.domain import Action, AnalysisProposal, CycleOutcome, OrderStatus, RiskOutcome


class StaticAnalyst:
    def __init__(self, result: AnalystResult) -> None:
        self.result = result
        self.calls = 0

    def analyze(self, panel):
        self.calls += 1
        return self.result


def _propose_config(app_config):
    return app_config.model_copy(
        update={
            "pipeline": app_config.pipeline.model_copy(
                update={"version": "propose-pipeline-v1", "ai_mode": "PROPOSE"}
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
        "position_protected",
    }


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

    decision = FrequencyController(app_config.frequency).evaluate(
        intent,
        as_of=replay_input.market.as_of,
        spread_bps=Decimal("1"),
        current_price=intent.reference_price * Decimal("1.01"),
        state=FrequencyState(),
    )

    assert not decision.allowed
    assert decision.reason_code == "ALPHA_ALREADY_CONSUMED"
    assert decision.price_move_consumed_bps >= Decimal("100")


def test_daily_order_budget_is_enforced(app_config, replay_input) -> None:
    at_limit = replay_input.model_copy(
        update={"frequency_orders_today": app_config.frequency.maximum_orders_per_day}
    )

    result = AnalysisCycle.create(app_config).run(at_limit)

    assert result.outcome == CycleOutcome.NO_TRADE
    assert result.reason_code == "DAILY_ORDER_BUDGET_EXHAUSTED"


def test_propose_pipeline_fails_closed_even_when_program_signal_exists(
    app_config, replay_input
) -> None:
    analyst = StaticAnalyst(AnalystResult(False, None, "CODEX_SCHEMA_INVALID"))

    result = AnalysisCycle.create(_propose_config(app_config), analyst=analyst).run(replay_input)

    assert result.outcome == CycleOutcome.NO_TRADE
    assert result.reason_code == "CODEX_SCHEMA_INVALID"
    assert len(result.candidates) == 1
    assert result.order is None


def test_successful_ai_no_action_keeps_independent_program_baseline(
    app_config, replay_input
) -> None:
    proposal = AnalysisProposal(
        proposal_id="proposal-no-action",
        suggested_action=Action.NO_ACTION,
        symbol=replay_input.market.symbol,
        thesis="信息证据不足，不独立提出交易候选",
        confidence=Decimal("0.60"),
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
