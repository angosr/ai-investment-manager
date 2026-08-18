from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from quant_core.cycle import AnalysisCycle
from quant_core.domain import (
    ExitReason,
    MarketSnapshot,
    PositionLifecycleStatus,
)
from quant_core.execution import MockExchange
from quant_core.ledger import InMemoryFactLedger
from quant_core.lifecycle import PositionLifecycleManager
from quant_core.reconciliation import MockReconciler
from quant_core.risk_budget import InMemoryRiskBudgetStore


def _later_market(replay_input, *, last: Decimal, minutes: int) -> MarketSnapshot:
    as_of = replay_input.market.as_of + timedelta(minutes=minutes)
    return MarketSnapshot.model_validate(
        {
            **replay_input.market.model_dump(mode="json"),
            "cycle_id": f"cycle-lifecycle-{minutes}-{last}",
            "as_of": as_of,
            "observed_at": as_of,
            "bid": last - Decimal("0.01"),
            "ask": last + Decimal("0.01"),
            "last": last,
        }
    )


def test_entry_fill_creates_reconciled_account_and_protected_position(
    app_config, replay_input
) -> None:
    result = AnalysisCycle.create(app_config).run(replay_input)

    assert result.account_after is not None
    assert result.account_after.reconciled is True
    assert result.account_after.quote_balance < replay_input.account.quote_balance
    assert result.account_after.positions[0].symbol == "BTCUSDT"
    assert result.position_lifecycle is not None
    assert result.position_lifecycle.status == PositionLifecycleStatus.PROTECTED
    assert result.position_lifecycle.protection_id is not None


def test_stop_loss_closes_position_and_attributes_net_loss(app_config, replay_input) -> None:
    cycle = AnalysisCycle.create(app_config)
    opened = cycle.run(replay_input)
    assert opened.position_lifecycle is not None
    assert opened.account_after is not None
    stop_market = _later_market(
        replay_input,
        last=opened.position_lifecycle.stop_price - Decimal("0.10"),
        minutes=10,
    )
    manager = PositionLifecycleManager(
        exchange=cycle.exchange,
        reconciler=MockReconciler(),
        risk_budget=cycle.risk_budget,
    )

    closed = manager.evaluate(
        lifecycle=opened.position_lifecycle,
        market=stop_market,
        account=opened.account_after,
        pipeline_version=app_config.pipeline.version,
    )

    assert closed.lifecycle.status == PositionLifecycleStatus.CLOSED
    assert closed.lifecycle.exit_reason == ExitReason.STOP_LOSS
    assert closed.exit_order is not None
    assert closed.outcome is not None
    assert closed.outcome.net_pnl < 0
    assert {dict(item.dimensions)["metric"] for item in closed.metrics} == {
        "gross_pnl",
        "net_pnl",
        "maximum_favorable_excursion",
        "maximum_adverse_excursion",
        "holding_minutes",
    }
    assert closed.account.positions == ()
    assert cycle.risk_budget.status(opened.position_lifecycle.reservation_id) == "RELEASED"


def test_max_holding_time_closes_without_codex(app_config, replay_input) -> None:
    cycle = AnalysisCycle.create(app_config)
    opened = cycle.run(replay_input)
    assert opened.position_lifecycle is not None
    assert opened.account_after is not None
    later = _later_market(
        replay_input,
        last=opened.position_lifecycle.entry_price + Decimal("1"),
        minutes=app_config.strategy.horizon_minutes + 1,
    )

    closed = PositionLifecycleManager(
        exchange=cycle.exchange,
        reconciler=MockReconciler(),
        risk_budget=cycle.risk_budget,
    ).evaluate(
        lifecycle=opened.position_lifecycle,
        market=later,
        account=opened.account_after,
        pipeline_version=app_config.pipeline.version,
    )

    assert closed.lifecycle.exit_reason == ExitReason.MAX_HOLDING_TIME
    assert closed.outcome is not None
    assert closed.account.positions == ()


def test_protection_failure_forces_immediate_exit(app_config, replay_input) -> None:
    risk_budget = InMemoryRiskBudgetStore()
    exchange = MockExchange(app_config.execution, accept_protection=False)
    cycle = AnalysisCycle.with_adapters(
        app_config,
        ledger=InMemoryFactLedger(),
        exchange=exchange,
        risk_budget=risk_budget,
    )

    result = cycle.run(replay_input)

    assert result.reason_code == "PROTECTION_FAILURE_EMERGENCY_EXIT"
    assert result.position_lifecycle is not None
    assert result.position_lifecycle.status == PositionLifecycleStatus.CLOSED
    assert result.exit_order is not None
    assert result.decision_outcome is not None
    assert result.decision_outcome.exit_reason == ExitReason.PROTECTION_FAILURE
    assert len(result.outcome_metrics) == 5
    assert result.account_after is not None
    assert result.account_after.positions == ()
    assert risk_budget.status(result.position_lifecycle.reservation_id) == "RELEASED"
    assert len(exchange.orders) == 2


def test_partial_fill_cancels_remainder_before_protection(app_config, replay_input) -> None:
    partial_execution = app_config.execution.model_copy(
        update={"default_fill_fraction": Decimal("0.5")}
    )
    partial_config = app_config.model_copy(update={"execution": partial_execution})

    result = AnalysisCycle.create(partial_config).run(replay_input)

    assert result.order is not None
    assert result.order.status.value == "CANCELED"
    assert result.order.fills
    assert result.account_after is not None
    assert result.position_lifecycle is not None
    assert result.position_lifecycle.status == PositionLifecycleStatus.PROTECTED
