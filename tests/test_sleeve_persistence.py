from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine

from investment_manager.portfolio.models import (
    PortfolioAccountingTotals,
    PortfolioAccountSnapshot,
    PortfolioPerformanceAttribution,
    PortfolioPerformanceInterval,
    PortfolioPerformanceKind,
)
from investment_manager.portfolio.repository import (
    SqlPortfolioPerformanceStore,
    SqlPortfolioStore,
)
from investment_manager.schema import create_schema

NOW = datetime(2026, 8, 21, 5, tzinfo=UTC)


def test_portfolio_performance_records_same_time_execution_once() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    accounts = SqlPortfolioStore(engine)
    performance = SqlPortfolioPerformanceStore(engine)
    start = PortfolioAccountSnapshot(
        snapshot_id="account-before-execution",
        cycle_id="cycle-before-execution",
        portfolio_id="primary",
        revision=0,
        as_of=NOW,
        observed_at=NOW,
        settlement_asset="USDT",
        cash_balance=Decimal("10000"),
        equity=Decimal("10000"),
        equity_high_water=Decimal("10000"),
        accounting=PortfolioAccountingTotals(starting_equity=Decimal("10000")),
    )
    end = PortfolioAccountSnapshot(
        snapshot_id="account-after-execution",
        cycle_id="cycle-after-execution",
        portfolio_id="primary",
        revision=1,
        as_of=NOW,
        observed_at=NOW,
        settlement_asset="USDT",
        cash_balance=Decimal("9996.5"),
        equity=Decimal("9996.5"),
        equity_high_water=Decimal("10000"),
        daily_pnl=Decimal("-3.5"),
        drawdown_fraction=Decimal("0.00035"),
        accounting=PortfolioAccountingTotals(
            starting_equity=Decimal("10000"),
            price_pnl=Decimal("-2.5"),
            fee_cost=Decimal("1"),
            execution_slippage_cost=Decimal("0.25"),
            compensation_loss=Decimal("0.5"),
            net_pnl=Decimal("-3.5"),
        ),
    )

    assert accounts.record_account(start)
    assert performance.record(start) is None
    assert accounts.record_account(end)
    interval = performance.record(end)

    assert interval is not None
    assert interval.kind == PortfolioPerformanceKind.EXECUTION
    assert interval.net_pnl == Decimal("-3.5")
    assert interval.return_fraction == Decimal("-0.00035")
    assert interval.attribution is not None
    assert interval.attribution.price_pnl == Decimal("-2.5")
    assert interval.attribution.fee_cost == Decimal("1")
    assert interval.attribution.compensation_loss == Decimal("0.5")
    assert performance.record(end) == interval
    assert performance.count(portfolio_id="primary") == 1
    assert performance.latest(portfolio_id="primary") == interval

    later = end.model_copy(
        update={
            "snapshot_id": "account-next-mark",
            "cycle_id": "cycle-next-mark",
            "revision": 2,
            "as_of": NOW + timedelta(minutes=30),
            "observed_at": NOW + timedelta(minutes=30),
            "cash_balance": Decimal("9997"),
            "equity": Decimal("9997"),
            "daily_pnl": Decimal("-3"),
            "drawdown_fraction": Decimal("0.0003"),
            "accounting": PortfolioAccountingTotals(
                starting_equity=Decimal("10000"),
                price_pnl=Decimal("-2"),
                fee_cost=Decimal("1"),
                execution_slippage_cost=Decimal("0.20"),
                compensation_loss=Decimal("0.5"),
                net_pnl=Decimal("-3"),
            ),
        }
    )
    assert accounts.record_account(later)
    mark_interval = performance.record(later)
    assert mark_interval is not None
    assert mark_interval.kind == PortfolioPerformanceKind.MARK_TO_MARKET
    assert mark_interval.net_pnl == Decimal("0.5")
    assert mark_interval.attribution is not None
    assert mark_interval.attribution.price_pnl == Decimal("0.5")
    assert mark_interval.attribution.execution_slippage_cost == Decimal("-0.05")
    assert performance.count(portfolio_id="primary") == 2
    assert performance.latest(portfolio_id="primary") == mark_interval

    with pytest.raises(ValueError, match="权威账户事实"):
        performance.record(
            later.model_copy(
                update={"equity": Decimal("1"), "cash_balance": Decimal("1")}
            )
        )


def test_portfolio_performance_starts_attribution_after_legacy_snapshot() -> None:
    """Deploying attribution must not make the first post-upgrade interval unrecordable."""

    legacy = PortfolioAccountSnapshot(
        snapshot_id="account-before-attribution",
        cycle_id="cycle-before-attribution",
        portfolio_id="primary",
        revision=4,
        as_of=NOW,
        observed_at=NOW,
        settlement_asset="USDT",
        cash_balance=Decimal("10000"),
        equity=Decimal("10000"),
        equity_high_water=Decimal("10000"),
    )
    attributed = PortfolioAccountSnapshot(
        snapshot_id="account-with-attribution",
        cycle_id="cycle-with-attribution",
        portfolio_id="primary",
        revision=5,
        as_of=NOW + timedelta(minutes=1),
        observed_at=NOW + timedelta(minutes=1),
        settlement_asset="USDT",
        cash_balance=Decimal("9999"),
        equity=Decimal("9999"),
        equity_high_water=Decimal("10000"),
        daily_pnl=Decimal("-1"),
        drawdown_fraction=Decimal("0.0001"),
        accounting=PortfolioAccountingTotals(
            starting_equity=Decimal("10000"),
            price_pnl=Decimal("0"),
            fee_cost=Decimal("1"),
            net_pnl=Decimal("-1"),
        ),
    )

    interval = PortfolioPerformanceInterval.between(legacy, attributed)

    assert interval.net_pnl == Decimal("-1")
    assert interval.attribution is None


def test_compensation_attribution_allows_later_fill_price_improvement() -> None:
    """A better later fill may reduce cumulative compensation loss without corrupting PnL."""

    start = PortfolioAccountingTotals(
        starting_equity=Decimal("10000"),
        price_pnl=Decimal("-5"),
        compensation_loss=Decimal("5"),
        net_pnl=Decimal("-5"),
    )
    end = PortfolioAccountingTotals(
        starting_equity=Decimal("10000"),
        price_pnl=Decimal("0"),
        compensation_loss=Decimal("0"),
        net_pnl=Decimal("0"),
    )

    attribution = PortfolioPerformanceAttribution.between(start, end)

    assert attribution.net_pnl == Decimal("5")
    assert attribution.compensation_loss == Decimal("-5")


def test_portfolio_performance_repairs_a_crash_gap_before_appending() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    accounts = SqlPortfolioStore(engine)
    performance = SqlPortfolioPerformanceStore(engine)
    baseline = PortfolioAccountSnapshot(
        snapshot_id="account-r0",
        cycle_id="cycle-r0",
        portfolio_id="primary",
        revision=0,
        as_of=NOW,
        observed_at=NOW,
        settlement_asset="USDT",
        cash_balance=Decimal("10000"),
        equity=Decimal("10000"),
        equity_high_water=Decimal("10000"),
    )
    missed = baseline.model_copy(
        update={
            "snapshot_id": "account-r1",
            "cycle_id": "cycle-r1",
            "revision": 1,
            "as_of": NOW + timedelta(minutes=1),
            "observed_at": NOW + timedelta(minutes=1),
            "cash_balance": Decimal("9999"),
            "equity": Decimal("9999"),
            "daily_pnl": Decimal("-1"),
            "drawdown_fraction": Decimal("0.0001"),
        }
    )
    current = missed.model_copy(
        update={
            "snapshot_id": "account-r2",
            "cycle_id": "cycle-r2",
            "revision": 2,
            "as_of": NOW + timedelta(minutes=2),
            "observed_at": NOW + timedelta(minutes=2),
            "cash_balance": Decimal("10001"),
            "equity": Decimal("10001"),
            "equity_high_water": Decimal("10001"),
            "daily_pnl": Decimal("1"),
            "drawdown_fraction": Decimal("0"),
        }
    )

    assert accounts.record_account(baseline)
    assert accounts.record_account(missed)  # Simulate exit before interval persistence.
    assert accounts.record_account(current)
    latest = performance.record(current)

    assert latest is not None
    assert latest.start_snapshot_id == missed.snapshot_id
    assert latest.net_pnl == Decimal("2")
    assert performance.count(portfolio_id="primary") == current.revision
