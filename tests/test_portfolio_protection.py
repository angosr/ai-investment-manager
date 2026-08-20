from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine

from investment_manager.domain import AccountSnapshot, Position
from investment_manager.risk.protection import SqlPortfolioProtectionStore
from investment_manager.schema import create_schema

NOW = datetime(2026, 8, 18, 17, tzinfo=UTC)


def _account(*, quote: str, daily_pnl: str = "0", as_of: datetime = NOW):
    return AccountSnapshot(
        cycle_id=f"account-{as_of.isoformat()}-{quote}",
        as_of=as_of,
        observed_at=as_of,
        quote_balance=Decimal(quote),
        daily_pnl=Decimal(daily_pnl),
        reconciled=True,
    )


def _store(app_config):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    return SqlPortfolioProtectionStore(
        engine,
        policy=app_config.risk,
        initial_equity=Decimal("10000"),
    )


def test_drawdown_trips_persistent_kill_switch_until_manual_reset(app_config) -> None:
    store = _store(app_config)
    baseline = store.observe(_account(quote="10000"), marks={}, as_of=NOW)
    breached = store.observe(
        _account(quote="9400", as_of=NOW + timedelta(minutes=1)),
        marks={},
        as_of=NOW + timedelta(minutes=1),
    )
    recovered_next_day = store.observe(
        _account(quote="10000", as_of=NOW + timedelta(days=1)),
        marks={},
        as_of=NOW + timedelta(days=1),
    )

    assert baseline.drawdown_fraction == 0
    assert breached.drawdown_fraction == Decimal("0.06")
    assert breached.kill_switch_active is True
    assert recovered_next_day.kill_switch_active is True
    assert store.current().trip_reason == "DRAWDOWN_LIMIT_EXCEEDED"

    reset = store.reset(reset_at=NOW + timedelta(days=1, minutes=1), reason="人工复核完成")
    assert reset.kill_switch_active is False
    assert reset.high_water_equity == reset.last_equity == Decimal("10000")
    assert reset.last_reset_reason == "人工复核完成"


def test_daily_loss_trips_even_without_drawdown_breach(app_config) -> None:
    store = _store(app_config)
    protected = store.observe(
        _account(quote="10000", daily_pnl="-201"),
        marks={},
        as_of=NOW,
    )

    assert protected.kill_switch_active is True
    assert store.current().trip_reason == "DAILY_LOSS_LIMIT_EXCEEDED"


def test_equity_marks_positions_instead_of_treating_invested_cash_as_loss(app_config) -> None:
    store = _store(app_config)
    account = _account(quote="0").model_copy(
        update={
            "positions": (
                Position(
                    symbol="BTCUSDT",
                    quantity=Decimal("1"),
                    average_price=Decimal("10000"),
                ),
            )
        }
    )

    baseline = store.observe(account, marks={"BTCUSDT": Decimal("10000")}, as_of=NOW)
    breached = store.observe(
        account.model_copy(update={"as_of": NOW + timedelta(minutes=1)}),
        marks={"BTCUSDT": Decimal("9400")},
        as_of=NOW + timedelta(minutes=1),
    )

    assert baseline.equity == Decimal("10000")
    assert baseline.drawdown_fraction == 0
    assert breached.equity == Decimal("9400")
    assert breached.kill_switch_active is True
