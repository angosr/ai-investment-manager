from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from investment_manager.market.models import ClosedMarketBar
from investment_manager.research.carry import CarryFundingSettlement, CarryMarketBar
from investment_manager.research.perpetual_funding_carry import (
    _evaluate_window,
    load_perpetual_funding_carry_plan,
)

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "config" / "research" / "btc-perpetual-funding-carry-v1.yaml"
ENTRY = datetime(2020, 1, 31, tzinfo=UTC)
EXIT = ENTRY + timedelta(days=30)


def _spot(at: datetime, price: str = "100") -> ClosedMarketBar:
    value = Decimal(price)
    return ClosedMarketBar(
        symbol="BTCUSDT",
        interval="1d",
        open_time=at,
        close_time=at + timedelta(days=1) - timedelta(milliseconds=1),
        observed_at=at + timedelta(days=1) - timedelta(milliseconds=1),
        open=value,
        high=value,
        low=value,
        close=value,
        volume=Decimal("100"),
        source="fixture",
    )


def _carry(at: datetime, price: str = "100") -> CarryMarketBar:
    value = Decimal(price)
    return CarryMarketBar(
        symbol="BTCUSDT",
        open_time=at,
        close_time=at + timedelta(days=1) - timedelta(milliseconds=1),
        contract_open=value,
        contract_high=value,
        contract_low=value,
        contract_close=value,
        mark_open=value,
        mark_high=value,
        mark_low=value,
        mark_close=value,
        index_open=value,
        index_high=value,
        index_low=value,
        index_close=value,
        premium_open=Decimal("0"),
        premium_high=Decimal("0"),
        premium_low=Decimal("0"),
        premium_close=Decimal("0"),
    )


def _settlement(at: datetime, rate: str) -> CarryFundingSettlement:
    return CarryFundingSettlement(
        symbol="BTCUSDT",
        funding_time=at,
        available_at=at + timedelta(minutes=1),
        funding_interval_hours=8,
        funding_rate=Decimal(rate),
        mark_price=Decimal("100"),
    )


def _market():
    times = tuple(ENTRY + timedelta(days=index) for index in range(31))
    return (
        {at: _spot(at) for at in times},
        {at: _carry(at) for at in times},
    )


def test_market_neutral_window_keeps_positive_funding_after_complete_cost() -> None:
    plan = load_perpetual_funding_carry_plan(PLAN)
    spot, carry = _market()
    formation = tuple(
        _settlement(ENTRY - timedelta(days=30) + timedelta(hours=8 * index), "0.0001")
        for index in range(90)
    )
    holding = tuple(
        _settlement(ENTRY + timedelta(hours=8 * index), "0.0001")
        for index in range(1, 90)
    )

    outcome, path = _evaluate_window(
        plan=plan,
        partition="validation",
        entry_time=ENTRY,
        exit_time=EXIT,
        equity_before=Decimal("10000"),
        spot_by_time=spot,
        carry_by_time=carry,
        settlements=(*formation, *holding),
    )

    assert outcome.status == "ENTERED"
    assert outcome.quantity == Decimal("24.993")
    assert outcome.basis_and_price_pnl == 0
    assert outcome.funding_pnl > outcome.modeled_cost
    assert outcome.net_pnl > 0
    assert outcome.equity_after > outcome.equity_before
    assert path[-1] == outcome.equity_after


def test_window_does_not_trade_when_visible_funding_cannot_cover_cost() -> None:
    plan = load_perpetual_funding_carry_plan(PLAN)
    spot, carry = _market()
    formation = tuple(
        _settlement(ENTRY - timedelta(days=30) + timedelta(hours=8 * index), "0.00001")
        for index in range(90)
    )

    outcome, path = _evaluate_window(
        plan=plan,
        partition="validation",
        entry_time=ENTRY,
        exit_time=EXIT,
        equity_before=Decimal("10000"),
        spot_by_time=spot,
        carry_by_time=carry,
        settlements=formation,
    )

    assert outcome.status == "SKIPPED_COST"
    assert outcome.reason == "TRAILING_FUNDING_DID_NOT_EXCEED_FULL_COST"
    assert outcome.quantity == 0
    assert outcome.equity_after == Decimal("10000")
    assert path == (Decimal("10000"),)
