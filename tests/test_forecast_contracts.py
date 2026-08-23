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
    ForecastNoEstimate,
    ForecastNoEstimateReason,
    ForecastOrientation,
    ForecastOutcomeBucket,
    ForecastPermission,
    ForecastPriceAnchor,
    ForecastProducerBinding,
    ForecastProducerKind,
)
from investment_manager.forecast.models import ForecastTarget
from investment_manager.kernel.identity import stable_id
from investment_manager.market.models import InstrumentId
from investment_manager.schema import create_schema

NOW = datetime(2026, 8, 23, 12, tzinfo=UTC)


def _contract() -> ForecastContract:
    buckets = (
        ForecastOutcomeBucket(
            bucket_id="LOSS",
            upper_bps=Decimal("-25"),
            representative_bps=Decimal("-60"),
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
            representative_bps=Decimal("60"),
        ),
    )
    return ForecastContract.create(
        contract_version="btc-spot-24h-v1",
        outcome_family_id="btc-spot-direction-24h",
        target=ForecastTarget.single_long(
            InstrumentId.binance_spot(
                symbol="BTCUSDT",
                base_asset="BTC",
                quote_asset="USDT",
            )
        ),
        allowed_orientations=(ForecastOrientation.CANONICAL,),
        outcome_buckets=buckets,
        horizon_minutes=1_440,
        decision_slot_rule="daily-and-material-event-v1",
        evaluation_trigger="daily-or-material-delta-v1",
        information_cutoff_rule="slot-as-of-v1",
        completion_deadline_seconds=300,
        minimum_remaining_horizon_minutes=1_200,
        entry_anchor_rule="first-executable-quote-after-completion-v1",
        cost_semantics_version="point-in-time-complete-cost-v1",
        validity_minutes=1_440,
        validity_conditions=("TARGET_MATERIAL_DELTA",),
        settlement_rule="cutoff-to-horizon-executable-v1",
        forecast_benchmark=tuple(
            ForecastBenchmarkProbability(bucket_id=item.bucket_id, probability=probability)
            for item, probability in zip(
                buckets,
                (Decimal("0.25"), Decimal("0.5"), Decimal("0.25")),
                strict=True,
            )
        ),
        decision_benchmark="cash-and-risk-matched-passive-v1",
    )


def test_contract_identity_is_source_independent_and_slot_is_stable() -> None:
    contract = _contract()
    slot = ForecastDecisionSlot.create(
        contract,
        slot_as_of=NOW,
        cutoff_prices=_cutoff_prices(contract),
    )
    assert slot.slot_id == ForecastDecisionSlot.identity_for(contract.contract_id, NOW)
    assert slot.information_cutoff_at == NOW
    assert slot.completion_deadline_at == NOW + timedelta(minutes=5)
    assert slot.evaluation_at == NOW + timedelta(days=1)

    program = ForecastProducerBinding(
        binding_id=stable_id(
            "forecast_producer_binding",
            contract.contract_id,
            ForecastProducerKind.PROGRAM.value,
            "trend",
            "trend-v1",
            ForecastPermission.RESEARCH.value,
            (),
            None,
        ),
        contract_id=contract.contract_id,
        producer_kind=ForecastProducerKind.PROGRAM,
        producer_id="trend",
        producer_behavior_id="trend-v1",
        permission=ForecastPermission.RESEARCH,
    )
    context = ForecastProducerBinding(
        binding_id=stable_id(
            "forecast_producer_binding",
            contract.contract_id,
            ForecastProducerKind.CONTEXT.value,
            "codex",
            "codex-v1",
            ForecastPermission.RESEARCH.value,
            (),
            3_600,
        ),
        contract_id=contract.contract_id,
        producer_kind=ForecastProducerKind.CONTEXT,
        producer_id="codex",
        producer_behavior_id="codex-v1",
        permission=ForecastPermission.RESEARCH,
        maximum_world_model_age_seconds=3_600,
    )
    assert program.contract_id == context.contract_id == contract.contract_id


def test_contract_rejects_probability_gap_and_untradeable_deadline() -> None:
    contract = _contract()
    payload = contract.model_dump(mode="python")
    payload["contract_id"] = "invalid"
    payload["outcome_buckets"][1]["lower_bps"] = Decimal("-20")
    with pytest.raises(ValueError, match="连续"):
        ForecastContract.model_validate(payload)

    values = contract.model_dump(mode="python", exclude={"contract_id"})
    values["completion_deadline_seconds"] = 20_000
    with pytest.raises(ValueError, match="最小可交易时长"):
        ForecastContract.create(**values)


def test_no_estimate_has_one_identity_per_slot_and_behavior() -> None:
    contract = _contract()
    slot = ForecastDecisionSlot.create(
        contract,
        slot_as_of=NOW,
        cutoff_prices=_cutoff_prices(contract),
    )
    result = ForecastNoEstimate(
        result_id=stable_id("forecast_no_estimate", slot.slot_id, "codex-v1"),
        slot_id=slot.slot_id,
        contract_id=contract.contract_id,
        producer_kind=ForecastProducerKind.CONTEXT,
        producer_id="codex",
        producer_behavior_id="codex-v1",
        reason=ForecastNoEstimateReason.DEADLINE_MISSED,
        information_cutoff_at=NOW,
        attempted_at=NOW + timedelta(seconds=1),
        completed_at=NOW + timedelta(minutes=6),
        input_refs=("world-model-1",),
    )
    assert result.result_id == stable_id(
        "forecast_no_estimate",
        slot.slot_id,
        "codex-v1",
    )


def test_contract_slot_binding_and_absence_ledger_is_immutable() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    store = SqlForecastContractStore(engine)
    contract = _contract()
    slot = ForecastDecisionSlot.create(
        contract,
        slot_as_of=NOW,
        cutoff_prices=_cutoff_prices(contract),
    )
    binding = ForecastProducerBinding(
        binding_id=stable_id(
            "forecast_producer_binding",
            contract.contract_id,
            ForecastProducerKind.PROGRAM.value,
            "trend",
            "trend-v1",
            ForecastPermission.RESEARCH.value,
            (),
            None,
        ),
        contract_id=contract.contract_id,
        producer_kind=ForecastProducerKind.PROGRAM,
        producer_id="trend",
        producer_behavior_id="trend-v1",
        permission=ForecastPermission.RESEARCH,
    )
    absence = ForecastNoEstimate(
        result_id=stable_id("forecast_no_estimate", slot.slot_id, "trend-v1"),
        slot_id=slot.slot_id,
        contract_id=contract.contract_id,
        producer_kind=ForecastProducerKind.PROGRAM,
        producer_id="trend",
        producer_behavior_id="trend-v1",
        reason=ForecastNoEstimateReason.REQUIRED_FEATURE_MISSING,
        information_cutoff_at=NOW,
        attempted_at=NOW,
        completed_at=NOW,
        input_refs=(),
    )

    with pytest.raises(ValueError, match="ForecastContract"):
        store.record_slot(slot)
    assert store.record_contract(contract)
    assert not store.record_contract(contract)
    assert store.record_binding(binding)
    assert store.record_slot(slot)
    assert store.record_no_estimate(absence)
    assert not store.record_no_estimate(absence)
    assert store.contract(contract.contract_id) == contract
    assert store.binding(binding.binding_id) == binding
    assert store.slot(slot.slot_id) == slot
    assert store.no_estimate(absence.result_id) == absence


def _cutoff_prices(contract: ForecastContract) -> tuple[ForecastPriceAnchor, ...]:
    return tuple(
        ForecastPriceAnchor(
            instrument_id=leg.instrument.key,
            price=Decimal("100"),
            observed_at=NOW,
            available_at=NOW,
            quote_ref=f"cutoff:{leg.instrument.key}",
        )
        for leg in contract.target.legs
    )
