from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, func, select

from investment_manager.decision_cycle.capital import assemble_capital_cycle
from investment_manager.decision_cycle.portfolio import TradePlanExecutionResult
from investment_manager.entrypoints.dashboard.capital import (
    CapitalDashboardReader,
    serialize_capital_overview,
)
from investment_manager.execution.tables import mock_product_orders, trade_plans
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.market.models import InstrumentProduct, MarketQuote
from investment_manager.market.perpetual.models import PerpetualMarketState, PerpetualQuote
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.portfolio.repository import SqlPortfolioStore
from investment_manager.portfolio.tables import (
    portfolio_performance_intervals,
    portfolio_rebalance_periods,
    portfolio_targets,
)
from investment_manager.risk.tables import portfolio_risk_decisions
from investment_manager.schema import create_schema
from investment_manager.settings import load_config

NOW = datetime(2026, 9, 1, 0, 5, tzinfo=UTC)


def _put_market(
    market: SqlMarketDataStore,
    config,
    *,
    at: datetime,
    sequence: int,
    spot_bid: str = "99990",
    spot_ask: str = "100000",
    perpetual_bid: str = "100300",
    perpetual_ask: str = "100310",
) -> None:
    market.put_quote(
        MarketQuote(
            quote_id=f"spot-capital-quote-{sequence}",
            symbol="BTCUSDT",
            observed_at=at,
            bid=Decimal(spot_bid),
            bid_quantity=Decimal("2"),
            ask=Decimal(spot_ask),
            ask_quantity=Decimal("2"),
            source="test",
        )
    )
    perpetual = next(
        item.instrument
        for item in config.capital.execution_specs
        if item.instrument.product == InstrumentProduct.USD_M_PERPETUAL
    )
    market.put_perpetual_quote(
        PerpetualQuote(
            quote_id=stable_id("perpetual_quote", perpetual.key, sequence),
            instrument=perpetual,
            exchange_time=at,
            observed_at=at,
            bid=Decimal(perpetual_bid),
            bid_quantity=Decimal("2"),
            ask=Decimal(perpetual_ask),
            ask_quantity=Decimal("2"),
            update_id=sequence,
            source="test",
        )
    )
    market.put_perpetual_state(
        PerpetualMarketState(
            state_id=stable_id(
                "perpetual_market_state",
                perpetual.key,
                at.isoformat(),
            ),
            instrument=perpetual,
            exchange_time=at,
            observed_at=at,
            mark_price=Decimal(perpetual_bid),
            index_price=Decimal(spot_ask),
            last_funding_rate=Decimal("0.0001"),
            interest_rate=Decimal("0.0001"),
            next_funding_time=at + timedelta(hours=4),
            source="test",
        )
    )


def test_capital_cycle_turns_monthly_released_carry_into_idempotent_mock_trade() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=NOW, sequence=7)
    service = assemble_capital_cycle(config, engine)

    first = service.produce(as_of=NOW)
    replay = service.produce(as_of=NOW)

    assert isinstance(first, TradePlanExecutionResult)
    assert replay == first
    assert len(first.groups) == 1
    assert first.groups[0].terminal
    assert first.groups[0].valid_until == NOW.replace(minute=30)
    assert first.account.equity < Decimal("10000")
    assert first.account.equity == Decimal("9996.91685")
    assert first.account.revision == 1
    assert content_hash(first.account) == content_hash(
        first.account.model_copy(update={"revision": 0})
    )
    assert {abs(item.quantity) for item in first.account.positions} == {Decimal("0.014")}
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(mock_product_orders)) == 2
        assert (
            connection.scalar(
                select(func.count()).select_from(portfolio_performance_intervals)
            )
            == 1
        )

    overview = CapitalDashboardReader(engine, config).overview(now=NOW)
    assert (
        SqlPortfolioStore(engine).latest_account(
            portfolio_id=config.capital.decision.portfolio_id,
            as_of=NOW,
        )
        == first.account
    )
    dto = serialize_capital_overview(overview)
    assert dto["account"]["equity"] == "9996.91685"
    assert dto["decision"]["risk_outcome"] == "APPROVED"
    assert dto["decision"]["plan_group_count"] == 1
    assert dto["execution"] == {
        "active_group_count": 0,
        "active_groups": [],
        "total_order_count": 2,
    }
    assert dto["performance"]["interval_count"] == 1
    assert dto["performance"]["cumulative_net_pnl"] == "-3.08315"
    assert dto["performance"]["latest"]["kind"] == "EXECUTION"
    assert dto["performance"]["latest"]["net_pnl"] == "-3.08315"
    assert dto["forecast"]["base_count"] == 1
    assert dto["forecast"]["calibrated_count"] == 1


def test_capital_cycle_freezes_one_monthly_decision_and_holds_after_missed_window() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=NOW, sequence=1)
    service = assemble_capital_cycle(config, engine)

    opened = service.produce(as_of=NOW)
    assert isinstance(opened, TradePlanExecutionResult)

    heartbeat = NOW + timedelta(minutes=15)
    _put_market(
        market,
        config,
        at=heartbeat,
        sequence=2,
        spot_bid="105000",
        spot_ask="105010",
        perpetual_bid="105310",
        perpetual_ask="105320",
    )
    held = service.produce(as_of=heartbeat)

    missed = datetime(2026, 10, 1, 0, 31, tzinfo=UTC)
    _put_market(
        market,
        config,
        at=missed,
        sequence=3,
        spot_bid="103000",
        spot_ask="103010",
        perpetual_bid="103280",
        perpetual_ask="103290",
    )
    after_restart = assemble_capital_cycle(config, engine).produce(as_of=missed)

    assert held.outcome.value == "NO_CHANGE"
    assert after_restart.outcome.value == "NO_CHANGE"
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(portfolio_targets)) == 1
        assert (
            connection.scalar(select(func.count()).select_from(mock_product_orders))
            == 2
        )
        assert (
            connection.scalar(
                select(func.count()).select_from(portfolio_rebalance_periods)
            )
            == 2
        )


def test_risk_forced_cash_is_not_retried_later_in_the_month() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    loaded = load_config("config/investment-manager.shadow.yaml")
    config = loaded.model_copy(
        update={
            "capital": loaded.capital.model_copy(
                update={
                    "risk": loaded.capital.risk.model_copy(
                        update={"kill_switch": True}
                    )
                }
            )
        }
    )
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=NOW, sequence=10)
    service = assemble_capital_cycle(config, engine)

    forced_cash = service.produce(as_of=NOW)
    later = NOW + timedelta(minutes=15)
    _put_market(market, config, at=later, sequence=11)
    held = service.produce(as_of=later)

    assert forced_cash.outcome.value == "PLANNED"
    assert forced_cash.trade_plan is not None
    assert forced_cash.trade_plan.groups == ()
    assert held.outcome.value == "NO_CHANGE"
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(portfolio_targets)) == 1
        assert (
            connection.scalar(
                select(func.count()).select_from(portfolio_risk_decisions)
            )
            == 1
        )
        assert connection.scalar(select(func.count()).select_from(trade_plans)) == 1
        assert (
            connection.scalar(select(func.count()).select_from(mock_product_orders))
            == 0
        )
