from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from investment_manager.execution.exit_policy import program_exit_triggered
from investment_manager.execution.ledger import InMemoryFactLedger
from investment_manager.execution.legacy_exchange import MockExchange
from investment_manager.execution.lifecycle import PositionLifecycleManager
from investment_manager.execution.models import (
    ExitReason,
    Fill,
    OrderStatus,
    PositionLifecycleStatus,
    ProgramExitCondition,
)
from investment_manager.execution.reconciliation import MockReconciler
from investment_manager.legacy.cycle import AnalysisCycle
from investment_manager.market.models import MarketSnapshot
from investment_manager.risk.budget import InMemoryRiskBudgetStore


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


def test_program_exit_is_shared_by_lifecycle_and_requires_matching_closed_bars(
    app_config, replay_input
) -> None:
    condition = ProgramExitCondition(
        version="close-below-sma3-v1",
        bar_interval_minutes=5,
        moving_average_bars=3,
    )
    falling_bars = tuple(
        bar.model_copy(update={"close": close})
        for bar, close in zip(
            replay_input.market.bars[-3:],
            (Decimal("101"), Decimal("100"), Decimal("98")),
            strict=True,
        )
    )
    falling_market = replay_input.market.model_copy(update={"bars": falling_bars})

    assert program_exit_triggered(condition, falling_market)
    assert not program_exit_triggered(
        condition.model_copy(update={"bar_interval_minutes": 1}),
        falling_market,
    )
    assert not program_exit_triggered(
        condition.model_copy(update={"moving_average_bars": 4}),
        falling_market,
    )

    cycle = AnalysisCycle.create(app_config)
    opened = cycle.run(replay_input)
    lifecycle = opened.position_lifecycle
    assert lifecycle is not None and opened.account_after is not None
    lifecycle = lifecycle.model_copy(update={"program_exit": condition})
    exit_market = _later_market(
        replay_input,
        last=lifecycle.entry_price + Decimal("1"),
        minutes=10,
    ).model_copy(update={"bars": falling_bars})

    closed = PositionLifecycleManager(
        exchange=cycle.exchange,
        reconciler=MockReconciler(),
        risk_budget=cycle.risk_budget,
    ).evaluate(
        lifecycle=lifecycle,
        market=exit_market,
        account=opened.account_after,
        pipeline_version=app_config.pipeline.version,
    )

    assert closed.lifecycle.exit_reason == ExitReason.PROGRAM_SIGNAL
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


class _ExitOrderExchange:
    def __init__(self, order) -> None:
        self._order = order

    def submit_position_exit(self, **_kwargs):
        return self._order


def test_exit_aggregates_all_fills_into_vwap_and_pnl(app_config, replay_input) -> None:
    cycle = AnalysisCycle.create(app_config)
    opened = cycle.run(replay_input)
    lifecycle = opened.position_lifecycle
    assert lifecycle is not None and opened.account_after is not None
    market = _later_market(replay_input, last=lifecycle.entry_price + Decimal("2"), minutes=10)
    template = cycle.exchange.submit_position_exit(
        lifecycle=lifecycle,
        market=market,
        reason=ExitReason.MAX_HOLDING_TIME,
    )
    first_quantity = lifecycle.quantity * Decimal("0.4")
    second_quantity = lifecycle.quantity - first_quantity
    first_price = lifecycle.entry_price + Decimal("1")
    second_price = lifecycle.entry_price + Decimal("3")
    fills = (
        Fill(
            fill_id="multi-fill-1",
            order_id=template.order_id,
            event_time=market.as_of,
            price=first_price,
            quantity=first_quantity,
            fee=Decimal("0.11"),
        ),
        Fill(
            fill_id="multi-fill-2",
            order_id=template.order_id,
            event_time=market.as_of,
            price=second_price,
            quantity=second_quantity,
            fee=Decimal("0.22"),
        ),
    )
    order = template.model_copy(update={"fills": fills})

    closed = PositionLifecycleManager(
        exchange=_ExitOrderExchange(order),
        reconciler=MockReconciler(),
        risk_budget=cycle.risk_budget,
    ).force_exit(
        lifecycle=lifecycle,
        market=market,
        account=opened.account_after,
        reason=ExitReason.MAX_HOLDING_TIME,
        pipeline_version=app_config.pipeline.version,
    )

    expected_gross = (first_price - lifecycle.entry_price) * first_quantity + (
        second_price - lifecycle.entry_price
    ) * second_quantity
    expected_exit_price = (
        first_price * first_quantity + second_price * second_quantity
    ) / lifecycle.quantity
    assert closed.outcome is not None
    assert closed.outcome.exit_price == expected_exit_price
    assert closed.outcome.gross_pnl == expected_gross
    assert closed.outcome.total_fees == lifecycle.entry_fee + Decimal("0.33")
    assert closed.outcome.net_pnl == expected_gross - closed.outcome.total_fees
    assert closed.account.positions == ()
    assert cycle.risk_budget.status(lifecycle.reservation_id) == "RELEASED"


def test_incomplete_exit_never_closes_or_releases_risk_budget(app_config, replay_input) -> None:
    cycle = AnalysisCycle.create(app_config)
    opened = cycle.run(replay_input)
    lifecycle = opened.position_lifecycle
    assert lifecycle is not None and opened.account_after is not None
    market = _later_market(replay_input, last=lifecycle.stop_price, minutes=10)
    template = cycle.exchange.submit_position_exit(
        lifecycle=lifecycle,
        market=market,
        reason=ExitReason.STOP_LOSS,
    )
    partial_fill = template.fills[0].model_copy(
        update={"quantity": lifecycle.quantity / Decimal("2")}
    )
    partial_order = template.model_copy(
        update={"status": OrderStatus.PARTIALLY_FILLED, "fills": (partial_fill,)}
    )

    manager = PositionLifecycleManager(
        exchange=_ExitOrderExchange(partial_order),
        reconciler=MockReconciler(),
        risk_budget=cycle.risk_budget,
    )
    with pytest.raises(ValueError, match="未完全成交"):
        manager.force_exit(
            lifecycle=lifecycle,
            market=market,
            account=opened.account_after,
            reason=ExitReason.STOP_LOSS,
            pipeline_version=app_config.pipeline.version,
        )

    assert cycle.risk_budget.status(lifecycle.reservation_id) == "CONSUMED"
