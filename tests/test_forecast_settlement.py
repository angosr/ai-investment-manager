from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine

from investment_manager.forecast.models import (
    BaseForecast,
    DirectionalView,
    ExposureDirection,
    ForecastLeg,
    ForecastOutcomeStatus,
    ForecastReferencePrice,
    ForecastTarget,
)
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.forecast.settlement import ForecastOutcomeSettler
from investment_manager.kernel.identity import stable_id
from investment_manager.market.models import (
    InstrumentId,
    InstrumentProduct,
    MarketQuote,
)
from investment_manager.market.perpetual.models import (
    FundingRateType,
    FundingSettlement,
    PerpetualQuote,
)
from investment_manager.market.repository import InMemoryMarketDataStore
from investment_manager.schema import create_schema

NOW = datetime(2026, 8, 21, 3, tzinfo=UTC)


def _instruments() -> tuple[InstrumentId, InstrumentId]:
    spot = InstrumentId.binance_spot(
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
    )
    perpetual = InstrumentId(
        product=InstrumentProduct.USD_M_PERPETUAL,
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        settlement_asset="USDT",
    )
    return spot, perpetual


def _forecast() -> BaseForecast:
    spot, perpetual = _instruments()
    target = ForecastTarget.create(
        (
            ForecastLeg(
                instrument=spot,
                direction=ExposureDirection.LONG,
                gross_weight=Decimal("0.5"),
            ),
            ForecastLeg(
                instrument=perpetual,
                direction=ExposureDirection.SHORT,
                gross_weight=Decimal("0.5"),
            ),
        )
    )
    return BaseForecast(
        forecast_id="carry-base-1",
        producer_id="btc-carry",
        producer_version="btc-carry-v1",
        forecast_family="DELTA_NEUTRAL_FUNDING_CARRY",
        target=target,
        horizon_minutes=60,
        direction=DirectionalView.UP,
        reference_prices=(
            ForecastReferencePrice(
                instrument_id=spot.key,
                price=Decimal("100"),
            ),
            ForecastReferencePrice(
                instrument_id=perpetual.key,
                price=Decimal("101"),
            ),
        ),
        observed_at=NOW,
        available_at=NOW,
        valid_until=NOW + timedelta(minutes=30),
        raw_score=Decimal("5"),
        input_refs=("spot-entry-quote", "perpetual-entry-quote", "state-1"),
    )


def _spot_quote(*, quote_id: str, at: datetime, bid: str, ask: str) -> MarketQuote:
    return MarketQuote(
        quote_id=quote_id,
        symbol="BTCUSDT",
        observed_at=at,
        bid=bid,
        bid_quantity="1",
        ask=ask,
        ask_quantity="1",
        source="test",
    )


def _perpetual_quote(
    *,
    at: datetime,
    update_id: int,
    bid: str,
    ask: str,
) -> PerpetualQuote:
    instrument = _instruments()[1]
    return PerpetualQuote(
        quote_id=stable_id("perpetual_quote", instrument.key, update_id),
        instrument=instrument,
        exchange_time=at,
        observed_at=at,
        bid=bid,
        bid_quantity="1",
        ask=ask,
        ask_quantity="1",
        update_id=update_id,
        source="test",
    )


def test_multi_leg_forecast_settles_executable_basis_and_funding() -> None:
    forecast = _forecast()
    market = InMemoryMarketDataStore()
    market.put_quote(_spot_quote(quote_id="spot-entry", at=NOW, bid="99.9", ask="100"))
    market.put_perpetual_quote(_perpetual_quote(at=NOW, update_id=1, bid="101", ask="101.1"))
    evaluation_at = NOW + timedelta(minutes=60)
    market.put_quote(
        _spot_quote(
            quote_id="spot-exit",
            at=evaluation_at,
            bid="102",
            ask="102.1",
        )
    )
    market.put_perpetual_quote(
        _perpetual_quote(
            at=evaluation_at,
            update_id=2,
            bid="100.4",
            ask="100.5",
        )
    )
    perpetual = _instruments()[1]
    funding_time = NOW + timedelta(minutes=30)
    market.put_funding_settlement(
        FundingSettlement(
            settlement_id=stable_id(
                "funding_settlement",
                perpetual.key,
                funding_time.isoformat(),
                FundingRateType.REGULAR.value,
            ),
            instrument=perpetual,
            funding_time=funding_time,
            observed_at=funding_time + timedelta(seconds=1),
            funding_rate=Decimal("0.0001"),
            mark_price=Decimal("100.8"),
            rate_type=FundingRateType.REGULAR,
            source="test",
        )
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    store = SqlForecastStore(engine)
    store.record(forecast)
    settler = ForecastOutcomeSettler(
        market=market,
        store=store,
        evaluation_version="forecast-target-outcome-v1",
        maximum_spot_age_seconds=60,
        maximum_perpetual_age_seconds=900,
        maximum_funding_gap_hours=12,
        settlement_grace_minutes=5,
    )

    result = settler.settle(as_of=evaluation_at + timedelta(seconds=2))
    outcome = store.outcomes(
        producer_id=forecast.producer_id,
        producer_version=forecast.producer_version,
        evaluation_version="forecast-target-outcome-v1",
    )[0]

    assert result.settled == 1
    assert outcome.status == ForecastOutcomeStatus.SETTLED
    assert outcome.legs[0].exit_price == Decimal("102")
    assert outcome.legs[1].exit_price == Decimal("100.5")
    assert outcome.legs[1].funding_return_bps > 0
    assert outcome.gross_target_return_bps == sum(
        (item.price_return_bps + item.funding_return_bps for item in outcome.legs),
        Decimal("0"),
    )


def test_forecast_waits_for_market_facts_then_becomes_unscorable() -> None:
    forecast = _forecast()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    store = SqlForecastStore(engine)
    store.record(forecast)
    settler = ForecastOutcomeSettler(
        market=InMemoryMarketDataStore(),
        store=store,
        evaluation_version="forecast-target-outcome-v1",
        maximum_spot_age_seconds=60,
        maximum_perpetual_age_seconds=900,
        maximum_funding_gap_hours=12,
        settlement_grace_minutes=5,
    )
    evaluation_at = NOW + timedelta(minutes=60)

    pending = settler.settle(as_of=evaluation_at + timedelta(minutes=4))
    unscorable = settler.settle(as_of=evaluation_at + timedelta(minutes=5))
    outcome = store.outcomes(
        producer_id=forecast.producer_id,
        producer_version=forecast.producer_version,
        evaluation_version="forecast-target-outcome-v1",
    )[0]

    assert pending.pending == 1
    assert unscorable.unscorable == 1
    assert outcome.status == ForecastOutcomeStatus.UNSCORABLE
