from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, func, select

from investment_manager.decision_cycle.capital import assemble_capital_cycle
from investment_manager.decision_cycle.portfolio import TradePlanExecutionResult
from investment_manager.execution.tables import mock_product_orders
from investment_manager.kernel.identity import stable_id
from investment_manager.market.models import InstrumentProduct, MarketQuote
from investment_manager.market.perpetual.models import PerpetualMarketState, PerpetualQuote
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.schema import create_schema
from investment_manager.settings import load_config

NOW = datetime(2026, 9, 1, 0, 5, tzinfo=UTC)


def test_capital_cycle_turns_monthly_released_carry_into_idempotent_mock_trade() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    market.put_quote(
        MarketQuote(
            quote_id="spot-capital-quote",
            symbol="BTCUSDT",
            observed_at=NOW,
            bid=Decimal("99990"),
            bid_quantity=Decimal("2"),
            ask=Decimal("100000"),
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
            quote_id=stable_id("perpetual_quote", perpetual.key, 7),
            instrument=perpetual,
            exchange_time=NOW,
            observed_at=NOW,
            bid=Decimal("100300"),
            bid_quantity=Decimal("2"),
            ask=Decimal("100310"),
            ask_quantity=Decimal("2"),
            update_id=7,
            source="test",
        )
    )
    market.put_perpetual_state(
        PerpetualMarketState(
            state_id=stable_id(
                "perpetual_market_state",
                perpetual.key,
                NOW.isoformat(),
            ),
            instrument=perpetual,
            exchange_time=NOW,
            observed_at=NOW,
            mark_price=Decimal("100300"),
            index_price=Decimal("100000"),
            last_funding_rate=Decimal("0.0001"),
            interest_rate=Decimal("0.0001"),
            next_funding_time=NOW + timedelta(hours=4),
            source="test",
        )
    )
    service = assemble_capital_cycle(config, engine)

    first = service.produce(as_of=NOW)
    replay = service.produce(as_of=NOW)

    assert isinstance(first, TradePlanExecutionResult)
    assert replay == first
    assert len(first.groups) == 1
    assert first.groups[0].terminal
    assert first.account.equity < Decimal("10000")
    assert first.account.equity == Decimal("9996.91685")
    assert {abs(item.quantity) for item in first.account.positions} == {Decimal("0.014")}
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(mock_product_orders)) == 2
