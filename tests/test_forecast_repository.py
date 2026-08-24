from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine

from investment_manager.forecast.contract_repository import SqlForecastContractStore
from investment_manager.forecast.contracts import (
    ForecastBenchmarkProbability,
    ForecastContract,
    ForecastDecisionSlot,
    ForecastOutcomeBucket,
    ForecastPermission,
    ForecastPriceAnchor,
    ForecastProducerBinding,
    ForecastProducerKind,
)
from investment_manager.forecast.models import ForecastTarget
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.forecast.results import (
    BaseForecast,
    CalibratedForecast,
    ForecastBucketProbability,
    ForecastLegOutcome,
    ForecastOutcome,
    ForecastOutcomeStatus,
)
from investment_manager.kernel.identity import stable_id
from investment_manager.market.models import InstrumentId
from investment_manager.schema import create_schema

NOW = datetime(2026, 8, 21, 3, tzinfo=UTC)


def _contract() -> ForecastContract:
    buckets = (
        ForecastOutcomeBucket(
            bucket_id="LOSS",
            upper_bps=Decimal("-25"),
            representative_bps=Decimal("-100"),
        ),
        ForecastOutcomeBucket(
            bucket_id="FLAT",
            lower_bps=Decimal("-25"),
            upper_bps=Decimal("25"),
            representative_bps=Decimal("0"),
        ),
        ForecastOutcomeBucket(
            bucket_id="GAIN",
            lower_bps=Decimal("25"),
            representative_bps=Decimal("100"),
        ),
    )
    return ForecastContract.create(
        contract_version="trend-v1",
        outcome_family_id="BTC_SPOT_1H",
        target=ForecastTarget.single_long(
            InstrumentId.binance_spot(
                symbol="BTCUSDT",
                base_asset="BTC",
                quote_asset="USDT",
            )
        ),
        outcome_buckets=buckets,
        horizon_minutes=60,
        decision_slot_rule="hourly-v1",
        evaluation_trigger="hourly-v1",
        information_cutoff_rule="slot-as-of-v1",
        completion_deadline_seconds=30,
        minimum_remaining_horizon_minutes=30,
        entry_anchor_rule="first-executable-v1",
        cost_semantics_version="complete-cost-v1",
        validity_minutes=30,
        validity_conditions=("TARGET_MATERIAL_DELTA",),
        settlement_rule="cutoff-executable-v1",
        forecast_benchmark=tuple(
            ForecastBenchmarkProbability(bucket_id=item.bucket_id, probability=probability)
            for item, probability in zip(
                buckets,
                (Decimal("0.25"), Decimal("0.5"), Decimal("0.25")),
                strict=True,
            )
        ),
        decision_benchmark="cash-and-passive-v1",
    )


def _base(contract: ForecastContract, slot: ForecastDecisionSlot) -> BaseForecast:
    instrument_id = contract.target.legs[0].instrument.key
    available_at = NOW + timedelta(seconds=10)
    return BaseForecast(
        forecast_id=stable_id("base_forecast", slot.slot_id, "trend-v1"),
        contract_id=contract.contract_id,
        decision_slot_id=slot.slot_id,
        producer_id="trend",
        producer_behavior_id="trend-v1",
        outcome_family_id=contract.outcome_family_id,
        target=contract.target,
        horizon_minutes=contract.horizon_minutes,
        cutoff_prices=(
            ForecastPriceAnchor(
                instrument_id=instrument_id,
                price=Decimal("100"),
                observed_at=NOW,
                available_at=NOW,
                quote_ref="cutoff-quote",
            ),
        ),
        entry_prices=(
            ForecastPriceAnchor(
                instrument_id=instrument_id,
                price=Decimal("100.1"),
                observed_at=available_at,
                available_at=available_at,
                quote_ref="entry-quote",
            ),
        ),
        information_cutoff_at=NOW,
        input_observed_at=NOW,
        available_at=available_at,
        valid_until=NOW + timedelta(minutes=30),
        outcome_probabilities=(
            ForecastBucketProbability(bucket_id="LOSS", probability=Decimal("0.2")),
            ForecastBucketProbability(bucket_id="FLAT", probability=Decimal("0.5")),
            ForecastBucketProbability(bucket_id="GAIN", probability=Decimal("0.3")),
        ),
        expected_gross_bps=Decimal("10"),
        input_refs=("feature-1",),
    )


def _calibrated(base: BaseForecast) -> CalibratedForecast:
    policy_id = "forecast-policy-v1"
    return CalibratedForecast(
        forecast_id=stable_id(
            "calibrated_forecast",
            base.decision_slot_id,
            policy_id,
            base.forecast_id,
        ),
        contract_id=base.contract_id,
        decision_slot_id=base.decision_slot_id,
        base_forecast_id=base.forecast_id,
        forecast_policy_id=policy_id,
        producer_id=base.producer_id,
        producer_behavior_id=base.producer_behavior_id,
        outcome_family_id=base.outcome_family_id,
        target=base.target,
        horizon_minutes=base.horizon_minutes,
        cutoff_prices=base.cutoff_prices,
        entry_prices=base.entry_prices,
        information_cutoff_at=base.information_cutoff_at,
        available_at=base.available_at,
        valid_until=base.valid_until,
        outcome_probabilities=base.outcome_probabilities,
        expected_gross_bps=base.expected_gross_bps,
        conservative_gross_bps=Decimal("2"),
        dispersion_bps=Decimal("80"),
        calibration_sample_size=40,
        non_overlapping_sample_size=20,
        input_refs=(base.forecast_id, "policy-v1"),
    )


@pytest.fixture
def ledger():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    contracts = SqlForecastContractStore(engine)
    forecasts = SqlForecastStore(engine)
    contract = _contract()
    slot = ForecastDecisionSlot.create(
        contract,
        slot_as_of=NOW,
        cutoff_prices=(
            ForecastPriceAnchor(
                instrument_id=contract.target.legs[0].instrument.key,
                price=Decimal("100"),
                observed_at=NOW,
                available_at=NOW,
                quote_ref="cutoff-quote",
            ),
        ),
    )
    contracts.record_contract(contract)
    binding = ForecastProducerBinding(
        binding_id=stable_id(
            "forecast_producer_binding",
            contract.contract_id,
            ForecastProducerKind.PROGRAM.value,
            "trend",
            "trend-v1",
            ForecastPermission.RESEARCH.value,
            (),
        ),
        contract_id=contract.contract_id,
        producer_kind=ForecastProducerKind.PROGRAM,
        producer_id="trend",
        producer_behavior_id="trend-v1",
        permission=ForecastPermission.RESEARCH,
    )
    contracts.record_binding(binding, activated_at=NOW)
    contracts.record_slot(slot, binding=binding)
    return forecasts, contract, slot


def test_forecast_ledger_validates_contract_slot_distribution_and_base_dependency(ledger) -> None:
    store, contract, slot = ledger
    base = _base(contract, slot)
    calibrated = _calibrated(base)

    with pytest.raises(ValueError, match="BaseForecast"):
        store.record(calibrated)
    assert store.record(base)
    assert not store.record(base)
    assert store.record(calibrated)
    assert store.forecast(base.forecast_id) == base
    assert store.forecast(calibrated.forecast_id) == calibrated
    altered = base.model_copy(update={"expected_gross_bps": Decimal("11")})
    with pytest.raises(ValueError, match="expected_gross_bps"):
        store.record(altered)


def test_slot_outcome_settles_once_for_every_producer(ledger) -> None:
    store, contract, slot = ledger
    base = _base(contract, slot)
    store.record(base)
    assert store.pending_slots(evaluation_version="outcome-v1", limit=10) == (
        (contract, slot),
    )
    outcome = ForecastOutcome(
        outcome_id=stable_id("forecast_outcome", slot.slot_id, "outcome-v1"),
        contract_id=contract.contract_id,
        decision_slot_id=slot.slot_id,
        evaluation_version="outcome-v1",
        status=ForecastOutcomeStatus.SETTLED,
        information_cutoff_at=slot.information_cutoff_at,
        evaluation_at=slot.evaluation_at,
        settled_at=slot.evaluation_at + timedelta(seconds=1),
        legs=(
            ForecastLegOutcome(
                instrument_id=contract.target.legs[0].instrument.key,
                direction=contract.target.legs[0].direction,
                gross_weight=Decimal("1"),
                reference_price=Decimal("100"),
                exit_price=Decimal("101"),
                price_return_bps=Decimal("100"),
            ),
        ),
        gross_target_return_bps=Decimal("100"),
        realized_bucket_id="GAIN",
        reason_code="GROSS_TARGET_RETURN_AVAILABLE",
    )
    assert store.record_outcome(outcome)
    assert not store.record_outcome(outcome)
    assert store.pending_slots(evaluation_version="outcome-v1", limit=10) == ()
    assert store.outcomes(
        contract_id=contract.contract_id,
        evaluation_version="outcome-v1",
    ) == (outcome,)
