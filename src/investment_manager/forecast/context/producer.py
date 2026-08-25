"""Persist a Context forecast or one explicit terminal absence for every slot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from investment_manager.forecast.codex.router import AnalystResult
from investment_manager.forecast.context.estimate import (
    ContextForecastStructuredOutput,
    ContextForecastTargetState,
    context_forecast_input_projection,
)
from investment_manager.forecast.context.repository import SqlContextAssessmentStore
from investment_manager.forecast.contract_repository import SqlForecastContractStore
from investment_manager.forecast.contracts import (
    ForecastContract,
    ForecastDecisionSlot,
    ForecastNoEstimate,
    ForecastNoEstimateReason,
    ForecastOrientation,
    ForecastPriceAnchor,
    ForecastProducerBinding,
    ForecastProducerKind,
)
from investment_manager.forecast.models import ContextAssessment, ForecastTarget
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.forecast.results import (
    BaseForecast,
    ForecastBucketProbability,
    ForecastMechanismContribution,
)
from investment_manager.kernel.errors import PointInTimeInputUnavailable
from investment_manager.kernel.identity import canonical_json, content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.market.features import FeatureEngine, build_derivative_context_snapshot
from investment_manager.market.models import InstrumentId, InstrumentProduct
from investment_manager.market.policy import FeaturePolicy
from investment_manager.market.repository import MarketDataStore
from investment_manager.portfolio.policy import ContextForecastPolicy
from investment_manager.state.decision.packet import (
    DecisionPacket,
    PacketAssetState,
    PacketDerivativeState,
)

ForecastProductionResult = BaseForecast | ForecastNoEstimate


class ContextProbabilityAnalyst(Protocol):
    def estimate(
        self,
        *,
        slot: ForecastDecisionSlot,
        assessment: ContextAssessment,
        packet: DecisionPacket,
        target_state: ContextForecastTargetState,
    ) -> AnalystResult: ...


class ContextTargetStateProvider(Protocol):
    def build(self, *, as_of: datetime) -> ContextForecastTargetState: ...


@dataclass(frozen=True, slots=True)
class MarketContextTargetStateProvider:
    """Rebuild the target-only market state at the Forecast slot, without AI."""

    market: MarketDataStore
    feature_policy: FeaturePolicy
    spot: InstrumentId
    perpetual: InstrumentId | None
    interval: str
    bar_window: int
    funding_lookback_hours: int
    maximum_quote_skew_seconds: int

    def build(self, *, as_of: datetime) -> ContextForecastTargetState:
        at = require_utc(as_of)
        cycle_id = stable_id("context_forecast_target_state", self.spot.key, at.isoformat())
        snapshot = self.market.snapshot(
            cycle_id=cycle_id,
            symbol=self.spot.symbol,
            interval=self.interval,
            as_of=at,
            bar_window=self.bar_window,
            source="point-in-time-market-ledger",
        )
        features = FeatureEngine(self.feature_policy).compute(snapshot)
        asset_state = PacketAssetState(
            asset=self.spot.base_asset,
            market_symbol=self.spot.symbol,
            observed_at=snapshot.observed_at,
            bid=snapshot.bid,
            ask=snapshot.ask,
            last=snapshot.last,
            return_fraction=features.return_fraction,
            realized_volatility=features.realized_volatility,
            atr=features.atr,
            spread_bps=features.spread_bps,
            volume_ratio=features.volume_ratio,
            regime=features.regime,
            market_age_seconds=features.market_age_seconds,
        )
        derivative_states: tuple[PacketDerivativeState, ...] = ()
        refs = {content_hash(snapshot), content_hash(features)}
        if self.perpetual is not None:
            state = self.market.latest_perpetual_state(instrument=self.perpetual, as_of=at)
            quote = self.market.latest_perpetual_quote(
                instrument=self.perpetual,
                evaluation_at=at,
                visible_at=at,
            )
            if (state is None) != (quote is None):
                raise PointInTimeInputUnavailable(
                    "Context Forecast 衍生品状态与报价必须同时可用"
                )
            if state is not None and quote is not None:
                aligned_spot = self.market.latest_spot_quote(
                    instrument=self.spot,
                    # Spot bookTicker has no exchange timestamp.  Align the two
                    # feeds on their local visibility clock; using the perpetual
                    # exchange timestamp can incorrectly skip a spot quote that
                    # was observed milliseconds before the perpetual response.
                    evaluation_at=quote.observed_at,
                    visible_at=at,
                )
                if aligned_spot is None:
                    raise PointInTimeInputUnavailable(
                        "Context Forecast 缺少与衍生品对齐的 Spot quote"
                    )
                derivative = build_derivative_context_snapshot(
                    cycle_id=cycle_id,
                    asset=self.spot.base_asset,
                    spot=snapshot,
                    aligned_spot_quote=aligned_spot,
                    state=state,
                    quote=quote,
                    settlements=self.market.funding_settlements(
                        instrument=self.perpetual,
                        start=at - timedelta(hours=self.funding_lookback_hours),
                        end=at,
                        visible_at=at,
                    ),
                    funding_window_hours=self.funding_lookback_hours,
                    maximum_quote_skew_seconds=self.maximum_quote_skew_seconds,
                )
                derivative_states = (self._derivative_state(derivative),)
                refs.update(derivative.input_refs)
                refs.add(content_hash(derivative))
        return ContextForecastTargetState(
            as_of=at,
            asset_states=(asset_state,),
            derivative_states=derivative_states,
            input_refs=tuple(sorted(refs)),
        )

    @staticmethod
    def _derivative_state(snapshot) -> PacketDerivativeState:
        payload = snapshot.model_dump(
            exclude={"cycle_id", "asset", "instrument", "as_of", "input_refs"}
        )
        return PacketDerivativeState(
            evidence_ref=content_hash(snapshot),
            asset=snapshot.asset,
            market_symbol=snapshot.instrument.symbol,
            **payload,
        )


def context_spot_forecast_contract(
    *,
    policy: ContextForecastPolicy,
    instrument: InstrumentId,
    cost_semantics_version: str,
) -> ForecastContract:
    if instrument.product != InstrumentProduct.SPOT:
        raise ValueError("Context Spot Forecast 合同必须使用 SPOT Instrument")
    return ForecastContract.create(
        contract_version=policy.contract_version,
        outcome_family_id=policy.outcome_family_id,
        target=ForecastTarget.single_long(instrument),
        allowed_orientations=(ForecastOrientation.CANONICAL,),
        outcome_buckets=policy.outcome_buckets,
        horizon_minutes=policy.horizon_minutes,
        decision_slot_rule="fixed-utc-cadence-after-release-activation-v4",
        evaluation_trigger="contract-cadence-only-v2",
        information_cutoff_rule="fresh-target-state-at-slot-v2",
        completion_deadline_seconds=policy.completion_deadline_seconds,
        minimum_remaining_horizon_minutes=policy.minimum_remaining_horizon_minutes,
        entry_anchor_rule="latest-visible-executable-quote-at-completion-v1",
        cost_semantics_version=cost_semantics_version,
        validity_minutes=policy.validity_minutes,
        validity_conditions=(
            "EXECUTABLE_QUOTES_REMAIN_VALID",
        ),
        settlement_rule="completion-deadline-to-horizon-executable-spot-return-v2",
        outcome_start_delay_seconds=0,
        forecast_benchmark=policy.forecast_benchmark,
        decision_benchmark="cash-and-passive-spot-v1",
    )


@dataclass(frozen=True, slots=True)
class ContextForecastProducer:
    policy: ContextForecastPolicy
    contract: ForecastContract
    binding: ForecastProducerBinding
    market: MarketDataStore
    contexts: SqlContextAssessmentStore
    contracts: SqlForecastContractStore
    forecasts: SqlForecastStore
    instrument: InstrumentId
    analyst: ContextProbabilityAnalyst
    target_states: ContextTargetStateProvider
    analysis_scope: str
    activated_at: datetime

    def __post_init__(self) -> None:
        require_utc(self.activated_at)
        if self.instrument.product != InstrumentProduct.SPOT:
            raise ValueError("首个 Context Forecast 合同只支持可直接持有的 Spot 多头")
        if self.binding.producer_kind != ForecastProducerKind.CONTEXT:
            raise ValueError("Context Forecast 必须绑定 CONTEXT Producer")
        if self.binding.contract_id != self.contract.contract_id:
            raise ValueError("Context Forecast Binding 与 Contract 不一致")
        if self.contract.target.legs[0].instrument != self.instrument:
            raise ValueError("Context Forecast Instrument 与 Contract Target 不一致")

    def produce(self, *, as_of: datetime) -> ForecastProductionResult:
        slot_as_of = require_utc(as_of)
        self.contracts.record_contract(self.contract)
        self.contracts.record_binding(self.binding, activated_at=self.activated_at)
        slot_id = ForecastDecisionSlot.identity_for(self.contract.contract_id, slot_as_of)
        existing = self.forecasts.result_for_behavior(
            decision_slot_id=slot_id,
            producer_behavior_id=self.binding.producer_behavior_id,
        )
        if existing is not None:
            return existing
        absence_id = stable_id(
            "forecast_no_estimate",
            slot_id,
            self.binding.producer_behavior_id,
        )
        existing_absence = self.contracts.no_estimate(absence_id)
        if existing_absence is not None:
            return existing_absence

        assessment = self.contexts.latest_before(
            analysis_scope=self.analysis_scope,
            as_of=slot_as_of,
        )
        packet = (
            self.contexts.packet_for_assessment(assessment.assessment_id)
            if assessment is not None
            else None
        )
        information_cutoff = slot_as_of
        cutoff_quote = self.market.latest_spot_quote(
            instrument=self.instrument,
            evaluation_at=information_cutoff,
            visible_at=information_cutoff,
        )
        cutoff_prices = self._anchors(
            quote=cutoff_quote,
            available_at=information_cutoff,
        )
        slot = self.contracts.slot(slot_id)
        if slot is None:
            slot = ForecastDecisionSlot.create(
                self.contract,
                slot_as_of=slot_as_of,
                information_cutoff_at=information_cutoff,
                cutoff_prices=cutoff_prices,
            )
            self.contracts.record_slot(slot, binding=self.binding)
        elif (
            slot.information_cutoff_at != information_cutoff or slot.cutoff_prices != cutoff_prices
        ):
            raise ValueError("Context decision slot 已绑定不同点时输入")
        else:
            self.contracts.record_obligation(slot=slot, binding=self.binding)

        if assessment is None or packet is None:
            return self._no_estimate(
                slot=slot,
                reason=ForecastNoEstimateReason.WORLD_MODEL_UNAVAILABLE,
                completed_at=slot_as_of,
                input_refs=tuple(item.quote_ref for item in cutoff_prices),
            )
        provenance = (assessment.assessment_id, packet.packet_id, packet.content_hash)
        if assessment.decision_packet_hash != packet.content_hash:
            raise ValueError("Context Forecast 的 WorldModel/Packet 身份不一致")
        if any(item.next_review_at <= slot_as_of for item in assessment.mechanisms):
            return self._no_estimate(
                slot=slot,
                reason=ForecastNoEstimateReason.WORLD_MODEL_STALE,
                completed_at=slot_as_of,
                input_refs=tuple(sorted(provenance)),
                detail="WORLD_MODEL_MECHANISM_REVIEW_DUE",
            )
        try:
            target_state = self.target_states.build(as_of=slot_as_of)
        except ValueError as exc:
            return self._no_estimate(
                slot=slot,
                reason=ForecastNoEstimateReason.MARKET_INPUT_INVALID,
                completed_at=slot_as_of,
                input_refs=tuple(sorted(provenance)),
                detail=f"TARGET_STATE_UNAVAILABLE:{type(exc).__name__}",
            )
        provenance = tuple(sorted({*provenance, *target_state.input_refs}))
        available_features = set(target_state.feature_selectors)
        missing_features = tuple(
            sorted(set(self.binding.required_feature_keys) - available_features)
        )
        if missing_features:
            return self._no_estimate(
                slot=slot,
                reason=ForecastNoEstimateReason.REQUIRED_FEATURE_MISSING,
                completed_at=slot_as_of,
                input_refs=tuple(sorted(provenance)),
                detail=f"MISSING_FEATURES:{','.join(missing_features)}",
            )
        if (
            cutoff_quote is None
            or self._quote_age_seconds(
                cutoff_quote.observed_at,
                information_cutoff,
            )
            > self.policy.maximum_quote_age_seconds
        ):
            return self._no_estimate(
                slot=slot,
                reason=ForecastNoEstimateReason.MARKET_INPUT_INVALID,
                completed_at=slot_as_of,
                input_refs=tuple(sorted(provenance)),
                detail="CUTOFF_QUOTE_MISSING_OR_STALE",
            )

        result = self.analyst.estimate(
            slot=slot,
            assessment=assessment,
            packet=packet,
            target_state=target_state,
        )
        completed_at = max(result.completed_at or slot_as_of, slot_as_of)
        if not result.success or not isinstance(
            result.output,
            ContextForecastStructuredOutput,
        ):
            return self._no_estimate(
                slot=slot,
                reason=ForecastNoEstimateReason.PRODUCER_FAILED,
                completed_at=completed_at,
                input_refs=tuple(sorted(provenance)),
                detail=result.reason_code,
            )
        if completed_at > slot.completion_deadline_at:
            return self._no_estimate(
                slot=slot,
                reason=ForecastNoEstimateReason.DEADLINE_MISSED,
                completed_at=completed_at,
                input_refs=tuple(sorted(provenance)),
                detail="CONTEXT_FORECAST_COMPLETION_DEADLINE_EXCEEDED",
            )
        if slot.evaluation_at - completed_at < timedelta(
            minutes=self.contract.minimum_remaining_horizon_minutes
        ):
            return self._no_estimate(
                slot=slot,
                reason=ForecastNoEstimateReason.INSUFFICIENT_REMAINING_HORIZON,
                completed_at=completed_at,
                input_refs=tuple(sorted(provenance)),
            )
        entry_quote = self.market.latest_spot_quote(
            instrument=self.instrument,
            evaluation_at=completed_at,
            visible_at=completed_at,
        )
        if (
            entry_quote is None
            or self._quote_age_seconds(
                entry_quote.observed_at,
                completed_at,
            )
            > self.policy.maximum_quote_age_seconds
        ):
            return self._no_estimate(
                slot=slot,
                reason=ForecastNoEstimateReason.STALE_BEFORE_AVAILABLE,
                completed_at=completed_at,
                input_refs=tuple(sorted(provenance)),
                detail="ENTRY_QUOTE_MISSING_OR_STALE",
            )
        move_bps = abs((entry_quote.ask / cutoff_quote.ask - Decimal("1")) * Decimal("10000"))
        if move_bps > self.policy.maximum_reanchor_move_bps:
            return self._no_estimate(
                slot=slot,
                reason=ForecastNoEstimateReason.STALE_BEFORE_AVAILABLE,
                completed_at=completed_at,
                input_refs=tuple(
                    sorted({*provenance, cutoff_quote.quote_id, entry_quote.quote_id})
                ),
                detail=f"MATERIAL_MOVE_DURING_ANALYSIS:{move_bps}",
            )
        try:
            return self._finalize(
                slot=slot,
                assessment=assessment,
                packet=packet,
                target_state=target_state,
                output=result.output,
                completed_at=completed_at,
                entry_quote=entry_quote,
            )
        except ValueError:
            return self._no_estimate(
                slot=slot,
                reason=ForecastNoEstimateReason.PRODUCER_FAILED,
                completed_at=completed_at,
                input_refs=tuple(sorted(provenance)),
                detail="CONTEXT_FORECAST_CONTRACT_INVALID",
            )

    def record_deadline_missed(
        self,
        *,
        as_of: datetime,
        completed_at: datetime,
    ) -> ForecastProductionResult:
        """Recover one scheduled obligation without running historical AI."""

        slot_as_of = require_utc(as_of)
        completed = require_utc(completed_at)
        self.contracts.record_contract(self.contract)
        self.contracts.record_binding(self.binding, activated_at=self.activated_at)
        slot_id = ForecastDecisionSlot.identity_for(self.contract.contract_id, slot_as_of)
        existing = self.forecasts.result_for_behavior(
            decision_slot_id=slot_id,
            producer_behavior_id=self.binding.producer_behavior_id,
        )
        if existing is not None:
            return existing
        absence_id = stable_id(
            "forecast_no_estimate",
            slot_id,
            self.binding.producer_behavior_id,
        )
        existing_absence = self.contracts.no_estimate(absence_id)
        if existing_absence is not None:
            return existing_absence
        cutoff_quote = self.market.latest_spot_quote(
            instrument=self.instrument,
            evaluation_at=slot_as_of,
            visible_at=slot_as_of,
        )
        cutoff_prices = self._anchors(
            quote=cutoff_quote,
            available_at=slot_as_of,
        )
        slot = self.contracts.slot(slot_id)
        if slot is None:
            slot = ForecastDecisionSlot.create(
                self.contract,
                slot_as_of=slot_as_of,
                cutoff_prices=cutoff_prices,
            )
            self.contracts.record_slot(slot, binding=self.binding)
        elif slot.cutoff_prices != cutoff_prices:
            raise ValueError("错过的 Context slot 已绑定不同点时价格")
        else:
            self.contracts.record_obligation(slot=slot, binding=self.binding)
        if completed <= slot.completion_deadline_at:
            raise ValueError("只有超过完成期限的 slot 才能记录 DEADLINE_MISSED")
        return self._no_estimate(
            slot=slot,
            reason=ForecastNoEstimateReason.DEADLINE_MISSED,
            completed_at=completed,
            input_refs=tuple(item.quote_ref for item in cutoff_prices),
            detail="SCHEDULED_SLOT_RECOVERED_AFTER_DEADLINE",
        )

    def recover_deadline_missed(
        self,
        *,
        before_as_of: datetime,
        completed_at: datetime,
    ) -> tuple[ForecastNoEstimate, ...]:
        """Materialize every expired cadence obligation after the last known slot."""

        before = require_utc(before_as_of)
        completed = require_utc(completed_at)
        latest = self.contracts.latest_obligated_slot_at(
            binding_id=self.binding.binding_id
        )
        if latest is None:
            return ()
        cursor = latest + timedelta(minutes=self.policy.cadence_minutes)
        recovered: list[ForecastNoEstimate] = []
        while cursor < before:
            if completed <= cursor + timedelta(
                seconds=self.contract.completion_deadline_seconds
            ):
                break
            result = self.record_deadline_missed(
                as_of=cursor,
                completed_at=completed,
            )
            if isinstance(result, ForecastNoEstimate):
                recovered.append(result)
            cursor += timedelta(minutes=self.policy.cadence_minutes)
        return tuple(recovered)

    def _finalize(
        self,
        *,
        slot: ForecastDecisionSlot,
        assessment: ContextAssessment,
        packet: DecisionPacket,
        target_state: ContextForecastTargetState,
        output: ContextForecastStructuredOutput,
        completed_at: datetime,
        entry_quote,
    ) -> BaseForecast:
        draft = output.forecast
        if draft.decision_slot_id != slot.slot_id:
            raise ValueError("Context Forecast 输出绑定了错误 decision slot")
        bucket_ids = tuple(item.bucket_id for item in draft.outcome_probabilities)
        expected_bucket_ids = tuple(item.bucket_id for item in self.contract.outcome_buckets)
        if bucket_ids != expected_bucket_ids:
            raise ValueError("Context Forecast 概率未按合同完整覆盖 bucket")
        probabilities = tuple(
            ForecastBucketProbability(
                bucket_id=item.bucket_id,
                probability=Decimal(item.probability),
            )
            for item in draft.outcome_probabilities
        )
        if sum((item.probability for item in probabilities), Decimal("0")) != 1:
            raise ValueError("Context Forecast 概率之和必须精确为 1")
        mechanisms = {item.mechanism_id: item for item in assessment.mechanisms}
        contribution_ids = tuple(item.mechanism_id for item in draft.mechanism_contributions)
        if len(set(contribution_ids)) != len(contribution_ids) or not set(
            contribution_ids
        ).issubset(mechanisms):
            raise ValueError("Context Forecast 引用了未知或重复 WorldModel mechanism")
        visible_evidence = {
            *(item.evidence_id for item in assessment.event_references),
            *(
                evidence_id
                for mechanism in assessment.mechanisms
                for node in mechanism.causal_chain
                for evidence_id in node.evidence_ids
            ),
            *(
                evidence_id
                for mechanism in assessment.mechanisms
                for evidence_id in mechanism.conflicting_evidence_ids
            ),
        }
        if not set(draft.evidence_refs).issubset(visible_evidence):
            raise ValueError("Context Forecast 引用了 WorldModel 之外的 evidence")
        expected_gross_bps = sum(
            (
                probability.probability * bucket.representative_bps
                for probability, bucket in zip(
                    probabilities,
                    self.contract.outcome_buckets,
                    strict=True,
                )
            ),
            Decimal("0"),
        )
        analysis_input = context_forecast_input_projection(
            slot=slot,
            contract=self.contract,
            assessment=assessment,
            packet=packet,
            target_state=target_state,
        )
        forecast = BaseForecast(
            forecast_id=stable_id(
                "base_forecast",
                slot.slot_id,
                self.binding.producer_behavior_id,
            ),
            contract_id=self.contract.contract_id,
            decision_slot_id=slot.slot_id,
            producer_id=self.binding.producer_id,
            producer_behavior_id=self.binding.producer_behavior_id,
            outcome_family_id=self.contract.outcome_family_id,
            target=self.contract.target,
            horizon_minutes=self.contract.horizon_minutes,
            cutoff_prices=slot.cutoff_prices,
            entry_prices=self._anchors(quote=entry_quote, available_at=completed_at),
            information_cutoff_at=slot.information_cutoff_at,
            input_observed_at=target_state.as_of,
            available_at=completed_at,
            valid_until=min(
                slot.evaluation_at,
                completed_at + timedelta(minutes=self.contract.validity_minutes),
            ),
            outcome_probabilities=probabilities,
            expected_gross_bps=expected_gross_bps,
            input_refs=tuple(
                sorted(
                    {
                        assessment.assessment_id,
                        packet.packet_id,
                        packet.content_hash,
                        *target_state.input_refs,
                        *(item.mechanism_id for item in draft.mechanism_contributions),
                        *draft.evidence_refs,
                        *(item.quote_ref for item in slot.cutoff_prices),
                    }
                )
            ),
            world_model_id=assessment.assessment_id,
            mechanism_contributions=tuple(
                ForecastMechanismContribution(**item.model_dump())
                for item in draft.mechanism_contributions
            ),
            evidence_refs=tuple(sorted(set(draft.evidence_refs))),
            invalidation_conditions=tuple(sorted(set(draft.invalidation_conditions))),
            analysis_input_json=canonical_json(analysis_input),
            analysis_input_hash=content_hash(analysis_input),
        )
        self.forecasts.record(forecast)
        return forecast

    def _anchors(self, *, quote, available_at: datetime) -> tuple[ForecastPriceAnchor, ...]:
        if quote is None:
            return ()
        return (
            ForecastPriceAnchor(
                instrument_id=self.instrument.key,
                price=quote.ask,
                observed_at=quote.observed_at,
                available_at=available_at,
                quote_ref=quote.quote_id,
            ),
        )

    @staticmethod
    def _quote_age_seconds(observed_at: datetime, as_of: datetime) -> float:
        return max(0.0, (as_of - observed_at).total_seconds())

    def _no_estimate(
        self,
        *,
        slot: ForecastDecisionSlot,
        reason: ForecastNoEstimateReason,
        completed_at: datetime,
        input_refs: tuple[str, ...],
        detail: str | None = None,
    ) -> ForecastNoEstimate:
        result = ForecastNoEstimate(
            result_id=stable_id(
                "forecast_no_estimate",
                slot.slot_id,
                self.binding.producer_behavior_id,
            ),
            slot_id=slot.slot_id,
            contract_id=self.contract.contract_id,
            producer_kind=self.binding.producer_kind,
            producer_id=self.binding.producer_id,
            producer_behavior_id=self.binding.producer_behavior_id,
            reason=reason,
            information_cutoff_at=slot.information_cutoff_at,
            attempted_at=slot.slot_as_of,
            completed_at=max(require_utc(completed_at), slot.slot_as_of),
            input_refs=tuple(sorted(set(input_refs))),
            detail=detail,
        )
        self.contracts.record_no_estimate(result)
        return result
