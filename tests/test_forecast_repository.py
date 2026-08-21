from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine

from investment_manager.forecast.models import (
    BaseForecast,
    CalibratedForecast,
    DirectionalView,
    ExposureDirection,
    ForecastKind,
    ForecastLegOutcome,
    ForecastOutcome,
    ForecastOutcomeStatus,
    ForecastReferencePrice,
    ForecastRole,
    ForecastTarget,
)
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.kernel.identity import stable_id
from investment_manager.market.models import InstrumentId
from investment_manager.schema import create_schema

NOW = datetime(2026, 8, 21, 3, tzinfo=UTC)


def _target() -> ForecastTarget:
    return ForecastTarget.single_long(
        InstrumentId.binance_spot(
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
        )
    )


def _base(*, forecast_id: str = "base-1") -> BaseForecast:
    target = _target()
    return BaseForecast(
        forecast_id=forecast_id,
        producer_id="trend",
        producer_version="trend-v1",
        forecast_family="TREND",
        target=target,
        horizon_minutes=60,
        direction=DirectionalView.UP,
        reference_prices=(
            ForecastReferencePrice(
                instrument_id=target.legs[0].instrument.key,
                price=Decimal("100"),
            ),
        ),
        observed_at=NOW,
        available_at=NOW + timedelta(seconds=10),
        valid_until=NOW + timedelta(hours=1),
        raw_score=Decimal("1.2"),
        input_refs=("feature-1",),
    )


def _calibrated(base: BaseForecast) -> CalibratedForecast:
    return CalibratedForecast(
        forecast_id="calibrated-1",
        role=ForecastRole.PROGRAM_BASE,
        producer_id=base.producer_id,
        producer_version=base.producer_version,
        forecast_family=base.forecast_family,
        target=base.target,
        horizon_minutes=base.horizon_minutes,
        direction=base.direction,
        reference_prices=base.reference_prices,
        expected_edge_half_life_seconds=1800,
        available_at=base.available_at,
        valid_until=base.valid_until,
        base_forecast_id=base.forecast_id,
        expected_gross_bps=Decimal("8"),
        conservative_gross_bps=Decimal("2"),
        dispersion_bps=Decimal("12"),
        calibration_ref="calibration-1",
        calibration_sample_size=40,
        non_overlapping_sample_size=20,
        input_refs=(base.forecast_id, "calibration-1"),
    )


def _outcome(forecast: BaseForecast) -> ForecastOutcome:
    evaluation_version = "forecast-outcome-v1"
    price_return = Decimal("100")
    return ForecastOutcome(
        outcome_id=stable_id(
            "forecast_outcome",
            forecast.forecast_id,
            evaluation_version,
        ),
        forecast_id=forecast.forecast_id,
        forecast_kind=ForecastKind.BASE,
        producer_id=forecast.producer_id,
        producer_version=forecast.producer_version,
        target_id=forecast.target.target_id,
        direction=forecast.direction,
        horizon_minutes=forecast.horizon_minutes,
        evaluation_version=evaluation_version,
        status=ForecastOutcomeStatus.SETTLED,
        available_at=forecast.available_at,
        evaluation_at=forecast.available_at + timedelta(minutes=60),
        settled_at=forecast.available_at + timedelta(minutes=61),
        legs=(
            ForecastLegOutcome(
                instrument_id=forecast.target.legs[0].instrument.key,
                direction=ExposureDirection.LONG,
                gross_weight=Decimal("1"),
                reference_price=Decimal("100"),
                exit_price=Decimal("101"),
                price_return_bps=price_return,
            ),
        ),
        gross_target_return_bps=price_return,
        directional_return_bps=price_return,
        reason_code="TARGET_RETURN_AVAILABLE",
    )


@pytest.fixture
def store() -> SqlForecastStore:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    return SqlForecastStore(engine)


def test_forecast_ledger_is_immutable_and_dependency_checked(store) -> None:
    base = _base()
    calibrated = _calibrated(base)

    with pytest.raises(ValueError, match="已持久化 BaseForecast"):
        store.record(calibrated)
    assert store.record(base)
    assert not store.record(base)
    assert store.record(calibrated)
    assert store.forecast(base.forecast_id) == base
    assert store.forecast(calibrated.forecast_id) == calibrated
    with pytest.raises(ValueError, match="内容不同"):
        store.record(base.model_copy(update={"raw_score": Decimal("2")}))


def test_forecast_outcome_settles_once_and_removes_pending(store) -> None:
    base = _base()
    store.record(base)
    assert store.pending(evaluation_version="forecast-outcome-v1", limit=10) == (base,)
    assert (
        store.pending(
            evaluation_version="forecast-outcome-v1",
            limit=10,
            due_at=base.available_at + timedelta(minutes=59),
        )
        == ()
    )
    assert store.pending(
        evaluation_version="forecast-outcome-v1",
        limit=10,
        due_at=base.available_at + timedelta(minutes=60),
    ) == (base,)

    outcome = _outcome(base)
    assert store.record_outcome(outcome)
    assert not store.record_outcome(outcome)
    assert store.outcome(outcome.outcome_id) == outcome
    assert store.pending(evaluation_version="forecast-outcome-v1", limit=10) == ()
    assert store.outcomes(
        producer_id=base.producer_id,
        producer_version=base.producer_version,
        evaluation_version="forecast-outcome-v1",
    ) == (outcome,)


def test_forecast_outcome_cannot_change_authoritative_leg(store) -> None:
    base = _base()
    store.record(base)
    outcome = _outcome(base)
    altered = outcome.model_copy(
        update={"legs": (outcome.legs[0].model_copy(update={"instrument_id": "other"}),)}
    )
    with pytest.raises(ValueError, match="权威 Forecast Leg"):
        store.record_outcome(altered)
    altered_reference = outcome.model_copy(
        update={"legs": (outcome.legs[0].model_copy(update={"reference_price": Decimal("99")}),)}
    )
    with pytest.raises(ValueError, match="权威 Forecast Leg 事实"):
        store.record_outcome(altered_reference)
