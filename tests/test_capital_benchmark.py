from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, func, select

from investment_manager.entrypoints.dashboard.capital import CapitalDashboardReader
from investment_manager.governance.evaluation.capital_benchmark import (
    CapitalBenchmarkPolicy,
    SqlCapitalBenchmarkEvaluator,
    build_capital_benchmark_policy,
)
from investment_manager.governance.tables import capital_benchmark_points
from investment_manager.market.models import InstrumentId, MarketQuote
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.portfolio.models import PortfolioAccountSnapshot
from investment_manager.portfolio.repository import SqlPortfolioStore
from investment_manager.schema import create_schema
from investment_manager.settings import load_config

AT = datetime(2026, 8, 24, 12, tzinfo=UTC)


def _account(*, revision: int, equity: str, at: datetime) -> PortfolioAccountSnapshot:
    amount = Decimal(equity)
    return PortfolioAccountSnapshot(
        snapshot_id=f"account-{revision}",
        cycle_id=f"cycle-{revision}",
        portfolio_id="primary",
        revision=revision,
        as_of=at,
        observed_at=at,
        settlement_asset="USDT",
        cash_balance=amount,
        equity=amount,
        equity_high_water=max(Decimal("10020"), amount),
        daily_pnl=amount - Decimal("10000"),
        drawdown_fraction=Decimal("0"),
    )


def _quote(*, quote_id: str, at: datetime, bid: str, ask: str) -> MarketQuote:
    return MarketQuote(
        quote_id=quote_id,
        symbol="BTCUSDT",
        observed_at=at,
        bid=Decimal(bid),
        bid_quantity=Decimal("100"),
        ask=Decimal(ask),
        ask_quantity=Decimal("100"),
        source="test",
    )


def _policy(*, fee_bps: str = "12.5") -> CapitalBenchmarkPolicy:
    return CapitalBenchmarkPolicy.create(
        portfolio_id="primary",
        instrument=InstrumentId.binance_spot(
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
        ),
        allocation_fraction=Decimal("0.10"),
        quantity_step=Decimal("0.001"),
        minimum_order_notional=Decimal("10"),
        fee_bps=Decimal(fee_bps),
    )


def test_capital_benchmark_is_point_in_time_immutable_and_restart_safe() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    portfolio = SqlPortfolioStore(engine)
    market = SqlMarketDataStore(engine)
    t1 = AT + timedelta(hours=1)

    portfolio.record_account(_account(revision=0, equity="10000", at=AT))
    portfolio.record_account(_account(revision=1, equity="10020", at=t1))
    market.put_quote(_quote(quote_id="entry", at=AT, bid="99", ask="100"))
    market.put_quote(_quote(quote_id="mark", at=t1, bid="110", ask="111"))
    market.put_quote(
        _quote(
            quote_id="not-yet-visible",
            at=t1 + timedelta(seconds=1),
            bid="200",
            ask="201",
        )
    )

    config = load_config("config/investment-manager.shadow.yaml")
    policy = build_capital_benchmark_policy(config)
    assert policy is not None
    evaluator = SqlCapitalBenchmarkEvaluator(engine, policy)
    assert evaluator.reconcile(as_of=t1 + timedelta(hours=1)) == 2
    assert evaluator.reconcile(as_of=t1 + timedelta(hours=1)) == 0

    initial = evaluator.for_account("account-0")
    latest = evaluator.latest()
    assert initial is not None and latest is not None
    assert initial.passive_quantity == Decimal("10")
    assert initial.passive_entry_fee == Decimal("1.25")
    assert initial.passive_equity == Decimal("9988.75")
    assert initial.passive_equity_high_water == Decimal("10000")
    assert latest.revision == 1
    assert latest.mark_quote_id == "mark"
    assert latest.cash_equity == Decimal("10000")
    assert latest.passive_equity == Decimal("10098.75")
    assert latest.actual_increment_vs_cash == Decimal("20")
    assert latest.actual_increment_vs_passive == Decimal("-78.75")

    restarted = SqlCapitalBenchmarkEvaluator(engine, policy)
    assert restarted.reconcile(as_of=t1 + timedelta(hours=1)) == 0
    dashboard_points = CapitalDashboardReader(engine, config).equity_history()
    dashboard_latest = next(item for item in dashboard_points if item.revision == 1)
    assert dashboard_latest.cash_benchmark_equity == Decimal("10000")
    assert dashboard_latest.passive_benchmark_equity == Decimal("10098.75")
    assert dashboard_latest.increment_vs_passive == Decimal("-78.75")
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(capital_benchmark_points)) == 2


def test_capital_benchmark_policy_change_never_rewrites_prior_series() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    SqlPortfolioStore(engine).record_account(
        _account(revision=0, equity="10000", at=AT)
    )
    SqlMarketDataStore(engine).put_quote(
        _quote(quote_id="entry", at=AT, bid="99", ask="100")
    )

    old = SqlCapitalBenchmarkEvaluator(engine, _policy(fee_bps="12.5"))
    changed = SqlCapitalBenchmarkEvaluator(engine, _policy(fee_bps="20"))
    assert old.reconcile(as_of=AT) == 1
    assert changed.reconcile(as_of=AT) == 1
    assert old.latest() is not None
    assert changed.latest() is not None
    assert old.latest().point_id != changed.latest().point_id
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(capital_benchmark_points)) == 2
