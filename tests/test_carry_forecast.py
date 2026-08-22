from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from investment_manager.forecast.carry import (
    CarryForecastProducer,
    ReleasedCarryForecastProducer,
    validate_carry_evidence,
)
from investment_manager.forecast.models import DirectionalView, ForecastRole
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.kernel.identity import stable_id
from investment_manager.market.models import InstrumentId, InstrumentProduct, MarketQuote
from investment_manager.market.perpetual.models import (
    PerpetualMarketState,
    PerpetualQuote,
)
from investment_manager.market.repository import InMemoryMarketDataStore
from investment_manager.schema import create_schema

NOW = datetime(2026, 9, 1, 0, 5, tzinfo=UTC)


def _perpetual() -> InstrumentId:
    return InstrumentId(
        product=InstrumentProduct.USD_M_PERPETUAL,
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        settlement_asset="USDT",
    )


def _market(
    as_of: datetime,
    *,
    spot_observed_at: datetime | None = None,
) -> InMemoryMarketDataStore:
    market = InMemoryMarketDataStore()
    spot_observed_at = spot_observed_at or as_of
    market.put_quote(
        MarketQuote(
            quote_id=stable_id("spot_quote", spot_observed_at),
            symbol="BTCUSDT",
            observed_at=spot_observed_at,
            bid=Decimal("99990"),
            bid_quantity=Decimal("1"),
            ask=Decimal("100000"),
            ask_quantity=Decimal("1"),
            source="test",
        )
    )
    perpetual = _perpetual()
    update_id = int(as_of.timestamp())
    market.put_perpetual_quote(
        PerpetualQuote(
            quote_id=stable_id("perpetual_quote", perpetual.key, update_id),
            instrument=perpetual,
            exchange_time=as_of,
            observed_at=as_of,
            bid=Decimal("100300"),
            bid_quantity=Decimal("1"),
            ask=Decimal("100310"),
            ask_quantity=Decimal("1"),
            update_id=update_id,
            source="test",
        )
    )
    market.put_perpetual_state(
        PerpetualMarketState(
            state_id=stable_id(
                "perpetual_market_state",
                perpetual.key,
                as_of.isoformat(),
            ),
            instrument=perpetual,
            exchange_time=as_of,
            observed_at=as_of,
            mark_price=Decimal("100300"),
            index_price=Decimal("100000"),
            last_funding_rate=Decimal("0.0001"),
            interest_rate=Decimal("0.0001"),
            next_funding_time=as_of + timedelta(hours=8),
            source="test",
        )
    )
    return market


def test_carry_evidence_rejects_tampered_source_artifact(
    app_config,
    tmp_path,
) -> None:
    evidence = app_config.carry_forecast.evidence
    assert evidence is not None
    source = Path(evidence.source_artifact_path)
    copied = tmp_path / source.name
    copied.write_bytes(source.read_bytes() + b" ")
    copied_evidence = evidence.model_copy(
        update={"source_artifact_path": Path(source.name)}
    )

    with pytest.raises(ValueError, match="文件哈希不匹配"):
        validate_carry_evidence(copied_evidence, repository_root=tmp_path)


def test_carry_evidence_rejects_copied_metric_drift(app_config) -> None:
    evidence = app_config.carry_forecast.evidence
    assert evidence is not None
    changed = evidence.model_copy(
        update={
            "expected_annualized_net_fraction": (
                evidence.expected_annualized_net_fraction + Decimal("0.001")
            )
        }
    )

    with pytest.raises(ValueError, match="配置与源评价事实不一致"):
        validate_carry_evidence(changed)


def test_carry_producer_creates_one_point_in_time_monthly_shadow_forecast(
    app_config,
) -> None:
    market = _market(NOW)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    store = SqlForecastStore(engine)
    producer = CarryForecastProducer(
        policy=app_config.carry_forecast,
        market=market,
        store=store,
        maximum_spot_age_seconds=60,
        maximum_perpetual_age_seconds=900,
        maximum_quote_skew_seconds=15,
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

    assert app_config.carry_forecast.evidence is not None
    released = ReleasedCarryForecastProducer(
        base=producer,
        evidence=app_config.carry_forecast.evidence,
        store=store,
    ).produce(as_of=NOW + timedelta(minutes=1))
    assert released is not None
    assert released.role == ForecastRole.PROGRAM_BASE
    assert released.base_forecast_id == first.forecast_id
    assert released.conservative_gross_bps > Decimal("20")
    evidence = app_config.carry_forecast.evidence
    horizon_fraction = Decimal(first.horizon_minutes) / Decimal("525960")
    expected_conservative_net_bps = (
        evidence.conservative_annualized_net_fraction
        * horizon_fraction
        * Decimal("10000")
        / evidence.evaluated_gross_exposure_fraction
    )
    assert released.conservative_gross_bps == (
        expected_conservative_net_bps + evidence.round_trip_cost_bps
    )
    assert released.calibration_ref == (
        evidence.source_evaluation_id
    )


def test_carry_producer_does_not_enter_late_in_month(app_config) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    producer = CarryForecastProducer(
        policy=app_config.carry_forecast,
        market=InMemoryMarketDataStore(),
        store=SqlForecastStore(engine),
        maximum_spot_age_seconds=60,
        maximum_perpetual_age_seconds=900,
        maximum_quote_skew_seconds=15,
        clock=lambda: datetime(2026, 8, 21, tzinfo=UTC),
    )

    assert producer.produce(as_of=datetime(2026, 8, 21, tzinfo=UTC)) is None


@pytest.mark.parametrize(
    ("as_of", "expected_horizon_minutes"),
    (
        (datetime(2027, 2, 1, tzinfo=UTC), 28 * 24 * 60),
        (datetime(2027, 3, 1, tzinfo=UTC), 31 * 24 * 60),
    ),
)
def test_carry_forecast_horizon_matches_the_exact_calendar_month(
    app_config,
    as_of,
    expected_horizon_minutes,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    forecast = CarryForecastProducer(
        policy=app_config.carry_forecast,
        market=_market(as_of),
        store=SqlForecastStore(engine),
        maximum_spot_age_seconds=60,
        maximum_perpetual_age_seconds=900,
        maximum_quote_skew_seconds=15,
        clock=lambda: as_of,
    ).produce(as_of=as_of)

    assert forecast is not None
    assert forecast.horizon_minutes == expected_horizon_minutes
    assert forecast.valid_until == as_of + timedelta(minutes=30)
    assert forecast.economic_horizon_end == as_of + timedelta(
        minutes=expected_horizon_minutes
    )


def test_carry_producer_does_not_use_an_on_time_request_processed_late(
    app_config,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    producer = CarryForecastProducer(
        policy=app_config.carry_forecast,
        market=InMemoryMarketDataStore(),
        store=SqlForecastStore(engine),
        maximum_spot_age_seconds=60,
        maximum_perpetual_age_seconds=900,
        maximum_quote_skew_seconds=15,
        clock=lambda: NOW + timedelta(minutes=31),
    )

    assert producer.produce(as_of=NOW) is None


def test_persisted_monthly_forecast_cannot_authorize_late_catch_up(
    app_config,
) -> None:
    market = InMemoryMarketDataStore()
    market.put_quote(
        MarketQuote(
            quote_id="spot-quote-late-guard",
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
            quote_id=stable_id("perpetual_quote", perpetual.key, 2),
            instrument=perpetual,
            exchange_time=NOW,
            observed_at=NOW,
            bid=Decimal("100300"),
            bid_quantity=Decimal("1"),
            ask=Decimal("100310"),
            ask_quantity=Decimal("1"),
            update_id=2,
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
    clock = [NOW]
    producer = CarryForecastProducer(
        policy=app_config.carry_forecast,
        market=market,
        store=SqlForecastStore(engine),
        maximum_spot_age_seconds=60,
        maximum_perpetual_age_seconds=900,
        maximum_quote_skew_seconds=15,
        clock=lambda: clock[0],
    )

    assert producer.produce(as_of=NOW) is not None
    clock[0] = NOW.replace(minute=31)
    assert producer.produce(as_of=clock[0]) is None


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
        maximum_quote_skew_seconds=15,
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
