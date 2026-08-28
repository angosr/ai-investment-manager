from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from investment_manager.research.carry import CarryFundingSettlement, CarryMarketBar
from investment_manager.research.slow_trend import (
    _Signal,
    _simulate,
    _weekly_signals,
    load_slow_trend_plan,
    store_slow_trend_result,
)


def test_frozen_slow_trend_plan_has_one_untuned_rule() -> None:
    plan = load_slow_trend_plan(Path("config/research/btc-slow-trend-12w-v1.yaml"))

    assert plan.formation_days == 84
    assert plan.cost_bps_per_executed_notional == Decimal("7.5")
    assert plan.regimes[0][0] == datetime(2020, 3, 30, tzinfo=UTC)
    assert plan.regimes[-1][1] == plan.source_end


def test_weekly_signal_holds_position_without_weekly_reentry() -> None:
    bars = _rising_daily_bars(140)
    signals = _weekly_signals(bars, formation_days=84)

    assert signals
    assert {item.direction for item in signals} == {1}
    simulation = _simulate(
        bars=bars,
        settlements=(),
        signals=signals,
        start=signals[0].at,
        end=bars[-1].close_time + timedelta(milliseconds=1),
        cost_bps=Decimal("7.5"),
        always_long=False,
    )

    assert simulation.trade_count == 2
    assert simulation.ending_equity > 1
    assert simulation.execution_cost > 0
    assert len(simulation.period_pnl) == len(signals)


def test_funding_is_charged_to_position_held_at_settlement() -> None:
    start = datetime(2026, 1, 5, tzinfo=UTC)
    bar = _bar(start, Decimal("100"), Decimal("100"))
    settlement = CarryFundingSettlement(
        symbol="BTCUSDT",
        funding_time=start + timedelta(hours=8),
        available_at=start + timedelta(hours=8, minutes=1),
        funding_interval_hours=8,
        funding_rate=Decimal("0.01"),
        mark_price=Decimal("100"),
    )

    simulation = _simulate(
        bars=(bar,),
        settlements=(settlement,),
        signals=(_Signal(at=start, direction=1, formation_return=Decimal("0.1")),),
        start=start,
        end=start + timedelta(days=1),
        cost_bps=Decimal(0),
        always_long=False,
    )

    assert simulation.funding_pnl == Decimal("-0.01")
    assert simulation.ending_equity == Decimal("0.99")


def test_liquidation_is_a_terminal_result_not_an_evaluator_error() -> None:
    start = datetime(2026, 1, 5, tzinfo=UTC)
    bar = _bar(start, Decimal("100"), Decimal("100"))
    settlement = CarryFundingSettlement(
        symbol="BTCUSDT",
        funding_time=start + timedelta(hours=8),
        available_at=start + timedelta(hours=8, minutes=1),
        funding_interval_hours=8,
        funding_rate=Decimal("2"),
        mark_price=Decimal("100"),
    )

    simulation = _simulate(
        bars=(bar,),
        settlements=(settlement,),
        signals=(_Signal(at=start, direction=1, formation_return=Decimal("0.1")),),
        start=start,
        end=start + timedelta(days=1),
        cost_bps=Decimal(0),
        always_long=False,
    )

    assert simulation.liquidated is True
    assert simulation.ending_equity == 0
    assert simulation.period_pnl == (Decimal("-1"),)


def test_result_store_refuses_rewriting_history(tmp_path: Path) -> None:
    target = tmp_path / "result.json"
    store_slow_trend_result({"status": "REJECTED"}, target)
    assert store_slow_trend_result({"status": "REJECTED"}, target) == target

    with pytest.raises(ValueError, match="内容不一致"):
        store_slow_trend_result({"status": "PASSED"}, target)


def _rising_daily_bars(count: int) -> tuple[CarryMarketBar, ...]:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    bars = []
    price = Decimal("100")
    for index in range(count):
        close = price * Decimal("1.001")
        bars.append(_bar(start + timedelta(days=index), price, close))
        price = close
    return tuple(bars)


def _bar(open_time: datetime, open_price: Decimal, close_price: Decimal) -> CarryMarketBar:
    high = max(open_price, close_price)
    low = min(open_price, close_price)
    return CarryMarketBar(
        symbol="BTCUSDT",
        open_time=open_time,
        close_time=open_time + timedelta(days=1) - timedelta(milliseconds=1),
        contract_open=open_price,
        contract_high=high,
        contract_low=low,
        contract_close=close_price,
        mark_open=open_price,
        mark_high=high,
        mark_low=low,
        mark_close=close_price,
        index_open=open_price,
        index_high=high,
        index_low=low,
        index_close=close_price,
        premium_open=Decimal(0),
        premium_high=Decimal(0),
        premium_low=Decimal(0),
        premium_close=Decimal(0),
    )
