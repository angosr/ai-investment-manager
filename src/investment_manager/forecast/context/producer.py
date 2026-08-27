"""Persist a Context forecast or one explicit terminal absence for every slot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol, cast

from investment_manager.forecast.codex.router import AnalystResult
from investment_manager.forecast.context.estimate import (
    ContextForecastAnalysisTarget,
    ContextForecastComparisonState,
    ContextForecastStructuredOutput,
    ContextForecastTargetState,
    context_forecast_input_projection,
    context_forecast_output_schema,
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
    ForecastSlotCause,
    ForecastSlotOrigin,
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
from investment_manager.market.features import (
    FeatureEngine,
    build_derivative_context_snapshot,
    build_perpetual_only_context_snapshot,
)
from investment_manager.market.models import (
    InstrumentId,
    InstrumentProduct,
    MarketSnapshot,
    SpotVenue,
)
from investment_manager.market.policy import FeaturePolicy
from investment_manager.market.repository import MarketDataStore
from investment_manager.portfolio.policy import (
    ContextForecastPolicy,
    ContextForecastTargetPolicy,
)
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
        targets: tuple[ContextForecastAnalysisTarget, ...],
        assessment: ContextAssessment,
        packet: DecisionPacket,
    ) -> AnalystResult: ...


class ContextForecastPreflight(Protocol):
    """Persist prospective evaluation identity before the formal AI call."""

    def before_estimate(
        self,
        *,
        slot: ForecastDecisionSlot,
        formal_producer_behavior_id: str,
        formal_analysis_input: dict[str, object],
        formal_output_schema: dict[str, object],
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class CompositeContextForecastPreflight:
    """Require every independent evaluation assignment before the formal call."""

    preflights: tuple[ContextForecastPreflight, ...]

    def __post_init__(self) -> None:
        if not self.preflights:
            raise ValueError("Context Forecast composite preflight 不能为空")

    def before_estimate(
        self,
        *,
        slot: ForecastDecisionSlot,
        formal_producer_behavior_id: str,
        formal_analysis_input: dict[str, object],
        formal_output_schema: dict[str, object],
    ) -> None:
        for preflight in self.preflights:
            preflight.before_estimate(
                slot=slot,
                formal_producer_behavior_id=formal_producer_behavior_id,
                formal_analysis_input=formal_analysis_input,
                formal_output_schema=formal_output_schema,
            )


class ContextTargetStateProvider(Protocol):
    def build(self, *, as_of: datetime) -> ContextForecastTargetState: ...


@dataclass(frozen=True, slots=True)
class MarketContextTargetStateProvider:
    """Rebuild the target-only market state at the Forecast slot, without AI."""

    market: MarketDataStore
    feature_policy: FeaturePolicy
    reference: InstrumentId
    perpetual: InstrumentId | None
    interval: str
    bar_window: int
    funding_lookback_hours: int
    maximum_quote_skew_seconds: int
    comparison: InstrumentId | None = None
    comparison_price_multiplier: Decimal | None = None
    maximum_comparison_age_seconds: int = 300
    cross_venue_spot_venues: tuple[SpotVenue, ...] = ()
    maximum_cross_venue_spot_age_seconds: int = 30

    def __post_init__(self) -> None:
        enabled = self.comparison is not None
        if enabled != (self.comparison_price_multiplier is not None):
            raise ValueError("Context Forecast comparison 与价格换算必须同时配置")
        if enabled:
            assert self.comparison is not None
            if self.reference.product != InstrumentProduct.SPOT:
                raise ValueError("Context Forecast comparison 只允许比较 Spot Outcome")
            if self.comparison.product == InstrumentProduct.SPOT:
                raise ValueError("Context Forecast comparison 必须是指数化线性产品")
            if self.comparison.quote_asset != self.reference.quote_asset:
                raise ValueError("Context Forecast comparison 与 Outcome 计价资产不一致")
        if self.maximum_comparison_age_seconds < 1:
            raise ValueError("Context Forecast comparison 新鲜度必须为正数")

    def build(self, *, as_of: datetime) -> ContextForecastTargetState:
        at = require_utc(as_of)
        cycle_id = stable_id("context_forecast_target_state", self.reference.key, at.isoformat())
        snapshot = None
        asset_states: tuple[PacketAssetState, ...] = ()
        refs: set[str] = set()
        if self.reference.product == InstrumentProduct.SPOT:
            snapshot = self.market.snapshot(
                cycle_id=cycle_id,
                symbol=self.reference.symbol,
                interval=self.interval,
                as_of=at,
                bar_window=self.bar_window,
                source="point-in-time-market-ledger",
            )
            features = FeatureEngine(self.feature_policy).compute(snapshot)
            asset_states = (
                PacketAssetState(
                    asset=self.reference.base_asset,
                    market_symbol=self.reference.symbol,
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
                ),
            )
            refs.update((content_hash(snapshot), content_hash(features)))
        derivative_states: tuple[PacketDerivativeState, ...] = ()
        if self.perpetual is not None:
            state = self.market.latest_perpetual_state(instrument=self.perpetual, as_of=at)
            quote = self.market.latest_perpetual_quote(
                instrument=self.perpetual,
                evaluation_at=at,
                visible_at=at,
            )
            if (state is None) != (quote is None):
                raise PointInTimeInputUnavailable("Context Forecast 衍生品状态与报价必须同时可用")
            if state is not None and quote is not None:
                settlements = self.market.funding_settlements(
                    instrument=self.perpetual,
                    start=at - timedelta(hours=self.funding_lookback_hours),
                    end=at,
                    visible_at=at,
                )
                if snapshot is None:
                    if self.reference != self.perpetual:
                        raise PointInTimeInputUnavailable(
                            "非 Spot Context Forecast 必须以同一永续作为规范参考"
                        )
                    derivative = build_perpetual_only_context_snapshot(
                        cycle_id=cycle_id,
                        asset=self.reference.base_asset,
                        as_of=at,
                        state=state,
                        quote=quote,
                        settlements=settlements,
                        funding_window_hours=self.funding_lookback_hours,
                    )
                else:
                    aligned_spot = self.market.latest_spot_quote(
                        instrument=self.reference,
                        # Spot bookTicker has no exchange timestamp. Align feeds
                        # on the local visibility clock.
                        evaluation_at=quote.observed_at,
                        visible_at=at,
                    )
                    if aligned_spot is None:
                        raise PointInTimeInputUnavailable(
                            "Context Forecast 缺少与衍生品对齐的 Spot quote"
                        )
                    derivative = build_derivative_context_snapshot(
                        cycle_id=cycle_id,
                        asset=self.reference.base_asset,
                        spot=snapshot,
                        aligned_spot_quote=aligned_spot,
                        state=state,
                        quote=quote,
                        settlements=settlements,
                        funding_window_hours=self.funding_lookback_hours,
                        maximum_quote_skew_seconds=self.maximum_quote_skew_seconds,
                        cross_venue_quotes=self._cross_venue_quotes(as_of=at),
                        maximum_cross_venue_age_seconds=(self.maximum_cross_venue_spot_age_seconds),
                    )
                derivative_states = (self._derivative_state(derivative),)
                refs.update(derivative.input_refs)
                refs.add(content_hash(derivative))
        comparison_states: tuple[ContextForecastComparisonState, ...] = ()
        missing_comparison_keys: tuple[str, ...] = ()
        if self.comparison is not None:
            comparison_state = self._comparison_state(
                as_of=at,
                target_snapshot=snapshot,
            )
            if comparison_state is None:
                missing_comparison_keys = (self.comparison.key,)
            else:
                comparison_states = (comparison_state,)
                refs.update(comparison_state.input_refs)
                refs.add(content_hash(comparison_state))
        return ContextForecastTargetState(
            as_of=at,
            asset_states=asset_states,
            derivative_states=derivative_states,
            comparison_states=comparison_states,
            missing_comparison_instrument_keys=missing_comparison_keys,
            input_refs=tuple(sorted(refs)),
        )

    def _comparison_state(
        self,
        *,
        as_of: datetime,
        target_snapshot: MarketSnapshot | None,
    ) -> ContextForecastComparisonState | None:
        assert self.comparison is not None
        assert self.comparison_price_multiplier is not None
        if target_snapshot is None:
            raise ValueError("Context Forecast comparison 缺少 Spot Outcome 状态")
        observed = self.market.latest_perpetual_state(
            instrument=self.comparison,
            as_of=as_of,
        )
        if (
            observed is None
            or (as_of - observed.observed_at).total_seconds() > self.maximum_comparison_age_seconds
        ):
            return None
        schedule = None
        if self.comparison.product == InstrumentProduct.TRADFI_PERPETUAL:
            schedule = self.market.latest_trading_schedule(as_of=as_of)
            session = (
                None
                if schedule is None
                else schedule.session_at(instrument=self.comparison, at=as_of)
            )
            market_session = (
                "SCHEDULE_UNAVAILABLE"
                if schedule is None
                else "OUTSIDE_SCHEDULE"
                if session is None
                else session.session_type.value
            )
        else:
            market_session = "CONTINUOUS"
        target_mid = (target_snapshot.bid + target_snapshot.ask) / Decimal("2")
        normalized_reference = observed.index_price * self.comparison_price_multiplier
        return ContextForecastComparisonState(
            instrument=self.comparison,
            observed_at=observed.observed_at,
            index_price=observed.index_price,
            comparison_price_multiplier=self.comparison_price_multiplier,
            target_reference_deviation_bps=(
                (target_mid / normalized_reference - Decimal("1")) * Decimal("10000")
            ),
            mark_index_premium_bps=(
                (observed.mark_price / observed.index_price - Decimal("1")) * Decimal("10000")
            ),
            market_session=market_session,
            input_refs=tuple(
                sorted(
                    {content_hash(observed)}
                    | (set() if schedule is None else {content_hash(schedule)})
                )
            ),
        )

    def _cross_venue_quotes(self, *, as_of: datetime):
        if not self.cross_venue_spot_venues:
            return ()
        quotes = self.market.latest_cross_venue_spot_quotes(
            symbol=self.reference.symbol,
            venues=self.cross_venue_spot_venues,
            as_of=as_of,
        )
        if len(quotes) != len(self.cross_venue_spot_venues) or any(
            (as_of - item.observed_at).total_seconds() > self.maximum_cross_venue_spot_age_seconds
            for item in quotes
        ):
            return ()
        return quotes

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


def context_forecast_contract(
    *,
    policy: ContextForecastPolicy,
    target_policy: ContextForecastTargetPolicy,
    instrument: InstrumentId,
    cost_semantics_version: str,
) -> ForecastContract:
    if instrument.product not in {
        InstrumentProduct.SPOT,
        InstrumentProduct.TRADFI_PERPETUAL,
        InstrumentProduct.USD_M_PERPETUAL,
    }:
        raise ValueError("Context Forecast 规范参考必须是可结算线性产品")
    return ForecastContract.create(
        contract_version=target_policy.contract_version,
        outcome_family_id=target_policy.outcome_family_id,
        target=ForecastTarget.single_long(instrument),
        allowed_orientations=(ForecastOrientation.CANONICAL,),
        outcome_buckets=target_policy.outcome_buckets,
        horizon_minutes=policy.horizon_minutes,
        decision_slot_rule=(
            "independent-cadence-and-portfolio-material-world-model-v4"
            if policy.material_event_slots_enabled
            else "fixed-utc-cadence-after-release-activation-v4"
        ),
        evaluation_trigger=(
            "independent-cadence-or-portfolio-material-world-model-slot-v4"
            if policy.material_event_slots_enabled
            else "contract-cadence-only-v2"
        ),
        information_cutoff_rule="fresh-target-state-at-slot-v2",
        completion_deadline_seconds=policy.completion_deadline_seconds,
        minimum_remaining_horizon_minutes=policy.minimum_remaining_horizon_minutes,
        entry_anchor_rule="latest-visible-executable-quote-at-completion-v1",
        cost_semantics_version=cost_semantics_version,
        validity_minutes=policy.validity_minutes,
        validity_conditions=("EXECUTABLE_QUOTES_REMAIN_VALID",),
        settlement_rule=(
            "completion-to-horizon-executable-spot-return-v3"
            if instrument.product == InstrumentProduct.SPOT
            else "completion-to-horizon-executable-perpetual-return-and-funding-v1"
        ),
        outcome_start_delay_seconds=0,
        forecast_benchmark=target_policy.forecast_benchmark,
        decision_benchmark="cash-and-legal-product-expressions-v1",
    )


@dataclass(frozen=True, slots=True)
class ContextForecastRuntimeTarget:
    """One independently settleable member of a shared Forecast invocation."""

    policy: ContextForecastTargetPolicy
    contract: ForecastContract
    binding: ForecastProducerBinding
    instrument: InstrumentId
    target_states: ContextTargetStateProvider

    def __post_init__(self) -> None:
        if self.binding.producer_kind != ForecastProducerKind.CONTEXT:
            raise ValueError("Context Forecast 必须绑定 CONTEXT Producer")
        if self.binding.contract_id != self.contract.contract_id:
            raise ValueError("Context Forecast Binding 与 Contract 不一致")
        if self.contract.target.legs[0].instrument != self.instrument:
            raise ValueError("Context Forecast Instrument 与 Contract Target 不一致")
        if self.policy.outcome_family_id != self.contract.outcome_family_id:
            raise ValueError("Context Forecast target policy 与 Contract family 不一致")


@dataclass(frozen=True, slots=True)
class PortfolioContextForecastProducer:
    """Run one Codex call for every currently viable economic target."""

    policy: ContextForecastPolicy
    targets: tuple[ContextForecastRuntimeTarget, ...]
    market: MarketDataStore
    contexts: SqlContextAssessmentStore
    contracts: SqlForecastContractStore
    forecasts: SqlForecastStore
    analyst: ContextProbabilityAnalyst
    analysis_scope: str
    activated_at: datetime
    preflight: ContextForecastPreflight | None = None

    def __post_init__(self) -> None:
        require_utc(self.activated_at)
        keys = tuple(item.instrument.key for item in self.targets)
        families = tuple(item.contract.outcome_family_id for item in self.targets)
        if not self.targets or tuple(sorted(set(keys))) != keys:
            raise ValueError("Portfolio Context Forecast targets 必须唯一排序")
        if len(set(families)) != len(families):
            raise ValueError("Portfolio Context Forecast family 不得重复")
        if any(
            item.binding.producer_id != self.policy.producer_id
            or item.binding.producer_behavior_id != self.policy.producer_behavior_id
            for item in self.targets
        ):
            raise ValueError("Portfolio Context Forecast targets 必须共享生产行为")

    def view(self, outcome_family_id: str) -> ContextForecastProducerView:
        target = next(
            (item for item in self.targets if item.contract.outcome_family_id == outcome_family_id),
            None,
        )
        if target is None:
            raise ValueError("Context Forecast view 引用了未知 family")
        return ContextForecastProducerView(program=self, target=target)

    def existing_result(
        self,
        target: ContextForecastRuntimeTarget,
        *,
        as_of: datetime,
        cause: ForecastSlotCause | None = None,
    ) -> ForecastProductionResult | None:
        slot_at = require_utc(as_of)
        slot_cause = cause or ForecastSlotCause.cadence(target.contract)
        slot_id = ForecastDecisionSlot.identity_for(
            target.contract.contract_id,
            slot_at,
            cause=slot_cause,
        )
        existing = self.forecasts.result_for_behavior(
            decision_slot_id=slot_id,
            producer_behavior_id=target.binding.producer_behavior_id,
        )
        if existing is not None:
            return existing
        return self.contracts.no_estimate(
            stable_id(
                "forecast_no_estimate",
                slot_id,
                target.binding.producer_behavior_id,
            )
        )

    def produce_all(
        self,
        *,
        as_of: datetime,
        cause: ForecastSlotCause | None = None,
    ) -> tuple[ForecastProductionResult, ...]:
        slot_at = require_utc(as_of)
        for target in self.targets:
            self.contracts.record_contract(target.contract)
            self.contracts.record_binding(
                target.binding,
                activated_at=self.activated_at,
            )
        existing = {
            target.contract.outcome_family_id: self.existing_result(
                target,
                as_of=slot_at,
                cause=cause,
            )
            for target in self.targets
        }
        if all(item is not None for item in existing.values()):
            return self._ordered(existing)

        assessment = self.contexts.latest_before(
            analysis_scope=self.analysis_scope,
            as_of=slot_at,
        )
        packet = (
            self.contexts.packet_for_assessment(assessment.assessment_id)
            if assessment is not None
            else None
        )
        pending = tuple(
            target for target in self.targets if existing[target.contract.outcome_family_id] is None
        )
        slots = {
            target.contract.outcome_family_id: self._slot(
                target,
                as_of=slot_at,
                cause=cause,
            )
            for target in pending
        }
        if assessment is None or packet is None:
            self._fill_no_estimates(
                existing,
                pending,
                slots,
                reason=ForecastNoEstimateReason.WORLD_MODEL_UNAVAILABLE,
                completed_at=slot_at,
            )
            return self._ordered(existing)
        provenance = (assessment.assessment_id, packet.packet_id, packet.content_hash)
        if assessment.decision_packet_hash != packet.content_hash:
            raise ValueError("Context Forecast 的 WorldModel/Packet 身份不一致")
        if any(item.next_review_at <= slot_at for item in assessment.mechanisms):
            self._fill_no_estimates(
                existing,
                pending,
                slots,
                reason=ForecastNoEstimateReason.WORLD_MODEL_STALE,
                completed_at=slot_at,
                input_refs=provenance,
                detail="WORLD_MODEL_MECHANISM_REVIEW_DUE",
            )
            return self._ordered(existing)

        analysis_targets: list[ContextForecastAnalysisTarget] = []
        input_refs: dict[str, tuple[str, ...]] = {}
        cutoff_quotes: dict[str, object] = {}
        for target in pending:
            family = target.contract.outcome_family_id
            slot = slots[family]
            try:
                target_state = target.target_states.build(as_of=slot_at)
            except (PointInTimeInputUnavailable, ValueError) as exc:
                existing[family] = self._no_estimate(
                    target,
                    slot=slot,
                    reason=ForecastNoEstimateReason.MARKET_INPUT_INVALID,
                    completed_at=slot_at,
                    input_refs=provenance,
                    detail=f"TARGET_STATE_UNAVAILABLE:{type(exc).__name__}",
                )
                continue
            refs = tuple(sorted({*provenance, *target_state.input_refs}))
            input_refs[family] = refs
            missing = tuple(
                sorted(
                    set(target.binding.required_feature_keys) - set(target_state.feature_selectors)
                )
            )
            if missing:
                existing[family] = self._no_estimate(
                    target,
                    slot=slot,
                    reason=ForecastNoEstimateReason.REQUIRED_FEATURE_MISSING,
                    completed_at=slot_at,
                    input_refs=refs,
                    detail=f"MISSING_FEATURES:{','.join(missing)}",
                )
                continue
            quote = self._quote(target.instrument, at=slot_at)
            if quote is None or self._quote_age_seconds(
                self._quote_observed_at(target.instrument, quote),
                slot_at,
            ) > (self.policy.maximum_quote_age_seconds):
                existing[family] = self._no_estimate(
                    target,
                    slot=slot,
                    reason=ForecastNoEstimateReason.MARKET_INPUT_INVALID,
                    completed_at=slot_at,
                    input_refs=refs,
                    detail="CUTOFF_QUOTE_MISSING_OR_STALE",
                )
                continue
            cutoff_quotes[family] = quote
            analysis_targets.append(
                ContextForecastAnalysisTarget(
                    slot=slot,
                    contract=target.contract,
                    target_state=target_state,
                )
            )

        if not analysis_targets:
            return self._ordered(existing)
        frozen_targets = tuple(analysis_targets)
        analysis_input = context_forecast_input_projection(
            targets=frozen_targets,
            assessment=assessment,
            packet=packet,
        )
        if self.preflight is not None:
            self.preflight.before_estimate(
                slot=frozen_targets[0].slot,
                formal_producer_behavior_id=self.policy.producer_behavior_id,
                formal_analysis_input=analysis_input,
                formal_output_schema=context_forecast_output_schema(
                    targets=frozen_targets,
                    assessment=assessment,
                ),
            )
        result = self.analyst.estimate(
            targets=frozen_targets,
            assessment=assessment,
            packet=packet,
        )
        completed_at = max(result.completed_at or slot_at, slot_at)
        if not result.success or not isinstance(
            result.output,
            ContextForecastStructuredOutput,
        ):
            self._fail_analysis_targets(
                existing,
                frozen_targets,
                completed_at=completed_at,
                input_refs=input_refs,
                detail=result.reason_code,
            )
            return self._ordered(existing)
        drafts = {item.decision_slot_id: item for item in result.output.forecasts}
        expected_slot_ids = {item.slot.slot_id for item in frozen_targets}
        if len(drafts) != len(result.output.forecasts) or set(drafts) != expected_slot_ids:
            self._fail_analysis_targets(
                existing,
                frozen_targets,
                completed_at=completed_at,
                input_refs=input_refs,
                detail="CONTEXT_FORECAST_TARGET_SET_INVALID",
            )
            return self._ordered(existing)
        runtime_by_family = {item.contract.outcome_family_id: item for item in self.targets}
        for analysis_target in frozen_targets:
            family = analysis_target.contract.outcome_family_id
            runtime = runtime_by_family[family]
            slot = analysis_target.slot
            refs = input_refs[family]
            if completed_at > slot.completion_deadline_at:
                existing[family] = self._no_estimate(
                    runtime,
                    slot=slot,
                    reason=ForecastNoEstimateReason.DEADLINE_MISSED,
                    completed_at=completed_at,
                    input_refs=refs,
                    detail="CONTEXT_FORECAST_COMPLETION_DEADLINE_EXCEEDED",
                )
                continue
            if slot.evaluation_at - completed_at < timedelta(
                minutes=runtime.contract.minimum_remaining_horizon_minutes
            ):
                existing[family] = self._no_estimate(
                    runtime,
                    slot=slot,
                    reason=ForecastNoEstimateReason.INSUFFICIENT_REMAINING_HORIZON,
                    completed_at=completed_at,
                    input_refs=refs,
                )
                continue
            entry_quote = self._quote(runtime.instrument, at=completed_at)
            if (
                entry_quote is None
                or self._quote_age_seconds(
                    self._quote_observed_at(runtime.instrument, entry_quote),
                    completed_at,
                )
                > self.policy.maximum_quote_age_seconds
            ):
                existing[family] = self._no_estimate(
                    runtime,
                    slot=slot,
                    reason=ForecastNoEstimateReason.STALE_BEFORE_AVAILABLE,
                    completed_at=completed_at,
                    input_refs=refs,
                    detail="ENTRY_QUOTE_MISSING_OR_STALE",
                )
                continue
            cutoff_quote = cutoff_quotes[family]
            move_bps = abs((entry_quote.ask / cutoff_quote.ask - Decimal("1")) * Decimal("10000"))
            if move_bps > self.policy.maximum_reanchor_move_bps:
                existing[family] = self._no_estimate(
                    runtime,
                    slot=slot,
                    reason=ForecastNoEstimateReason.STALE_BEFORE_AVAILABLE,
                    completed_at=completed_at,
                    input_refs=tuple(
                        sorted(
                            {
                                *refs,
                                cutoff_quote.quote_id,
                                entry_quote.quote_id,
                            }
                        )
                    ),
                    detail=f"MATERIAL_MOVE_DURING_ANALYSIS:{move_bps}",
                )
                continue
            try:
                existing[family] = self._finalize(
                    runtime,
                    target=analysis_target,
                    assessment=assessment,
                    packet=packet,
                    draft=drafts[slot.slot_id],
                    analysis_input=analysis_input,
                    completed_at=completed_at,
                    entry_quote=entry_quote,
                )
            except ValueError:
                existing[family] = self._no_estimate(
                    runtime,
                    slot=slot,
                    reason=ForecastNoEstimateReason.PRODUCER_FAILED,
                    completed_at=completed_at,
                    input_refs=refs,
                    detail="CONTEXT_FORECAST_CONTRACT_INVALID",
                )
        return self._ordered(existing)

    def record_deadline_missed(
        self,
        target: ContextForecastRuntimeTarget,
        *,
        as_of: datetime,
        completed_at: datetime,
        cause: ForecastSlotCause | None = None,
    ) -> ForecastProductionResult:
        slot_at = require_utc(as_of)
        completed = require_utc(completed_at)
        existing = self.existing_result(target, as_of=slot_at, cause=cause)
        if existing is not None:
            return existing
        slot = self._slot(target, as_of=slot_at, cause=cause)
        if completed <= slot.completion_deadline_at:
            raise ValueError("只有超过完成期限的 slot 才能记录 DEADLINE_MISSED")
        return self._no_estimate(
            target,
            slot=slot,
            reason=ForecastNoEstimateReason.DEADLINE_MISSED,
            completed_at=completed,
            input_refs=tuple(item.quote_ref for item in slot.cutoff_prices),
            detail="SCHEDULED_SLOT_RECOVERED_AFTER_DEADLINE",
        )

    def recover_deadline_missed(
        self,
        target: ContextForecastRuntimeTarget,
        *,
        before_as_of: datetime,
        completed_at: datetime,
    ) -> tuple[ForecastNoEstimate, ...]:
        before = require_utc(before_as_of)
        completed = require_utc(completed_at)
        latest = self.contracts.latest_obligated_slot_at(
            binding_id=target.binding.binding_id,
            origin=ForecastSlotOrigin.CADENCE,
        )
        if latest is None:
            return ()
        cursor = latest + timedelta(minutes=self.policy.cadence_minutes)
        recovered: list[ForecastNoEstimate] = []
        while cursor < before:
            if completed <= cursor + timedelta(seconds=target.contract.completion_deadline_seconds):
                break
            outcome = self.record_deadline_missed(
                target,
                as_of=cursor,
                completed_at=completed,
                cause=ForecastSlotCause.cadence(target.contract),
            )
            if isinstance(outcome, ForecastNoEstimate):
                recovered.append(outcome)
            cursor += timedelta(minutes=self.policy.cadence_minutes)
        return tuple(recovered)

    def _slot(
        self,
        target: ContextForecastRuntimeTarget,
        *,
        as_of: datetime,
        cause: ForecastSlotCause | None,
    ) -> ForecastDecisionSlot:
        slot_cause = cause or ForecastSlotCause.cadence(target.contract)
        quote = self._quote(target.instrument, at=as_of)
        anchors = self._anchors(target.instrument, quote=quote, available_at=as_of)
        slot_id = ForecastDecisionSlot.identity_for(
            target.contract.contract_id,
            as_of,
            cause=slot_cause,
        )
        slot = self.contracts.slot(slot_id)
        if slot is None:
            slot = ForecastDecisionSlot.create(
                target.contract,
                slot_as_of=as_of,
                information_cutoff_at=as_of,
                cutoff_prices=anchors,
                cause=slot_cause,
            )
            self.contracts.record_slot(slot, binding=target.binding)
        elif slot.information_cutoff_at != as_of or slot.cutoff_prices != anchors:
            raise ValueError("Context decision slot 已绑定不同点时输入")
        else:
            self.contracts.record_obligation(slot=slot, binding=target.binding)
        return slot

    def _finalize(
        self,
        runtime: ContextForecastRuntimeTarget,
        *,
        target: ContextForecastAnalysisTarget,
        assessment: ContextAssessment,
        packet: DecisionPacket,
        draft,
        analysis_input: dict[str, object],
        completed_at: datetime,
        entry_quote,
    ) -> BaseForecast:
        slot = target.slot
        contract = target.contract
        bucket_ids = tuple(item.bucket_id for item in draft.outcome_probabilities)
        expected_bucket_ids = tuple(item.bucket_id for item in contract.outcome_buckets)
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
        mechanisms = {item.mechanism_id for item in assessment.mechanisms}
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
                    contract.outcome_buckets,
                    strict=True,
                )
            ),
            Decimal("0"),
        )
        forecast = BaseForecast(
            forecast_id=stable_id(
                "base_forecast",
                slot.slot_id,
                runtime.binding.producer_behavior_id,
            ),
            contract_id=contract.contract_id,
            decision_slot_id=slot.slot_id,
            producer_id=runtime.binding.producer_id,
            producer_behavior_id=runtime.binding.producer_behavior_id,
            outcome_family_id=contract.outcome_family_id,
            target=contract.target,
            horizon_minutes=contract.horizon_minutes,
            cutoff_prices=slot.cutoff_prices,
            entry_prices=self._anchors(
                runtime.instrument,
                quote=entry_quote,
                available_at=completed_at,
            ),
            information_cutoff_at=slot.information_cutoff_at,
            input_observed_at=target.target_state.as_of,
            available_at=completed_at,
            valid_until=min(
                slot.evaluation_at,
                completed_at + timedelta(minutes=contract.validity_minutes),
            ),
            outcome_probabilities=probabilities,
            expected_gross_bps=expected_gross_bps,
            input_refs=tuple(
                sorted(
                    {
                        assessment.assessment_id,
                        packet.packet_id,
                        packet.content_hash,
                        *target.target_state.input_refs,
                        *contribution_ids,
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

    def _fill_no_estimates(
        self,
        results: dict[str, ForecastProductionResult | None],
        targets: tuple[ContextForecastRuntimeTarget, ...],
        slots: dict[str, ForecastDecisionSlot],
        *,
        reason: ForecastNoEstimateReason,
        completed_at: datetime,
        input_refs: tuple[str, ...] = (),
        detail: str | None = None,
    ) -> None:
        for target in targets:
            family = target.contract.outcome_family_id
            results[family] = self._no_estimate(
                target,
                slot=slots[family],
                reason=reason,
                completed_at=completed_at,
                input_refs=input_refs,
                detail=detail,
            )

    def _fail_analysis_targets(
        self,
        results: dict[str, ForecastProductionResult | None],
        targets: tuple[ContextForecastAnalysisTarget, ...],
        *,
        completed_at: datetime,
        input_refs: dict[str, tuple[str, ...]],
        detail: str,
    ) -> None:
        runtime_by_family = {item.contract.outcome_family_id: item for item in self.targets}
        for item in targets:
            family = item.contract.outcome_family_id
            results[family] = self._no_estimate(
                runtime_by_family[family],
                slot=item.slot,
                reason=ForecastNoEstimateReason.PRODUCER_FAILED,
                completed_at=completed_at,
                input_refs=input_refs[family],
                detail=detail,
            )

    def _ordered(
        self,
        results: dict[str, ForecastProductionResult | None],
    ) -> tuple[ForecastProductionResult, ...]:
        ordered = tuple(results[item.contract.outcome_family_id] for item in self.targets)
        if any(item is None for item in ordered):
            raise RuntimeError("Context Forecast shared call 未形成完整终态")
        return cast(tuple[ForecastProductionResult, ...], ordered)

    def _quote(self, instrument: InstrumentId, *, at: datetime):
        if instrument.product == InstrumentProduct.SPOT:
            return self.market.latest_spot_quote(
                instrument=instrument,
                evaluation_at=at,
                visible_at=at,
            )
        return self.market.latest_perpetual_quote(
            instrument=instrument,
            evaluation_at=at,
            visible_at=at,
        )

    @staticmethod
    def _anchors(
        instrument: InstrumentId,
        *,
        quote,
        available_at: datetime,
    ) -> tuple[ForecastPriceAnchor, ...]:
        if quote is None:
            return ()
        return (
            ForecastPriceAnchor(
                instrument_id=instrument.key,
                price=quote.ask,
                observed_at=(
                    quote.observed_at
                    if instrument.product == InstrumentProduct.SPOT
                    else quote.exchange_time
                ),
                available_at=available_at,
                quote_ref=quote.quote_id,
            ),
        )

    @staticmethod
    def _quote_observed_at(instrument: InstrumentId, quote) -> datetime:
        return (
            quote.observed_at
            if instrument.product == InstrumentProduct.SPOT
            else quote.exchange_time
        )

    @staticmethod
    def _quote_age_seconds(observed_at: datetime, as_of: datetime) -> float:
        return max(0.0, (as_of - observed_at).total_seconds())

    def _no_estimate(
        self,
        target: ContextForecastRuntimeTarget,
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
                target.binding.producer_behavior_id,
            ),
            slot_id=slot.slot_id,
            contract_id=target.contract.contract_id,
            producer_kind=target.binding.producer_kind,
            producer_id=target.binding.producer_id,
            producer_behavior_id=target.binding.producer_behavior_id,
            reason=reason,
            information_cutoff_at=slot.information_cutoff_at,
            attempted_at=slot.slot_as_of,
            completed_at=max(require_utc(completed_at), slot.slot_as_of),
            input_refs=tuple(sorted(set(input_refs))),
            detail=detail,
        )
        self.contracts.record_no_estimate(result)
        return result


@dataclass(frozen=True, slots=True)
class ContextForecastProducerView:
    """Contract-shaped view over one shared portfolio Forecast producer."""

    program: PortfolioContextForecastProducer
    target: ContextForecastRuntimeTarget

    def existing_result(
        self,
        *,
        as_of: datetime,
        cause: ForecastSlotCause | None = None,
    ) -> ForecastProductionResult | None:
        return self.program.existing_result(
            self.target,
            as_of=as_of,
            cause=cause,
        )

    def produce(
        self,
        *,
        as_of: datetime,
        cause: ForecastSlotCause | None = None,
    ) -> ForecastProductionResult:
        results = self.program.produce_all(as_of=as_of, cause=cause)
        return next(
            item for item in results if item.contract_id == self.target.contract.contract_id
        )

    def record_deadline_missed(
        self,
        *,
        as_of: datetime,
        completed_at: datetime,
        cause: ForecastSlotCause | None = None,
    ) -> ForecastProductionResult:
        return self.program.record_deadline_missed(
            self.target,
            as_of=as_of,
            completed_at=completed_at,
            cause=cause,
        )

    def recover_deadline_missed(
        self,
        *,
        before_as_of: datetime,
        completed_at: datetime,
    ) -> tuple[ForecastNoEstimate, ...]:
        return self.program.recover_deadline_missed(
            self.target,
            before_as_of=before_as_of,
            completed_at=completed_at,
        )
