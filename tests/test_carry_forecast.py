from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine

from investment_manager.forecast.carry import CarryForecastProducer
from investment_manager.forecast.models import DirectionalView
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.kernel.identity import stable_id
from investment_manager.market.models import InstrumentId, InstrumentProduct, MarketQuote
from investment_manager.market.perpetual.models import PerpetualMarketState, PerpetualQuote
from investment_manager.market.repository import InMemoryMarketDataStore
from investment_manager.schema import create_schema

NOW = datetime(2026, 8, 21, 3, 20, tzinfo=UTC)


def _perpetual() -> InstrumentId:
    return InstrumentId(
        product=InstrumentProduct.USD_M_PERPETUAL,
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        settlement_asset="USDT",
    )


def test_carry_producer_creates_one_point_in_time_daily_shadow_forecast(
    app_config,
) -> None:
    market = InMemoryMarketDataStore()
    market.put_quote(
        MarketQuote(
            quote_id="spot-quote-1",
            symbol="BTCUSDT",
            observed_at=NOW,
            bid=Decimal("99990"),
            bid_quantity=Decimal("1"),
            ask=Decimal("100000"),
            ask_quantity=Decimal("1"),
            source="test",
        )
    )
    perpetual = _perpetual()
    market.put_perpetual_quote(
        PerpetualQuote(
            quote_id=stable_id("perpetual_quote", perpetual.key, 1),
            instrument=perpetual,
            exchange_time=NOW,
            observed_at=NOW,
            bid=Decimal("100300"),
            bid_quantity=Decimal("1"),
            ask=Decimal("100310"),
            ask_quantity=Decimal("1"),
            update_id=1,
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
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    store = SqlForecastStore(engine)
    producer = CarryForecastProducer(
        policy=app_config.carry_forecast,
        market=market,
        store=store,
        maximum_spot_age_seconds=60,
        maximum_perpetual_age_seconds=900,
        clock=lambda: NOW,
    )

    first = producer.produce(as_of=NOW)
    replay = producer.produce(as_of=NOW + timedelta(minutes=1))

    assert first is not None
    assert replay == first
    assert first.direction == DirectionalView.UP
    assert first.raw_score > 0
    assert tuple(item.price for item in first.reference_prices) == (
        Decimal("100000"),
        Decimal("100300"),
    )
    assert store.pending(
        evaluation_version="forecast-target-outcome-v1",
        limit=10,
    ) == (first,)


def test_disabled_carry_producer_does_not_write(app_config) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    store = SqlForecastStore(engine)
    producer = CarryForecastProducer(
        policy=app_config.carry_forecast.model_copy(update={"enabled": False}),
        market=InMemoryMarketDataStore(),
        store=store,
        maximum_spot_age_seconds=60,
        maximum_perpetual_age_seconds=900,
        clock=lambda: NOW,
    )

    assert producer.produce(as_of=NOW) is None
    assert (
        store.pending(
            evaluation_version="forecast-target-outcome-v1",
            limit=10,
        )
        == ()
    )
