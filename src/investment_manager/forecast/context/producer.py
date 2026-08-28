"""Persist a Context forecast or one explicit terminal absence for every slot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from investment_manager.forecast.context.estimate import (
    ContextForecastComparisonState,
    ContextForecastDraft,
    ContextForecastTargetState,
)
from investment_manager.forecast.contracts import (
    ForecastContract,
    ForecastDecisionSlot,
    ForecastOrientation,
    ForecastPriceAnchor,
    ForecastProducerBinding,
)
from investment_manager.forecast.models import ForecastTarget
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
    PacketAssetState,
    PacketDerivativeState,
)


def finalize_context_base_forecast(
    *,
    binding: ForecastProducerBinding,
    contract: ForecastContract,
    slot: ForecastDecisionSlot,
    draft: ContextForecastDraft,
    analysis_input: dict[str, object],
    input_observed_at: datetime,
    available_at: datetime,
    entry_prices: tuple[ForecastPriceAnchor, ...],
    input_refs: tuple[str, ...],
) -> BaseForecast:
    """Materialize one audited Context Forecast from its exact model-visible input."""

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

    world_model = analysis_input.get("world_model")
    if not isinstance(world_model, dict):
        raise ValueError("Context Forecast 输入缺少 WorldModel 投影")
    world_model_id = world_model.get("assessment_id")
    mechanisms = world_model.get("mechanisms")
    event_references = world_model.get("event_references")
    if (
        not isinstance(world_model_id, str)
        or not isinstance(mechanisms, (list, tuple))
        or not isinstance(event_references, (list, tuple))
    ):
        raise ValueError("Context Forecast WorldModel 投影结构非法")
    mechanism_ids = {
        item.get("mechanism_id") for item in mechanisms if isinstance(item, dict)
    }
    if None in mechanism_ids or len(mechanism_ids) != len(mechanisms):
        raise ValueError("Context Forecast WorldModel mechanism 身份非法")
    contribution_ids = tuple(item.mechanism_id for item in draft.mechanism_contributions)
    if len(set(contribution_ids)) != len(contribution_ids) or not set(
        contribution_ids
    ).issubset(mechanism_ids):
        raise ValueError("Context Forecast 引用了未知或重复 WorldModel mechanism")
    visible_evidence = {
        item.get("evidence_id")
        for item in event_references
        if isinstance(item, dict)
    }
    for mechanism in mechanisms:
        if not isinstance(mechanism, dict):
            raise ValueError("Context Forecast WorldModel mechanism 结构非法")
        for key in ("evidence_ids", "conflicting_evidence_ids"):
            refs = mechanism.get(key, ())
            if not isinstance(refs, (list, tuple)):
                raise ValueError("Context Forecast WorldModel evidence 结构非法")
            visible_evidence.update(refs)
    if None in visible_evidence or not set(draft.evidence_refs).issubset(visible_evidence):
        raise ValueError("Context Forecast 引用了 WorldModel 之外的 evidence")

    available = require_utc(available_at)
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
    return BaseForecast(
        forecast_id=stable_id(
            "base_forecast",
            slot.slot_id,
            binding.producer_behavior_id,
        ),
        contract_id=contract.contract_id,
        decision_slot_id=slot.slot_id,
        producer_id=binding.producer_id,
        producer_behavior_id=binding.producer_behavior_id,
        outcome_family_id=contract.outcome_family_id,
        target=contract.target,
        horizon_minutes=contract.horizon_minutes,
        cutoff_prices=slot.cutoff_prices,
        entry_prices=entry_prices,
        information_cutoff_at=slot.information_cutoff_at,
        input_observed_at=require_utc(input_observed_at),
        available_at=available,
        valid_until=min(
            slot.evaluation_at,
            available + timedelta(minutes=contract.validity_minutes),
        ),
        outcome_probabilities=probabilities,
        expected_gross_bps=expected_gross_bps,
        input_refs=tuple(
            sorted(
                {
                    *input_refs,
                    world_model_id,
                    *contribution_ids,
                    *draft.evidence_refs,
                    *(item.quote_ref for item in slot.cutoff_prices),
                }
            )
        ),
        world_model_id=world_model_id,
        mechanism_contributions=tuple(
            ForecastMechanismContribution(**item.model_dump())
            for item in draft.mechanism_contributions
        ),
        evidence_refs=tuple(sorted(set(draft.evidence_refs))),
        invalidation_conditions=tuple(sorted(set(draft.invalidation_conditions))),
        analysis_input_json=canonical_json(analysis_input),
        analysis_input_hash=content_hash(analysis_input),
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
