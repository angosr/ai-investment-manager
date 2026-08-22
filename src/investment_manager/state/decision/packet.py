from __future__ import annotations

from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from itertools import groupby
from typing import Literal

from pydantic import Field, field_validator, model_validator

from investment_manager.execution.models import AccountSnapshot
from investment_manager.information.aggregated_flows import CONTINUOUS_CONTEXT_FACT_TYPES
from investment_manager.information.models import (
    CoverageStatus,
    DomainCoverageSnapshot,
    IntelligenceEvent,
    SourceTier,
)
from investment_manager.kernel.identity import (
    SHA256_PATTERN,
    canonical_json,
    content_hash,
    stable_id,
)
from investment_manager.kernel.time import (
    optional_utc,
    require_utc,
)
from investment_manager.kernel.types import (
    FrozenModel,
    Money,
    PositiveDecimal,
)
from investment_manager.market.models import (
    FeatureSnapshot,
    MarketSnapshot,
)
from investment_manager.market.perpetual.models import DerivativeContextSnapshot
from investment_manager.state.facts import (
    FED_CHAIR_PUBLIC_EVENT_FACT_TYPE,
    FOMC_MEETING_FACT_TYPE,
    TREASURY_BUYBACK_OPERATION_FACT_TYPE,
    TREASURY_BUYBACK_RESULT_FACT_TYPE,
)
from investment_manager.state.models import (
    CanonicalFactRevision,
    DeltaCategory,
    FactDecisionMateriality,
    FactRevisionStatus,
    MaterialDelta,
    Materiality,
    StateSnapshot,
)
from investment_manager.state.panel import sanitize_external_text
from investment_manager.state.policy import DecisionPacketPolicy

_LEGACY_PACKET_SCHEMAS_WITHOUT_REVIEW_REQUESTS = {
    "decision-packet-v1",
    "decision-packet-v2",
    "decision-packet-v3",
}
_LEGACY_PACKET_SCHEMAS_WITHOUT_EVENT_URL = {
    "decision-packet-v1",
    "decision-packet-v2",
    "decision-packet-v3",
    "decision-packet-v4",
}
_PACKET_SCHEMAS_WITHOUT_FACT_MATERIALITY = {
    f"decision-packet-v{version}" for version in range(1, 11)
}
_CURRENT_PACKET_SCHEMAS = {
    "decision-packet-v8",
    "decision-packet-v9",
    "decision-packet-v10",
    "decision-packet-v11",
    "decision-packet-v12",
    "decision-packet-v13",
    "decision-packet-v14",
}
_CALENDAR_CONTEXT_FACT_TYPES = {
    FED_CHAIR_PUBLIC_EVENT_FACT_TYPE,
    FOMC_MEETING_FACT_TYPE,
    TREASURY_BUYBACK_OPERATION_FACT_TYPE,
}
_RESULT_CONTEXT_FACT_TYPES = {TREASURY_BUYBACK_RESULT_FACT_TYPE}
_EXTENDED_CONTEXT_FACT_TYPES = _CALENDAR_CONTEXT_FACT_TYPES | _RESULT_CONTEXT_FACT_TYPES
PREVIOUS_CONTEXT_MECHANISM_CHARACTERS = 800
PREVIOUS_CONTEXT_STATEMENT_CHARACTERS = 300
PREVIOUS_CONTEXT_TRANSMISSION_CHARACTERS = 500
PREVIOUS_CONTEXT_INVALIDATION_CHARACTERS = 200
PREVIOUS_CONTEXT_LIST_ITEMS = 3


class DecisionPacketCapacityError(ValueError):
    pass


_ANALYSIS_SIGNIFICANT_DIGITS = 6


def _analysis_decimal(value: Decimal) -> str:
    """Keep decision-scale precision without sending accounting-scale noise."""

    if value == 0:
        return "0"
    quantum = Decimal(1).scaleb(value.copy_abs().adjusted() - _ANALYSIS_SIGNIFICANT_DIGITS + 1)
    text = format(value.quantize(quantum, rounding=ROUND_HALF_EVEN), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def _analysis_fields(item: FrozenModel, names: tuple[str, ...]) -> dict[str, object]:
    payload = item.model_dump(mode="json")
    projected: dict[str, object] = {}
    for name in names:
        if name not in payload:
            continue
        raw = getattr(item, name)
        projected[name] = _analysis_decimal(raw) if isinstance(raw, Decimal) else payload[name]
    return projected


def _analysis_fact(item: PacketFact) -> dict[str, object]:
    """Remove audit-only duplication while retaining epistemic qualifiers."""

    projected: dict[str, object] = {
        "revision_id": item.revision_id,
        "fact_type": item.fact_type,
        "event_time": (item.event_time.isoformat() if item.event_time is not None else None),
        "claim": item.claim,
        "risk_factors": item.risk_factors,
        "decision_materiality": item.decision_materiality.value,
        "directly_triggered": item.directly_triggered,
    }
    if item.status != FactRevisionStatus.ACTIVE:
        projected["status"] = item.status.value
    if item.highest_source_tier != SourceTier.FIRST_PARTY:
        projected["highest_source_tier"] = item.highest_source_tier.value
    if item.independent_source_count != 1:
        projected["independent_source_count"] = item.independent_source_count
    if item.prompt_injection_suspected:
        projected["prompt_injection_suspected"] = True
    return projected


def previous_context_is_decision_relevant(previous: PacketPreviousContext | None) -> bool:
    """Carry the latest compact structural baseline, including uncertain views.

    A world model is durable context, not an alias for a directional signal.
    Current first-party evidence must still confirm, revise, or invalidate it;
    the previous assessment can never prove a fresh cause on its own.
    """

    return previous is not None


def decision_packet_analysis_projection(packet: DecisionPacket) -> dict:
    """Return the dense model-facing projection of an auditable packet.

    Omission identities remain in the immutable packet so a replay can explain
    selection, but they are bookkeeping rather than evidence.  Counting or
    sending those hashes would let audit metadata evict the facts it describes.
    """

    payload = packet.model_dump(mode="json")
    payload["capacity_summary"] = {
        "missing_fact_count": len(packet.missing_fact_revision_ids),
        "omitted_fact_count": len(packet.omitted_fact_revision_ids),
        "omitted_intelligence_event_count": len(packet.omitted_intelligence_event_refs),
    }
    for field_name in (
        "content_hash",
        "coverage_gap_codes",
        "data_quality_codes",
        "missing_fact_revision_ids",
        "mandate_version",
        "omitted_fact_revision_ids",
        "omitted_intelligence_event_refs",
        "packet_id",
        "policy_version",
        "schema_version",
        "state_id",
        "trigger_ids",
    ):
        payload.pop(field_name)
    if packet.capital_objective is not None:
        # Monitoring horizons remain in the immutable packet for Delta and
        # settlement compatibility.  They are not a request for redundant
        # short-horizon direction calls when the mandate names a capital task.
        payload.pop("required_views", None)
    payload["asset_states"] = tuple(
        _analysis_fields(
            item,
            (
                "asset",
                "market_symbol",
                "observed_at",
                "last",
                "return_fraction",
                "realized_volatility",
                "atr",
                "spread_bps",
                "volume_ratio",
                "regime",
            ),
        )
        for item in packet.asset_states
    )
    payload["derivative_states"] = tuple(
        _analysis_fields(
            item,
            (
                "asset",
                "evidence_ref",
                "observed_at",
                "mark_index_premium_bps",
                "executable_short_basis_bps",
                "perpetual_spread_bps",
                "last_funding_rate_bps",
                "trailing_funding_rate_mean_bps",
                "trailing_funding_rate_stddev_bps",
                "trailing_funding_positive_fraction",
                "trailing_funding_rate_min_bps",
                "funding_settlement_count",
                "funding_window_hours",
                "next_funding_time",
                "spot_flow_observed_at",
                "spot_flow_window_minutes",
                "spot_taker_buy_sell_ratio",
                "positioning_observed_at",
                "positioning_window_minutes",
                "open_interest_change_fraction",
                "global_long_account_fraction",
                "taker_buy_sell_ratio",
            ),
        )
        for item in packet.derivative_states
    )
    payload["facts"] = tuple(_analysis_fact(item) for item in packet.facts)
    previous = payload.get("previous_context")
    if previous is not None and not previous_context_is_decision_relevant(packet.previous_context):
        payload.pop("previous_context")
    elif previous is not None:
        if packet.previous_context.schema_version == "legacy-context-assessment-v1":
            payload["previous_context"] = {
                "schema_version": "legacy-context-assessment-v1",
                "event_references": tuple(
                    item
                    for item in previous["event_references"]
                    if item["impact_state"] == "ACTIVE"
                ),
            }
            previous = payload["previous_context"]
        else:
            # Previous hypotheses are derived state, not evidence.  The next
            # analysis only needs their stable identity and falsifiable edge
            # to decide continuity.  Re-sending old causal nodes, conflicts,
            # and capital evidence duplicates the immutable audit trail and
            # makes a maintained model larger than a cold start.
            previous["hypotheses"] = tuple(
                {
                    key: hypothesis[key]
                    for key in (
                        "hypothesis_id",
                        "continuity_ref",
                        "role",
                        "claim",
                        "horizon_hours",
                        "next_observation",
                        "invalidation_conditions",
                        "next_review_at",
                    )
                }
                for hypothesis in previous["hypotheses"]
            )
            capital = previous.get("capital_implication")
            if capital is not None:
                previous["capital_implication"] = {
                    key: capital[key]
                    for key in (
                        "objective_id",
                        "effect",
                        "incremental_reason",
                        "invalidation_conditions",
                    )
                }
        for field_name in (
            "analysis_behavior_hash",
            "analysis_scope",
            "decision_packet_hash",
            "mandate_version",
        ):
            previous.pop(field_name, None)
        previous["event_references"] = tuple(
            item for item in previous["event_references"] if item["impact_state"] == "ACTIVE"
        )
        # Historical contradictions and gaps describe the old evidence cut.
        # Sending them again competes with the current facts and can anchor the
        # analyst on a gap that the new packet has already closed.  They remain
        # in the immutable packet and assessment audit trail.
        previous.pop("contradictions", None)
        previous.pop("data_gaps", None)
    if not packet.review_requests:
        payload.pop("review_requests", None)
    payload["capability_summary"] = tuple(
        {
            "domain": item.domain.value,
            "status": item.status.value,
            # Selected facts are a bounded representative subset.  Keep the
            # compact capability inventory so the analyst cannot mistake an
            # omitted source fact for an unconfigured data route.
            "covered_capabilities": item.covered_capabilities,
            "missing_capabilities": item.missing_capabilities,
            **(
                {}
                if item.status in {CoverageStatus.CURRENT, CoverageStatus.PARTIAL}
                else {
                    "latest_success_at": (
                        item.latest_success_at.isoformat()
                        if item.latest_success_at is not None
                        else None
                    ),
                    "latest_publication_at": (
                        item.latest_publication_at.isoformat()
                        if item.latest_publication_at is not None
                        else None
                    ),
                }
            ),
        }
        for item in packet.information_coverage
    )
    payload.pop("information_coverage", None)
    return payload


class MandateAsset(FrozenModel):
    asset: str = Field(min_length=1)
    market_symbol: str = Field(min_length=1)
    horizons_minutes: tuple[int, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def horizons_must_be_positive_unique_and_sorted(self):
        if any(value <= 0 for value in self.horizons_minutes):
            raise ValueError("分析时域必须为正数")
        if tuple(sorted(set(self.horizons_minutes))) != self.horizons_minutes:
            raise ValueError("分析时域必须唯一且排序")
        return self


class CapitalContextObjective(FrozenModel):
    """One research-only capital question; never an order or allocation permission."""

    objective_id: str = Field(min_length=1)
    decision_kind: Literal["CARRY_ENTRY_VETO"] = "CARRY_ENTRY_VETO"
    producer_id: str = Field(min_length=1)
    producer_version: str = Field(min_length=1)
    forecast_family: str = Field(min_length=1)
    base_decision_inputs: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def base_inputs_are_canonical(self):
        if tuple(sorted(set(self.base_decision_inputs))) != self.base_decision_inputs:
            raise ValueError("资本上下文目标的程序输入必须唯一且排序")
        return self


class AnalysisMandate(FrozenModel):
    version: str = Field(min_length=1)
    analysis_scope: str = Field(min_length=1)
    question: str = Field(min_length=1, max_length=500)
    assets: tuple[MandateAsset, ...] = Field(min_length=1)
    required_risk_factors: tuple[str, ...] = Field(min_length=1)
    capital_objective: CapitalContextObjective | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def mandate_identity_must_be_unique_and_sorted(self):
        asset_keys = tuple(item.asset for item in self.assets)
        symbol_keys = tuple(item.market_symbol for item in self.assets)
        if tuple(sorted(set(asset_keys))) != asset_keys:
            raise ValueError("Mandate assets 必须唯一且排序")
        if len(set(symbol_keys)) != len(symbol_keys):
            raise ValueError("Mandate market_symbol 必须唯一")
        # Order is the mandate owner's explicit causal priority when the packet
        # cannot carry one representative for every channel.  Sorting it would
        # silently turn lexical order into investment priority.
        if len(set(self.required_risk_factors)) != len(self.required_risk_factors):
            raise ValueError("required_risk_factors 必须唯一")
        return self


class VisibleFact(FrozenModel):
    fact: CanonicalFactRevision
    highest_source_tier: SourceTier
    independent_source_count: int = Field(gt=0)
    prompt_injection_suspected: bool = False


class PacketPosition(FrozenModel):
    market_symbol: str
    quantity: Decimal
    average_price: Money


class PacketPortfolioState(FrozenModel):
    quote_balance: Money
    equity: Money | None
    daily_pnl: Decimal
    drawdown_fraction: Decimal
    open_order_count: int = Field(ge=0)
    kill_switch_active: bool
    reconciled: bool
    positions: tuple[PacketPosition, ...]


class PacketAssetState(FrozenModel):
    asset: str
    market_symbol: str
    observed_at: datetime
    bid: PositiveDecimal
    ask: PositiveDecimal
    last: PositiveDecimal
    return_fraction: Decimal
    realized_volatility: Money
    atr: Money
    spread_bps: Money
    volume_ratio: Money
    regime: str
    market_age_seconds: int = Field(ge=0)

    _utc_observed_at = field_validator("observed_at")(require_utc)


class PacketDerivativeState(FrozenModel):
    evidence_ref: str = Field(pattern=SHA256_PATTERN)
    asset: str
    market_symbol: str
    observed_at: datetime
    mark_index_premium_bps: Decimal
    executable_short_basis_bps: Decimal
    perpetual_spread_bps: Money
    last_funding_rate_bps: Decimal
    trailing_funding_rate_mean_bps: Decimal | None
    trailing_funding_rate_sum_bps: Decimal | None
    trailing_funding_rate_stddev_bps: Decimal | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    trailing_funding_positive_fraction: Decimal | None = Field(
        default=None,
        ge=0,
        le=1,
        exclude_if=lambda value: value is None,
    )
    trailing_funding_rate_min_bps: Decimal | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    funding_settlement_count: int = Field(ge=0)
    funding_window_hours: int = Field(gt=0, le=720)
    next_funding_time: datetime
    spot_flow_observed_at: datetime | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    spot_flow_window_minutes: int | None = Field(
        default=None,
        gt=0,
        le=1_440,
        exclude_if=lambda value: value is None,
    )
    spot_taker_buy_sell_ratio: Decimal | None = Field(
        default=None,
        ge=0,
        exclude_if=lambda value: value is None,
    )
    spot_taker_buy_volume: Decimal | None = Field(
        default=None,
        ge=0,
        exclude_if=lambda value: value is None,
    )
    spot_taker_sell_volume: Decimal | None = Field(
        default=None,
        ge=0,
        exclude_if=lambda value: value is None,
    )
    positioning_observed_at: datetime | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    positioning_window_minutes: int | None = Field(
        default=None,
        gt=0,
        le=1_440,
        exclude_if=lambda value: value is None,
    )
    open_interest: Decimal | None = Field(
        default=None,
        ge=0,
        exclude_if=lambda value: value is None,
    )
    open_interest_value: Decimal | None = Field(
        default=None,
        ge=0,
        exclude_if=lambda value: value is None,
    )
    open_interest_change_fraction: Decimal | None = Field(
        default=None,
        gt=-1,
        exclude_if=lambda value: value is None,
    )
    global_long_short_account_ratio: Decimal | None = Field(
        default=None,
        gt=0,
        exclude_if=lambda value: value is None,
    )
    global_long_account_fraction: Decimal | None = Field(
        default=None,
        ge=0,
        le=1,
        exclude_if=lambda value: value is None,
    )
    global_short_account_fraction: Decimal | None = Field(
        default=None,
        ge=0,
        le=1,
        exclude_if=lambda value: value is None,
    )
    taker_buy_sell_ratio: Decimal | None = Field(
        default=None,
        ge=0,
        exclude_if=lambda value: value is None,
    )
    taker_buy_volume: Decimal | None = Field(
        default=None,
        ge=0,
        exclude_if=lambda value: value is None,
    )
    taker_sell_volume: Decimal | None = Field(
        default=None,
        ge=0,
        exclude_if=lambda value: value is None,
    )

    _utc_observed_at = field_validator("observed_at")(require_utc)
    _utc_next_funding = field_validator("next_funding_time")(require_utc)
    _utc_spot_flow_observed = field_validator("spot_flow_observed_at")(optional_utc)
    _utc_positioning_observed = field_validator("positioning_observed_at")(optional_utc)

    @model_validator(mode="after")
    def positioning_summary_must_be_complete(self):
        spot_values = (
            self.spot_flow_observed_at,
            self.spot_flow_window_minutes,
            self.spot_taker_buy_sell_ratio,
            self.spot_taker_buy_volume,
            self.spot_taker_sell_volume,
        )
        if any(value is not None for value in spot_values) and not all(
            value is not None for value in spot_values
        ):
            raise ValueError("决策包现货主动成交摘要必须完整或全部缺省")
        values = (
            self.positioning_observed_at,
            self.positioning_window_minutes,
            self.open_interest,
            self.open_interest_value,
            self.global_long_short_account_ratio,
            self.global_long_account_fraction,
            self.global_short_account_fraction,
            self.taker_buy_sell_ratio,
            self.taker_buy_volume,
            self.taker_sell_volume,
        )
        if any(value is not None for value in values) and not all(
            value is not None for value in values
        ):
            raise ValueError("决策包衍生品仓位摘要必须完整或全部缺省")
        return self


class PacketDelta(FrozenModel):
    delta_id: str
    policy_version: str
    category: DeltaCategory
    materiality: Materiality
    observed_at: datetime
    expires_at: datetime
    affected_assets: tuple[str, ...]
    risk_factors: tuple[str, ...]
    horizons_minutes: tuple[int, ...]
    fact_revision_ids: tuple[str, ...]
    feature_snapshot_refs: tuple[str, ...]
    intelligence_event_refs: tuple[str, ...] = ()
    reason_codes: tuple[str, ...]

    _utc_observed_at = field_validator("observed_at")(require_utc)
    _utc_expires_at = field_validator("expires_at")(require_utc)


class PacketFact(FrozenModel):
    fact_id: str
    revision_id: str
    fact_type: str
    status: FactRevisionStatus
    event_time: datetime | None
    observed_at: datetime
    headline: str
    claim: str
    affected_assets: tuple[str, ...]
    risk_factors: tuple[str, ...]
    decision_materiality: FactDecisionMateriality = FactDecisionMateriality.UNKNOWN
    highest_source_tier: SourceTier
    independent_source_count: int = Field(gt=0)
    prompt_injection_suspected: bool
    directly_triggered: bool

    _utc_event_time = field_validator("event_time")(optional_utc)
    _utc_observed_at = field_validator("observed_at")(require_utc)


class PacketIntelligenceEvent(FrozenModel):
    evidence_ref: str = Field(pattern=SHA256_PATTERN)
    evidence_id: str = Field(min_length=1)
    normalizer_version: str = Field(min_length=1)
    acquisition_route: str = Field(min_length=1)
    source: str = Field(min_length=1)
    event_time: datetime
    observed_at: datetime
    title: str = Field(min_length=1)
    body: str
    url: str | None = None
    symbols: tuple[str, ...]
    relevance: Decimal
    impact: Decimal
    source_reliability: Decimal
    novelty: Decimal
    prompt_injection_suspected: bool = True
    directly_triggered: bool
    directional_support_eligible: bool = Field(
        default=False,
        exclude_if=lambda value: value is False,
    )

    _utc_event_time = field_validator("event_time")(require_utc)
    _utc_observed_at = field_validator("observed_at")(require_utc)


class RequiredView(FrozenModel):
    asset: str
    horizon_minutes: int = Field(gt=0)


class PacketReviewRequest(FrozenModel):
    review_id: str
    requested_at: datetime
    reason: str = Field(min_length=1, max_length=500)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=100)

    _utc_requested_at = field_validator("requested_at")(require_utc)

    @classmethod
    def create(
        cls,
        *,
        requested_at: datetime,
        reason: str,
        evidence_ids: tuple[str, ...] = (),
    ) -> PacketReviewRequest:
        requested = require_utc(requested_at)
        evidence = tuple(sorted(evidence_ids))
        return cls(
            review_id=stable_id(
                "decision_packet_review",
                requested,
                reason,
                evidence,
            ),
            requested_at=requested,
            reason=reason,
            evidence_ids=evidence,
        )

    @model_validator(mode="after")
    def identity_and_evidence_must_be_canonical(self):
        if tuple(sorted(set(self.evidence_ids))) != self.evidence_ids:
            raise ValueError("PacketReviewRequest evidence_ids 必须唯一且排序")
        expected = stable_id(
            "decision_packet_review",
            self.requested_at,
            self.reason,
            self.evidence_ids,
        )
        if self.review_id != expected:
            raise ValueError("PacketReviewRequest review_id 与内容不一致")
        return self


class PacketPreviousDriver(FrozenModel):
    statement: str = Field(
        min_length=1,
        max_length=PREVIOUS_CONTEXT_STATEMENT_CHARACTERS,
    )
    status: Literal["CONFIRMED", "INFERRED", "UNVERIFIED"]
    transmission: str = Field(
        min_length=1,
        max_length=PREVIOUS_CONTEXT_TRANSMISSION_CHARACTERS,
    )
    invalidation_condition: str = Field(
        min_length=1,
        max_length=PREVIOUS_CONTEXT_INVALIDATION_CHARACTERS,
    )


class PacketPreviousView(FrozenModel):
    asset: str = Field(min_length=1)
    horizon_minutes: int = Field(gt=0)
    direction: Literal["UP", "DOWN", "UNCERTAIN"]
    already_priced: Literal["NOT_PRICED", "PARTIAL", "MOSTLY_PRICED", "UNKNOWN"]
    uncertainty: Literal["LOW", "MEDIUM", "HIGH", "UNKNOWN"]


class PacketPreviousCapitalRelevance(FrozenModel):
    objective_id: str = Field(min_length=1)
    status: Literal[
        "BASE_UNCHANGED",
        "ENTRY_VETO_CANDIDATE",
        "INSUFFICIENT_EVIDENCE",
    ]
    thesis: str = Field(min_length=1, max_length=800)
    transmission: str = Field(min_length=1, max_length=1_200)
    invalidation_condition: str = Field(min_length=1, max_length=200)


class PacketPreviousEventReference(FrozenModel):
    evidence_id: str = Field(pattern=SHA256_PATTERN)
    source: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=1_000)
    event_time: datetime
    impact_state: Literal["ACTIVE", "STALE"]
    rationale: str = Field(min_length=1, max_length=600)
    stale_at: datetime | None = None

    _utc_event_time = field_validator("event_time")(require_utc)
    _utc_stale_at = field_validator("stale_at")(optional_utc)

    @model_validator(mode="after")
    def stale_time_must_match_state(self):
        if self.impact_state == "STALE":
            if self.stale_at is None:
                raise ValueError("过时事件引用必须记录首次过时时间")
        elif self.stale_at is not None:
            raise ValueError("仍有效事件引用不得记录过时时间")
        return self


class PacketPreviousCausalNode(FrozenModel):
    statement: str = Field(min_length=1, max_length=PREVIOUS_CONTEXT_TRANSMISSION_CHARACTERS)
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def evidence_must_be_unique(self):
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("上一轮因果节点不能重复引用证据")
        return self


class PacketPreviousHypothesis(FrozenModel):
    hypothesis_id: str = Field(min_length=1)
    continuity_ref: str | None = Field(default=None, min_length=1)
    role: Literal["PRIMARY", "ALTERNATIVE", "TAIL_RISK"]
    claim: str = Field(min_length=1, max_length=PREVIOUS_CONTEXT_MECHANISM_CHARACTERS)
    horizon_hours: int = Field(gt=0, le=4_380)
    causal_chain: tuple[PacketPreviousCausalNode, ...] = Field(min_length=2, max_length=5)
    conflicting_evidence_ids: tuple[str, ...] = Field(default=(), max_length=12)
    next_observation: str = Field(min_length=1, max_length=500)
    invalidation_conditions: tuple[str, ...] = Field(min_length=1, max_length=5)
    next_review_at: datetime

    _utc_next_review_at = field_validator("next_review_at")(require_utc)


class PacketPreviousCapitalImplication(FrozenModel):
    objective_id: str = Field(min_length=1)
    effect: Literal["SUPPORT", "NEUTRAL", "CAUTION", "OPPOSE", "INSUFFICIENT"]
    incremental_reason: str = Field(min_length=1, max_length=800)
    transmission: str = Field(min_length=1, max_length=1_200)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=12)
    invalidation_conditions: tuple[str, ...] = Field(min_length=1, max_length=5)


class PacketPreviousDecisionBlocker(FrozenModel):
    question: str = Field(min_length=1, max_length=500)
    action_if_yes: str = Field(min_length=1, max_length=500)
    action_if_no: str = Field(min_length=1, max_length=500)
    observation_needed: str = Field(min_length=1, max_length=500)


class PacketPreviousContext(FrozenModel):
    """Latest inherited world model; derived evidence, never a first-party fact."""

    assessment_id: str = Field(min_length=1)
    schema_version: Literal[
        "legacy-context-assessment-v1",
        "world-model-assessment-v1",
    ] = "legacy-context-assessment-v1"
    analysis_scope: str | None = Field(default=None, min_length=1)
    mandate_version: str | None = Field(default=None, min_length=1)
    analysis_behavior_hash: str | None = Field(
        default=None,
        pattern=SHA256_PATTERN,
    )
    decision_packet_hash: str | None = Field(
        default=None,
        pattern=SHA256_PATTERN,
    )
    as_of: datetime
    available_at: datetime
    market_mechanism: str | None = Field(
        default=None,
        min_length=1,
        max_length=PREVIOUS_CONTEXT_MECHANISM_CHARACTERS,
    )
    drivers: tuple[PacketPreviousDriver, ...] = Field(default=(), max_length=8)
    event_references: tuple[PacketPreviousEventReference, ...] = ()
    capital_relevance: PacketPreviousCapitalRelevance | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    views: tuple[PacketPreviousView, ...] = ()
    contradictions: tuple[str, ...] = Field(default=(), max_length=PREVIOUS_CONTEXT_LIST_ITEMS)
    data_gaps: tuple[str, ...] = Field(default=(), max_length=PREVIOUS_CONTEXT_LIST_ITEMS)
    hypotheses: tuple[PacketPreviousHypothesis, ...] = Field(default=(), max_length=3)
    capital_implication: PacketPreviousCapitalImplication | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    decision_blockers: tuple[PacketPreviousDecisionBlocker, ...] = Field(
        default=(),
        max_length=2,
    )

    _utc_as_of = field_validator("as_of")(require_utc)
    _utc_available_at = field_validator("available_at")(require_utc)

    @model_validator(mode="after")
    def timeline_and_views_are_consistent(self):
        if self.available_at < self.as_of:
            raise ValueError("上一轮世界认知的可用时间不能早于分析时点")
        keys = tuple((item.asset, item.horizon_minutes) for item in self.views)
        if tuple(sorted(set(keys))) != keys:
            raise ValueError("上一轮世界认知 views 必须唯一且排序")
        event_ids = tuple(item.evidence_id for item in self.event_references)
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("上一轮世界认知不能重复引用事件")
        if self.schema_version == "legacy-context-assessment-v1":
            if self.market_mechanism is None:
                raise ValueError("历史上一轮世界认知必须包含 market_mechanism")
            if self.hypotheses or self.capital_implication or self.decision_blockers:
                raise ValueError("历史上一轮世界认知不得混入新字段")
            return self
        if any(
            (
                self.market_mechanism is not None,
                bool(self.drivers),
                self.capital_relevance is not None,
                bool(self.views),
                bool(self.contradictions),
                bool(self.data_gaps),
            )
        ):
            raise ValueError("新上一轮世界模型不得混入已废弃字段")
        if sum(item.role == "PRIMARY" for item in self.hypotheses) != 1:
            raise ValueError("新上一轮世界模型必须且只能有一个 PRIMARY")
        return self


class DecisionPacket(FrozenModel):
    packet_id: str
    schema_version: str
    policy_version: str
    mandate_version: str
    analysis_scope: str
    as_of: datetime
    state_id: str
    question: str
    capital_objective: CapitalContextObjective | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    trigger_ids: tuple[str, ...] = Field(min_length=1)
    required_views: tuple[RequiredView, ...] = Field(min_length=1)
    portfolio: PacketPortfolioState
    asset_states: tuple[PacketAssetState, ...] = Field(min_length=1)
    derivative_states: tuple[PacketDerivativeState, ...] = ()
    deltas: tuple[PacketDelta, ...] = ()
    review_requests: tuple[PacketReviewRequest, ...] = ()
    facts: tuple[PacketFact, ...]
    intelligence_events: tuple[PacketIntelligenceEvent, ...] = ()
    # Read-only compatibility for immutable pre-v8 packets. New packets do not
    # serialize or populate these superseded placeholders.
    active_hypotheses: tuple[str, ...] = Field(default=(), exclude=True)
    previous_assessment_refs: tuple[str, ...] = Field(default=(), exclude=True)
    previous_context: PacketPreviousContext | None = None
    information_coverage: tuple[DomainCoverageSnapshot, ...] = ()
    data_quality_codes: tuple[str, ...]
    coverage_gap_codes: tuple[str, ...]
    missing_fact_revision_ids: tuple[str, ...]
    omitted_fact_revision_ids: tuple[str, ...]
    omitted_intelligence_event_refs: tuple[str, ...] = ()
    content_hash: str = Field(pattern=SHA256_PATTERN)

    _utc_as_of = field_validator("as_of")(require_utc)

    @classmethod
    def create(cls, **content: object) -> DecisionPacket:
        draft = cls.model_construct(
            packet_id="pending",
            content_hash="0" * 64,
            **content,
        )
        packet_hash = _decision_packet_content_hash(draft)
        return cls(
            packet_id=stable_id("decision_packet", packet_hash),
            content_hash=packet_hash,
            **content,
        )

    @model_validator(mode="after")
    def identity_and_refs_must_be_consistent(self):
        expected_trigger_ids = (
            *(item.delta_id for item in self.deltas),
            *(item.review_id for item in self.review_requests),
        )
        if self.trigger_ids != expected_trigger_ids:
            raise ValueError("DecisionPacket trigger_ids 与分析原因不一致")
        if len(set(self.trigger_ids)) != len(self.trigger_ids):
            raise ValueError("DecisionPacket trigger_ids 不得重复")
        if not self.deltas and not self.review_requests:
            raise ValueError("DecisionPacket 至少需要一个 Delta 或显式评审请求")
        review_ids = tuple(item.review_id for item in self.review_requests)
        if tuple(sorted(set(review_ids))) != review_ids:
            raise ValueError("DecisionPacket review_requests 必须唯一且排序")
        if any(item.requested_at > self.as_of for item in self.review_requests):
            raise ValueError("DecisionPacket 评审请求不能晚于 as_of")
        required_view_keys = tuple(
            (item.asset, item.horizon_minutes) for item in self.required_views
        )
        if tuple(sorted(set(required_view_keys))) != required_view_keys:
            raise ValueError("DecisionPacket required_views 必须唯一且排序")
        asset_keys = tuple(item.asset for item in self.asset_states)
        if tuple(sorted(set(asset_keys))) != asset_keys:
            raise ValueError("DecisionPacket asset_states 必须按资产唯一且排序")
        if set(asset_keys) != {item.asset for item in self.required_views}:
            raise ValueError("DecisionPacket asset_states 与 required_views 不一致")
        derivative_keys = tuple(item.asset for item in self.derivative_states)
        if tuple(sorted(set(derivative_keys))) != derivative_keys:
            raise ValueError("DecisionPacket derivative_states 必须按资产唯一且排序")
        if self.derivative_states and set(derivative_keys) != set(asset_keys):
            raise ValueError("DecisionPacket derivative_states 与 asset_states 不一致")
        for derivative in self.derivative_states:
            observation_times = (
                derivative.observed_at,
                derivative.spot_flow_observed_at,
                derivative.positioning_observed_at,
            )
            if any(
                observed_at is not None and observed_at > self.as_of
                for observed_at in observation_times
            ):
                raise ValueError("DecisionPacket 衍生品或成交摘要不能晚于 as_of")
        revision_ids = tuple(item.revision_id for item in self.facts)
        if len(set(revision_ids)) != len(revision_ids):
            raise ValueError("DecisionPacket facts revision_id 不得重复")
        event_refs = tuple(item.evidence_ref for item in self.intelligence_events)
        if len(set(event_refs)) != len(event_refs):
            raise ValueError("DecisionPacket intelligence event ref 不得重复")
        for name in (
            "data_quality_codes",
            "coverage_gap_codes",
            "missing_fact_revision_ids",
            "omitted_fact_revision_ids",
            "omitted_intelligence_event_refs",
        ):
            values = getattr(self, name)
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"DecisionPacket {name} 必须唯一且排序")
        if self.schema_version in _CURRENT_PACKET_SCHEMAS and (
            self.active_hypotheses or self.previous_assessment_refs
        ):
            raise ValueError("DecisionPacket v8+ 不再写入旧假设或 Assessment 引用占位")
        if self.schema_version in {"decision-packet-v13", "decision-packet-v14"} and (
            self.capital_objective is None
        ):
            raise ValueError("DecisionPacket v13+ 必须绑定一个明确资本问题")
        coverage_domains = tuple(item.domain.value for item in self.information_coverage)
        if tuple(sorted(set(coverage_domains))) != coverage_domains:
            raise ValueError("DecisionPacket information_coverage 必须按领域唯一且排序")
        if any(item.as_of != self.as_of for item in self.information_coverage):
            raise ValueError("DecisionPacket information_coverage 必须与 packet as_of 一致")
        expected_hash = _decision_packet_content_hash(self)
        if self.content_hash != expected_hash:
            raise ValueError("DecisionPacket content_hash 与内容不一致")
        if self.packet_id != stable_id("decision_packet", expected_hash):
            raise ValueError("DecisionPacket packet_id 与内容身份不一致")
        return self


def _decision_packet_content_hash(packet: DecisionPacket) -> str:
    payload = packet.model_dump(
        mode="json",
        exclude={"packet_id", "content_hash"},
    )
    if packet.schema_version not in _CURRENT_PACKET_SCHEMAS:
        payload["active_hypotheses"] = packet.active_hypotheses
        payload["previous_assessment_refs"] = packet.previous_assessment_refs
    if (
        packet.schema_version in _LEGACY_PACKET_SCHEMAS_WITHOUT_REVIEW_REQUESTS
        and not packet.review_requests
    ):
        payload.pop("review_requests", None)
    if packet.schema_version != "decision-packet-v6" and packet.previous_context is None:
        payload.pop("previous_context", None)
    if packet.schema_version != "decision-packet-v14" and packet.previous_context is not None:
        previous_context = payload["previous_context"]
        if packet.previous_context.schema_version == "legacy-context-assessment-v1":
            previous_context.pop("schema_version", None)
            previous_context.pop("hypotheses", None)
            previous_context.pop("decision_blockers", None)
    if packet.schema_version == "decision-packet-v6" and packet.previous_context is not None:
        previous_context = payload["previous_context"]
        for field_name in (
            "analysis_scope",
            "mandate_version",
            "analysis_behavior_hash",
            "decision_packet_hash",
        ):
            previous_context.pop(field_name, None)
        if not packet.previous_context.event_references:
            previous_context.pop("event_references", None)
    if packet.schema_version in _LEGACY_PACKET_SCHEMAS_WITHOUT_EVENT_URL:
        for event in payload["intelligence_events"]:
            if event["url"] is None:
                event.pop("url")
    if packet.schema_version in _PACKET_SCHEMAS_WITHOUT_FACT_MATERIALITY:
        for fact in payload["facts"]:
            fact.pop("decision_materiality", None)
    if (
        packet.schema_version
        not in {
            "decision-packet-v7",
            "decision-packet-v8",
            "decision-packet-v9",
            "decision-packet-v10",
            "decision-packet-v11",
        }
        and not packet.information_coverage
    ):
        payload.pop("information_coverage", None)
    if packet.schema_version not in _CURRENT_PACKET_SCHEMAS and not packet.derivative_states:
        payload.pop("derivative_states", None)
    return content_hash(payload)


_SOURCE_RANK = {
    SourceTier.FIRST_PARTY: 0,
    SourceTier.CONTRACTED: 1,
    SourceTier.AGGREGATOR: 2,
}


class DecisionPacketBuilder:
    def __init__(self, policy: DecisionPacketPolicy) -> None:
        self._policy = policy

    def build(
        self,
        *,
        mandate: AnalysisMandate,
        state: StateSnapshot,
        deltas: tuple[MaterialDelta, ...],
        facts: tuple[VisibleFact, ...],
        intelligence_events: tuple[IntelligenceEvent, ...] = (),
        review_requests: tuple[PacketReviewRequest, ...] = (),
        account: AccountSnapshot,
        markets: tuple[MarketSnapshot, ...],
        features: tuple[FeatureSnapshot, ...],
        derivatives: tuple[DerivativeContextSnapshot, ...] = (),
        previous_context: PacketPreviousContext | None = None,
        information_coverage: tuple[DomainCoverageSnapshot, ...] = (),
    ) -> DecisionPacket:
        ordered_deltas = tuple(sorted(deltas, key=lambda item: (item.observed_at, item.delta_id)))
        ordered_reviews = tuple(sorted(review_requests, key=lambda item: item.review_id))
        self._validate_inputs(
            mandate=mandate,
            state=state,
            deltas=ordered_deltas,
            review_requests=ordered_reviews,
            facts=facts,
            intelligence_events=intelligence_events,
            account=account,
            markets=markets,
            features=features,
            derivatives=derivatives,
            previous_context=previous_context,
        )
        direct_fact_ids = tuple(
            sorted({fact_id for delta in ordered_deltas for fact_id in delta.fact_revision_ids})
        )
        visible_by_id = {item.fact.revision_id: item for item in facts}
        missing_fact_ids = tuple(
            fact_id for fact_id in direct_fact_ids if fact_id not in visible_by_id
        )
        selected, omitted = self._select_facts(
            mandate=mandate,
            facts=facts,
            direct_fact_ids=frozenset(direct_fact_ids),
            as_of=state.as_of,
        )
        direct_event_refs = frozenset(
            event_ref for delta in ordered_deltas for event_ref in delta.intelligence_event_refs
        )
        selected_events, omitted_events = self._select_intelligence_events(
            events=intelligence_events,
            direct_event_refs=direct_event_refs,
            as_of=state.as_of,
        )
        market_by_symbol = {item.symbol: item for item in markets}
        feature_by_symbol = {item.symbol: item for item in features}
        asset_states = tuple(
            self._asset_state(
                asset=item,
                market=market_by_symbol[item.market_symbol],
                features=feature_by_symbol[item.market_symbol],
            )
            for item in mandate.assets
        )
        derivative_states = tuple(
            self._derivative_state(item)
            for item in sorted(derivatives, key=lambda value: value.asset)
        )
        required_views = tuple(
            RequiredView(asset=item.asset, horizon_minutes=horizon)
            for item in mandate.assets
            for horizon in item.horizons_minutes
        )
        trigger_ids = (
            *(delta.delta_id for delta in ordered_deltas),
            *(review.review_id for review in ordered_reviews),
        )
        payload: dict[str, object] = {
            "schema_version": self._policy.schema_version,
            "policy_version": self._policy.version,
            "mandate_version": mandate.version,
            "analysis_scope": mandate.analysis_scope,
            "as_of": state.as_of,
            "state_id": state.state_id,
            "question": mandate.question,
            "capital_objective": mandate.capital_objective,
            "trigger_ids": trigger_ids,
            "required_views": required_views,
            "portfolio": self._portfolio_state(account),
            "asset_states": asset_states,
            "derivative_states": derivative_states,
            "deltas": tuple(self._delta(item) for item in ordered_deltas),
            "review_requests": ordered_reviews,
            "facts": selected,
            "intelligence_events": selected_events,
            "previous_context": self._compact_previous_context(
                previous_context,
                as_of=state.as_of,
            ),
            "information_coverage": information_coverage,
            "data_quality_codes": state.data_quality_codes,
            "coverage_gap_codes": state.coverage_gap_codes,
            "missing_fact_revision_ids": missing_fact_ids,
            "omitted_fact_revision_ids": omitted,
            "omitted_intelligence_event_refs": omitted_events,
        }
        selected_facts = list(selected)
        selected_intelligence = list(selected_events)
        omitted_facts = set(omitted)
        omitted_intelligence = set(omitted_events)
        while True:
            payload["facts"] = tuple(selected_facts)
            payload["intelligence_events"] = tuple(selected_intelligence)
            payload["omitted_fact_revision_ids"] = tuple(sorted(omitted_facts))
            payload["omitted_intelligence_event_refs"] = tuple(sorted(omitted_intelligence))
            packet = DecisionPacket.create(**payload)
            if (
                len(canonical_json(decision_packet_analysis_projection(packet)))
                <= self._policy.maximum_packet_characters
            ):
                return packet
            removable_fact = (
                next(
                    (
                        index
                        for index in range(len(selected_facts) - 1, -1, -1)
                        if not selected_facts[index].directly_triggered
                    ),
                    None,
                )
                if len(selected_facts) > 1
                else None
            )
            if removable_fact is not None:
                removed = selected_facts.pop(removable_fact)
                omitted_facts.add(removed.revision_id)
                continue
            removable_event = (
                next(
                    (
                        index
                        for index in range(len(selected_intelligence) - 1, -1, -1)
                        if not selected_intelligence[index].directly_triggered
                    ),
                    None,
                )
                if len(selected_intelligence) > 1
                else None
            )
            if removable_event is not None:
                removed = selected_intelligence.pop(removable_event)
                omitted_intelligence.add(removed.evidence_ref)
                continue
            raise DecisionPacketCapacityError(
                "DecisionPacket directly triggered mandatory content exceeds "
                "maximum_packet_characters"
            )

    def _validate_inputs(
        self,
        *,
        mandate: AnalysisMandate,
        state: StateSnapshot,
        deltas: tuple[MaterialDelta, ...],
        review_requests: tuple[PacketReviewRequest, ...],
        facts: tuple[VisibleFact, ...],
        intelligence_events: tuple[IntelligenceEvent, ...],
        account: AccountSnapshot,
        markets: tuple[MarketSnapshot, ...],
        features: tuple[FeatureSnapshot, ...],
        derivatives: tuple[DerivativeContextSnapshot, ...],
        previous_context: PacketPreviousContext | None,
    ) -> None:
        if state.analysis_scope != mandate.analysis_scope:
            raise ValueError("StateSnapshot 与 AnalysisMandate scope 不一致")
        if previous_context is not None:
            if previous_context.analysis_scope != mandate.analysis_scope:
                raise ValueError("上一轮世界认知与 AnalysisMandate scope 不一致")
            if previous_context.available_at > state.as_of:
                raise ValueError("上一轮世界认知在 StateSnapshot as_of 时尚不可见")
        if not deltas and not review_requests:
            raise ValueError("DecisionPacket 至少需要 MaterialDelta 或显式评审请求")
        if tuple(sorted(set(item.review_id for item in review_requests))) != tuple(
            item.review_id for item in review_requests
        ):
            raise ValueError("PacketReviewRequest 必须按 review_id 唯一且排序")
        if any(item.requested_at > state.as_of for item in review_requests):
            raise ValueError("PacketReviewRequest requested_at 晚于 StateSnapshot")
        if account.as_of > state.as_of or account.observed_at > state.as_of:
            raise ValueError("账户事实晚于 StateSnapshot as_of")
        if state.account_snapshot_ref != content_hash(account):
            raise ValueError("账户事实与 StateSnapshot account_snapshot_ref 不一致")
        symbols = tuple(item.market_symbol for item in mandate.assets)
        if tuple(sorted(item.symbol for item in markets)) != tuple(sorted(symbols)):
            raise ValueError("MarketSnapshot 集合与 Mandate assets 不一致")
        if tuple(sorted(item.symbol for item in features)) != tuple(sorted(symbols)):
            raise ValueError("FeatureSnapshot 集合与 Mandate assets 不一致")
        if state.market_snapshot_refs != tuple(sorted(content_hash(item) for item in markets)):
            raise ValueError("行情事实与 StateSnapshot market_snapshot_refs 不一致")
        if state.feature_snapshot_refs != tuple(sorted(content_hash(item) for item in features)):
            raise ValueError("特征事实与 StateSnapshot feature_snapshot_refs 不一致")
        if state.derivative_snapshot_refs != tuple(
            sorted(content_hash(item) for item in derivatives)
        ):
            raise ValueError("衍生品事实与 StateSnapshot derivative_snapshot_refs 不一致")
        derivative_assets = tuple(item.asset for item in derivatives)
        derivative_symbols = tuple(item.instrument.symbol for item in derivatives)
        if derivatives and (
            set(derivative_assets) != {item.asset for item in mandate.assets}
            or set(derivative_symbols) != set(symbols)
        ):
            raise ValueError("DerivativeContextSnapshot 集合与 Mandate assets 不一致")
        for market in markets:
            if market.as_of > state.as_of or market.observed_at > state.as_of:
                raise ValueError("行情事实晚于 StateSnapshot as_of")
        for feature in features:
            if feature.as_of > state.as_of:
                raise ValueError("特征事实晚于 StateSnapshot as_of")
        for derivative in derivatives:
            if derivative.as_of != state.as_of or derivative.observed_at > state.as_of:
                raise ValueError("衍生品事实与 StateSnapshot 时点不一致")
        state_fact_ids = set(state.fact_revision_ids)
        revision_ids = tuple(item.fact.revision_id for item in facts)
        if len(set(revision_ids)) != len(revision_ids):
            raise ValueError("VisibleFact revision_id 必须唯一")
        for visible in facts:
            if visible.fact.observed_at > state.as_of:
                raise ValueError("事实修订晚于 StateSnapshot as_of")
            if visible.fact.revision_id not in state_fact_ids:
                raise ValueError("事实修订不属于 StateSnapshot")
        event_ids = tuple(item.evidence_id for item in intelligence_events)
        if tuple(sorted(set(event_ids))) != event_ids:
            raise ValueError("IntelligenceEvent 必须按 evidence_id 唯一且排序")
        event_refs = tuple(sorted(content_hash(item) for item in intelligence_events))
        if state.intelligence_event_refs != event_refs:
            raise ValueError("事件事实与 StateSnapshot intelligence refs 不一致")
        if any(item.observed_at > state.as_of for item in intelligence_events):
            raise ValueError("IntelligenceEvent 晚于 StateSnapshot as_of")
        for delta in deltas:
            if delta.analysis_scope != mandate.analysis_scope:
                raise ValueError("MaterialDelta 与 AnalysisMandate scope 不一致")
            if delta.current_state_id != state.state_id:
                raise ValueError("MaterialDelta 未指向当前 StateSnapshot")
            if not delta.observed_at <= state.as_of < delta.expires_at:
                raise ValueError("MaterialDelta 在 DecisionPacket as_of 不可用")

    def _select_intelligence_events(
        self,
        *,
        events: tuple[IntelligenceEvent, ...],
        direct_event_refs: frozenset[str],
        as_of: datetime,
    ) -> tuple[tuple[PacketIntelligenceEvent, ...], tuple[str, ...]]:
        eligible: list[IntelligenceEvent] = []
        omitted: list[str] = []
        for event in events:
            evidence_ref = content_hash(event)
            age_seconds = (as_of - event.event_time).total_seconds()
            if evidence_ref not in direct_event_refs and (
                event.impact < self._policy.minimum_background_intelligence_impact
                or event.source_reliability < self._policy.minimum_background_source_reliability
            ):
                omitted.append(evidence_ref)
                continue
            if (
                evidence_ref not in direct_event_refs
                and age_seconds > self._policy.maximum_background_fact_distance_seconds
            ):
                omitted.append(evidence_ref)
                continue
            eligible.append(event)
        ordered = sorted(
            eligible,
            key=lambda item: (
                content_hash(item) not in direct_event_refs,
                -item.impact,
                -item.source_reliability,
                -item.novelty,
                -item.observed_at.timestamp(),
                item.evidence_id,
            ),
        )
        selected: list[PacketIntelligenceEvent] = []
        used_characters = 0
        for event in ordered:
            evidence_ref = content_hash(event)
            title, _ = sanitize_external_text(
                event.title,
                maximum_length=min(
                    240,
                    self._policy.maximum_characters_per_intelligence_event,
                ),
            )
            body, _ = sanitize_external_text(
                event.body,
                maximum_length=self._policy.maximum_characters_per_intelligence_event,
            )
            character_cost = len(title) + len(body)
            if (
                len(selected) >= self._policy.maximum_intelligence_events
                or used_characters + character_cost > self._policy.maximum_intelligence_characters
            ):
                omitted.append(evidence_ref)
                continue
            selected.append(
                PacketIntelligenceEvent(
                    evidence_ref=evidence_ref,
                    evidence_id=event.evidence_id,
                    normalizer_version=event.normalizer_version,
                    acquisition_route=event.acquisition_route,
                    source=event.source,
                    event_time=event.event_time,
                    observed_at=event.observed_at,
                    title=title,
                    body=body,
                    url=event.url,
                    symbols=event.symbols,
                    relevance=event.relevance,
                    impact=event.impact,
                    source_reliability=event.source_reliability,
                    novelty=event.novelty,
                    prompt_injection_suspected=True,
                    directly_triggered=evidence_ref in direct_event_refs,
                    directional_support_eligible=self._is_context_reference_eligible(event),
                )
            )
            used_characters += character_cost
        return tuple(selected), tuple(sorted(omitted))

    def _is_context_reference_eligible(self, event: IntelligenceEvent) -> bool:
        """Keep weak leads visible to a triggered review without promoting them.

        Direct triggering is a latency decision, not an epistemic promotion.  A
        lead must independently clear both materiality and source-quality gates
        before it may persist in the current world model or support direction.
        """

        return (
            event.impact >= self._policy.minimum_background_intelligence_impact
            and event.source_reliability >= self._policy.minimum_background_source_reliability
        )

    def _compact_previous_context(
        self,
        context: PacketPreviousContext | None,
        *,
        as_of: datetime,
    ) -> PacketPreviousContext | None:
        if context is None:
            return None
        update: dict[str, object] = {
            "event_references": tuple(
                item
                for item in context.event_references
                if item.stale_at is None or item.stale_at + timedelta(days=1) > as_of
            ),
        }
        if context.schema_version == "legacy-context-assessment-v1":
            update["drivers"] = context.drivers[: self._policy.maximum_previous_context_drivers]
        return context.model_copy(update=update)

    def _select_facts(
        self,
        *,
        mandate: AnalysisMandate,
        facts: tuple[VisibleFact, ...],
        direct_fact_ids: frozenset[str],
        as_of: datetime,
    ) -> tuple[tuple[PacketFact, ...], tuple[str, ...]]:
        relevant_assets = {item.asset for item in mandate.assets}
        relevant_risk = set(mandate.required_risk_factors)
        scope_relevant = [
            item
            for item in facts
            if item.fact.revision_id in direct_fact_ids
            or bool(relevant_assets.intersection(item.fact.affected_assets))
            or bool(relevant_risk.intersection(item.fact.risk_factors))
        ]
        eligible: list[VisibleFact] = []
        omitted: list[str] = []
        for item in scope_relevant:
            distance = abs(
                ((item.fact.event_time or item.fact.observed_at) - as_of).total_seconds()
            )
            if (
                item.fact.revision_id in direct_fact_ids
                or item.fact.fact_type in CONTINUOUS_CONTEXT_FACT_TYPES
                or (
                    item.fact.fact_type in _EXTENDED_CONTEXT_FACT_TYPES
                    and distance <= self._policy.maximum_calendar_context_distance_seconds
                )
                or distance <= self._policy.maximum_background_fact_distance_seconds
            ):
                eligible.append(item)
            else:
                omitted.append(item.fact.revision_id)
        required_risk_position = {
            risk_factor: position
            for position, risk_factor in enumerate(mandate.required_risk_factors)
        }

        def causal_channel_rank(item: VisibleFact) -> tuple[int, tuple[str, ...]]:
            positions = tuple(
                required_risk_position[risk_factor]
                for risk_factor in item.fact.risk_factors
                if risk_factor in required_risk_position
            )
            return (
                min(positions) if positions else len(required_risk_position),
                item.fact.risk_factors,
            )

        eligible.sort(
            key=lambda item: (
                item.fact.revision_id not in direct_fact_ids,
                item.fact.decision_materiality != FactDecisionMateriality.CANDIDATE,
                item.fact.fact_type not in _RESULT_CONTEXT_FACT_TYPES,
                item.fact.fact_type not in CONTINUOUS_CONTEXT_FACT_TYPES,
                item.fact.fact_type not in _CALENDAR_CONTEXT_FACT_TYPES,
                causal_channel_rank(item),
                _SOURCE_RANK[item.highest_source_tier],
                item.fact.status.value != "ACTIVE",
                abs(((item.fact.event_time or item.fact.observed_at) - as_of).total_seconds()),
                -item.fact.observed_at.timestamp(),
                item.fact.revision_id,
            )
        )
        # Preserve causal coverage inside each epistemic class. Required channels
        # receive a slot before source tier ranks comparable evidence within a
        # channel; otherwise a lower-tier but unique transmission intermediary can
        # be starved by unrelated first-party background. Repeated facts from one
        # channel remain available in later rounds.
        diversified: list[VisibleFact] = []

        def epistemic_rank(item: VisibleFact) -> tuple[bool, bool]:
            return (
                item.fact.revision_id not in direct_fact_ids,
                item.fact.decision_materiality != FactDecisionMateriality.CANDIDATE,
            )

        for _, ranked_items in groupby(eligible, key=epistemic_rank):
            remaining = list(ranked_items)
            while remaining:
                seen_channels: set[tuple[str, ...]] = set()
                next_round: list[VisibleFact] = []
                for item in remaining:
                    channel = item.fact.risk_factors
                    if channel in seen_channels:
                        next_round.append(item)
                        continue
                    diversified.append(item)
                    seen_channels.add(channel)
                remaining = next_round
        eligible = diversified
        direct_count = sum(item.fact.revision_id in direct_fact_ids for item in eligible)
        if direct_count > self._policy.maximum_facts:
            raise DecisionPacketCapacityError("direct facts exceed maximum_facts")
        selected: list[PacketFact] = []
        used_characters = 0
        for item in eligible:
            headline, headline_suspicious = sanitize_external_text(
                item.fact.headline,
                maximum_length=min(240, self._policy.maximum_characters_per_fact),
            )
            claim, claim_suspicious = sanitize_external_text(
                item.fact.claim,
                maximum_length=self._policy.maximum_characters_per_fact,
            )
            character_cost = len(headline) + len(claim)
            required = item.fact.revision_id in direct_fact_ids
            if len(selected) >= self._policy.maximum_facts or (
                used_characters + character_cost > self._policy.maximum_fact_characters
            ):
                if required:
                    raise DecisionPacketCapacityError("direct facts exceed fact character capacity")
                omitted.append(item.fact.revision_id)
                continue
            selected.append(
                PacketFact(
                    fact_id=item.fact.fact_id,
                    revision_id=item.fact.revision_id,
                    fact_type=item.fact.fact_type,
                    status=item.fact.status,
                    event_time=item.fact.event_time,
                    observed_at=item.fact.observed_at,
                    headline=headline,
                    claim=claim,
                    affected_assets=item.fact.affected_assets,
                    risk_factors=item.fact.risk_factors,
                    decision_materiality=item.fact.decision_materiality,
                    highest_source_tier=item.highest_source_tier,
                    independent_source_count=item.independent_source_count,
                    prompt_injection_suspected=(
                        item.prompt_injection_suspected or headline_suspicious or claim_suspicious
                    ),
                    directly_triggered=required,
                )
            )
            used_characters += character_cost
        return tuple(selected), tuple(sorted(omitted))

    @staticmethod
    def _asset_state(
        *,
        asset: MandateAsset,
        market: MarketSnapshot,
        features: FeatureSnapshot,
    ) -> PacketAssetState:
        return PacketAssetState(
            asset=asset.asset,
            market_symbol=asset.market_symbol,
            observed_at=market.observed_at,
            bid=market.bid,
            ask=market.ask,
            last=market.last,
            return_fraction=features.return_fraction,
            realized_volatility=features.realized_volatility,
            atr=features.atr,
            spread_bps=features.spread_bps,
            volume_ratio=features.volume_ratio,
            regime=features.regime,
            market_age_seconds=features.market_age_seconds,
        )

    @staticmethod
    def _portfolio_state(account: AccountSnapshot) -> PacketPortfolioState:
        return PacketPortfolioState(
            quote_balance=account.quote_balance,
            equity=account.equity,
            daily_pnl=account.daily_pnl,
            drawdown_fraction=account.drawdown_fraction,
            open_order_count=account.open_order_count,
            kill_switch_active=account.kill_switch_active,
            reconciled=account.reconciled,
            positions=tuple(
                PacketPosition(
                    market_symbol=item.symbol,
                    quantity=item.quantity,
                    average_price=item.average_price,
                )
                for item in sorted(account.positions, key=lambda value: value.symbol)
            ),
        )

    @staticmethod
    def _derivative_state(
        snapshot: DerivativeContextSnapshot,
    ) -> PacketDerivativeState:
        return PacketDerivativeState(
            evidence_ref=content_hash(snapshot),
            asset=snapshot.asset,
            market_symbol=snapshot.instrument.symbol,
            observed_at=snapshot.observed_at,
            mark_index_premium_bps=snapshot.mark_index_premium_bps,
            executable_short_basis_bps=snapshot.executable_short_basis_bps,
            perpetual_spread_bps=snapshot.perpetual_spread_bps,
            last_funding_rate_bps=snapshot.last_funding_rate_bps,
            trailing_funding_rate_mean_bps=snapshot.trailing_funding_rate_mean_bps,
            trailing_funding_rate_sum_bps=snapshot.trailing_funding_rate_sum_bps,
            trailing_funding_rate_stddev_bps=(snapshot.trailing_funding_rate_stddev_bps),
            trailing_funding_positive_fraction=(snapshot.trailing_funding_positive_fraction),
            trailing_funding_rate_min_bps=snapshot.trailing_funding_rate_min_bps,
            funding_settlement_count=snapshot.funding_settlement_count,
            funding_window_hours=snapshot.funding_window_hours,
            next_funding_time=snapshot.next_funding_time,
            spot_flow_observed_at=snapshot.spot_flow_observed_at,
            spot_flow_window_minutes=snapshot.spot_flow_window_minutes,
            spot_taker_buy_sell_ratio=snapshot.spot_taker_buy_sell_ratio,
            spot_taker_buy_volume=snapshot.spot_taker_buy_volume,
            spot_taker_sell_volume=snapshot.spot_taker_sell_volume,
            positioning_observed_at=snapshot.positioning_observed_at,
            positioning_window_minutes=snapshot.positioning_window_minutes,
            open_interest=snapshot.open_interest,
            open_interest_value=snapshot.open_interest_value,
            open_interest_change_fraction=snapshot.open_interest_change_fraction,
            global_long_short_account_ratio=snapshot.global_long_short_account_ratio,
            global_long_account_fraction=snapshot.global_long_account_fraction,
            global_short_account_fraction=snapshot.global_short_account_fraction,
            taker_buy_sell_ratio=snapshot.taker_buy_sell_ratio,
            taker_buy_volume=snapshot.taker_buy_volume,
            taker_sell_volume=snapshot.taker_sell_volume,
        )

    @staticmethod
    def _delta(delta: MaterialDelta) -> PacketDelta:
        return PacketDelta(
            delta_id=delta.delta_id,
            policy_version=delta.policy_version,
            category=delta.category,
            materiality=delta.materiality,
            observed_at=delta.observed_at,
            expires_at=delta.expires_at,
            affected_assets=delta.affected_assets,
            risk_factors=delta.risk_factors,
            horizons_minutes=delta.horizons_minutes,
            fact_revision_ids=delta.fact_revision_ids,
            feature_snapshot_refs=delta.feature_snapshot_refs,
            intelligence_event_refs=delta.intelligence_event_refs,
            reason_codes=delta.reason_codes,
        )
