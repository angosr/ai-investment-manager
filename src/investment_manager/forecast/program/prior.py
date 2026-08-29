"""Transparent 72-hour prior producer on the shared Forecast ledger."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from investment_manager.forecast.contract_repository import SqlForecastContractStore
from investment_manager.forecast.contracts import (
    MATERIAL_SLOT_POLICY_VERSION,
    ForecastBenchmarkProbability,
    ForecastContract,
    ForecastDecisionSlot,
    ForecastNoEstimate,
    ForecastNoEstimateReason,
    ForecastOutcomeBucket,
    ForecastPermission,
    ForecastPriceAnchor,
    ForecastProducerBinding,
    ForecastProducerKind,
    ForecastSlotCause,
    ForecastSlotOrigin,
)
from investment_manager.forecast.models import ForecastTarget
from investment_manager.forecast.program.baseline import (
    ForecastBaselineArtifact,
    ForecastBaselineTargetResult,
    probabilities_from_counts,
)
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.forecast.results import (
    BaseForecast,
    ForecastBucketProbability,
    ForecastOutcomeStatus,
)
from investment_manager.kernel.identity import canonical_json, content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.market.models import InstrumentId, MarketQuote
from investment_manager.market.repository import MarketDataStore

PRIOR_PRODUCER_ID = "rolling-unconditional-prior"
PRIOR_CONTRACT_VERSION = "spot-midpoint-72h-v1"
PRIOR_BEHAVIOR_VERSION = "rolling-visible-outcomes-v2"
_HORIZON_MINUTES = 4_320
_CADENCE_DAYS = 3
_PHASE_EPOCH = date(1970, 1, 1)


@dataclass(frozen=True, slots=True)
class PriorRuntimeTarget:
    baseline: ForecastBaselineTargetResult
    instrument: InstrumentId
    contract: ForecastContract
    binding: ForecastProducerBinding


def build_prior_targets(artifact: ForecastBaselineArtifact) -> tuple[PriorRuntimeTarget, ...]:
    prepared: list[tuple[ForecastBaselineTargetResult, InstrumentId, ForecastContract]] = []
    for baseline in artifact.results:
        base_asset = baseline.symbol.removesuffix("USDT")
        instrument = InstrumentId.binance_spot(
            symbol=baseline.symbol,
            base_asset=base_asset,
            quote_asset="USDT",
        )
        buckets = tuple(
            ForecastOutcomeBucket(
                bucket_id=bucket_id,
                lower_bps=None if index == 0 else baseline.bucket_boundaries_bps[index - 1],
                upper_bps=(
                    None
                    if index == len(baseline.bucket_ids) - 1
                    else baseline.bucket_boundaries_bps[index]
                ),
                representative_bps=baseline.representative_bps[index],
            )
            for index, bucket_id in enumerate(baseline.bucket_ids)
        )
        contract = ForecastContract.create(
            contract_version=PRIOR_CONTRACT_VERSION,
            outcome_family_id=f"{baseline.symbol.lower()}-spot-midpoint-return-72h-v1",
            target=ForecastTarget.single_long(instrument),
            outcome_buckets=buckets,
            horizon_minutes=_HORIZON_MINUTES,
            decision_slot_rule="non-overlapping-72h-utc-epoch-phase-v1",
            evaluation_trigger="absolute-utc-cadence-v1",
            information_cutoff_rule="slot-boundary-v1",
            completion_deadline_seconds=3_600,
            minimum_remaining_horizon_minutes=4_200,
            entry_anchor_rule="first-visible-midpoint-after-completion-v1",
            cost_semantics_version="direction-only-no-cost-v1",
            validity_minutes=4_260,
            validity_conditions=("SPOT_MIDPOINT_OUTCOME_AVAILABLE",),
            settlement_rule="spot-midpoint-return-v1",
            forecast_benchmark=tuple(
                ForecastBenchmarkProbability(bucket_id=item.bucket_id, probability=probability)
                for item, probability in zip(
                    buckets,
                    baseline.fixed_probabilities,
                    strict=True,
                )
            ),
            decision_benchmark="research-only-no-capital-decision-v1",
        )
        prepared.append((baseline, instrument, contract))
    behavior_id = content_hash(
        {
            "version": PRIOR_BEHAVIOR_VERSION,
            "material_slot_policy": MATERIAL_SLOT_POLICY_VERSION,
            "artifact_id": artifact.artifact_id,
            "joint_targets": tuple(
                {
                    "contract": contract,
                    "seed_counts": baseline.terminal_bucket_counts,
                }
                for baseline, _instrument, contract in prepared
            ),
        }
    )
    return tuple(
        PriorRuntimeTarget(
            baseline=baseline,
            instrument=instrument,
            contract=contract,
            binding=ForecastProducerBinding.create(
                contract_id=contract.contract_id,
                producer_kind=ForecastProducerKind.PROGRAM,
                producer_id=PRIOR_PRODUCER_ID,
                producer_behavior_id=behavior_id,
                permission=ForecastPermission.RESEARCH,
            ),
        )
        for baseline, instrument, contract in prepared
    )


@dataclass(frozen=True, slots=True)
class RollingPriorForecastProducer:
    artifact: ForecastBaselineArtifact
    market: MarketDataStore
    contracts: SqlForecastContractStore
    forecasts: SqlForecastStore
    outcome_evaluation_version: str
    activated_at: datetime
    maximum_quote_age_seconds: int
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def __post_init__(self) -> None:
        require_utc(self.activated_at)
        if self.maximum_quote_age_seconds < 1:
            raise ValueError("先验 producer 行情年龄必须为正数")

    def produce(
        self,
        *,
        as_of: datetime,
        cause: ForecastSlotCause | None = None,
    ) -> tuple[BaseForecast | ForecastNoEstimate, ...]:
        observed_at = require_utc(as_of)
        slot_at = (
            prior_slot_at_or_before(observed_at)
            if cause is None or cause.origin == ForecastSlotOrigin.CADENCE
            else observed_at
        )
        if slot_at < self.activated_at:
            return ()
        return tuple(
            self._produce(
                target,
                slot_at=slot_at,
                observed_at=observed_at,
                cause=cause or ForecastSlotCause.cadence(target.contract),
            )
            for target in build_prior_targets(self.artifact)
        )

    def _produce(
        self,
        target: PriorRuntimeTarget,
        *,
        slot_at: datetime,
        observed_at: datetime,
        cause: ForecastSlotCause,
    ) -> BaseForecast | ForecastNoEstimate:
        self.contracts.record_contract(target.contract)
        self.contracts.resolve_binding(target.binding, activated_at=self.activated_at)
        slot_id = ForecastDecisionSlot.identity_for(
            target.contract.contract_id, slot_at, cause=cause
        )
        existing = self.forecasts.result_for_behavior(
            decision_slot_id=slot_id,
            producer_behavior_id=target.binding.producer_behavior_id,
        )
        if existing is not None:
            return existing
        absence_id = stable_id("forecast_no_estimate", slot_id, target.binding.producer_behavior_id)
        absence = self.contracts.no_estimate(absence_id)
        if absence is not None:
            return absence
        quote = self.market.latest_spot_quote(
            instrument=target.instrument,
            evaluation_at=slot_at,
            visible_at=observed_at,
        )
        cutoff_anchors = self._anchors(target.instrument, quote, available_at=None)
        slot = ForecastDecisionSlot.create(
            target.contract,
            slot_as_of=slot_at,
            information_cutoff_at=slot_at,
            cutoff_prices=cutoff_anchors,
            cause=cause,
        )
        self.contracts.record_slot(slot, binding=target.binding)
        completed_at = max(require_utc(self.clock()), observed_at)
        if completed_at > slot.completion_deadline_at:
            return self._no_estimate(target, slot, completed_at, "PRIOR_DEADLINE_MISSED")
        if quote is None or slot_at - quote.observed_at > timedelta(
            seconds=self.maximum_quote_age_seconds
        ):
            return self._no_estimate(
                target, slot, completed_at, "PRIOR_CUTOFF_QUOTE_MISSING_OR_STALE"
            )
        entry_quote = self.market.latest_spot_quote(
            instrument=target.instrument,
            evaluation_at=completed_at,
            visible_at=completed_at,
        )
        if entry_quote is None or completed_at - entry_quote.observed_at > timedelta(
            seconds=self.maximum_quote_age_seconds
        ):
            return self._no_estimate(
                target, slot, completed_at, "PRIOR_ENTRY_QUOTE_MISSING_OR_STALE"
            )
        counts = list(target.baseline.terminal_bucket_counts)
        outcome_refs = []
        for outcome in self.forecasts.outcomes(
            contract_id=target.contract.contract_id,
            evaluation_version=self.outcome_evaluation_version,
        ):
            if outcome.status != ForecastOutcomeStatus.SETTLED or outcome.settled_at > slot_at:
                continue
            counts[target.baseline.bucket_ids.index(outcome.realized_bucket_id)] += 1
            outcome_refs.append(outcome.outcome_id)
        probabilities = probabilities_from_counts(tuple(counts))
        distribution = tuple(
            ForecastBucketProbability(bucket_id=bucket_id, probability=probability)
            for bucket_id, probability in zip(
                target.baseline.bucket_ids, probabilities, strict=True
            )
        )
        program_input = {
            "version": PRIOR_BEHAVIOR_VERSION,
            "artifact_id": self.artifact.artifact_id,
            "slot_id": slot.slot_id,
            "visible_counts": counts,
            "visible_outcome_refs": sorted(outcome_refs),
        }
        forecast = BaseForecast(
            forecast_id=stable_id(
                "base_forecast", slot.slot_id, target.binding.producer_behavior_id
            ),
            contract_id=target.contract.contract_id,
            decision_slot_id=slot.slot_id,
            producer_id=target.binding.producer_id,
            producer_behavior_id=target.binding.producer_behavior_id,
            outcome_family_id=target.contract.outcome_family_id,
            target=target.contract.target,
            horizon_minutes=target.contract.horizon_minutes,
            cutoff_prices=slot.cutoff_prices,
            entry_prices=self._anchors(target.instrument, entry_quote, available_at=completed_at),
            information_cutoff_at=slot.information_cutoff_at,
            input_observed_at=slot.information_cutoff_at,
            available_at=completed_at,
            valid_until=slot.evaluation_at,
            outcome_probabilities=distribution,
            expected_gross_bps=sum(
                (
                    probability.probability * bucket.representative_bps
                    for probability, bucket in zip(
                        distribution, target.contract.outcome_buckets, strict=True
                    )
                ),
                Decimal("0"),
            ),
            input_refs=tuple(
                sorted(
                    {
                        self.artifact.artifact_id,
                        *(item.quote_ref for item in slot.cutoff_prices),
                        *outcome_refs,
                    }
                )
            ),
            program_input_json=canonical_json(program_input),
            program_input_hash=content_hash(program_input),
        )
        self.forecasts.record(forecast)
        return forecast

    def _no_estimate(
        self,
        target: PriorRuntimeTarget,
        slot: ForecastDecisionSlot,
        completed_at: datetime,
        detail: str,
    ) -> ForecastNoEstimate:
        result = ForecastNoEstimate(
            result_id=stable_id(
                "forecast_no_estimate", slot.slot_id, target.binding.producer_behavior_id
            ),
            slot_id=slot.slot_id,
            contract_id=slot.contract_id,
            producer_kind=target.binding.producer_kind,
            producer_id=target.binding.producer_id,
            producer_behavior_id=target.binding.producer_behavior_id,
            reason=ForecastNoEstimateReason.DEADLINE_MISSED
            if detail == "PRIOR_DEADLINE_MISSED"
            else ForecastNoEstimateReason.MARKET_INPUT_INVALID,
            information_cutoff_at=slot.information_cutoff_at,
            attempted_at=slot.slot_as_of,
            completed_at=completed_at,
            input_refs=tuple(item.quote_ref for item in slot.cutoff_prices),
            detail=detail,
        )
        self.contracts.record_no_estimate(result)
        return result

    @staticmethod
    def _anchors(
        instrument: InstrumentId, quote: MarketQuote | None, *, available_at: datetime | None
    ) -> tuple[ForecastPriceAnchor, ...]:
        if quote is None:
            return ()
        return (
            ForecastPriceAnchor(
                instrument_id=instrument.key,
                price=(quote.bid + quote.ask) / 2,
                observed_at=quote.observed_at,
                available_at=available_at or quote.observed_at,
                quote_ref=quote.quote_id,
            ),
        )


def prior_slot_at_or_before(as_of: datetime) -> datetime:
    current = require_utc(as_of)
    days = (current.date() - _PHASE_EPOCH).days
    slot_days = days - ((days - 1) % _CADENCE_DAYS)
    slot_date = _PHASE_EPOCH + timedelta(days=slot_days)
    return datetime.combine(slot_date, time.min, tzinfo=UTC)
