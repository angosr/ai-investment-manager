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
from investment_manager.information.official.metrics import TREASURY_AUCTION_FACT_TYPE
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
    FED_MONETARY_RELEASE_FACT_TYPE,
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

_CALENDAR_CONTEXT_FACT_TYPES = {
    FED_CHAIR_PUBLIC_EVENT_FACT_TYPE,
    FOMC_MEETING_FACT_TYPE,
    TREASURY_BUYBACK_OPERATION_FACT_TYPE,
}
_RESULT_CONTEXT_FACT_TYPES = {TREASURY_BUYBACK_RESULT_FACT_TYPE}
_EXTENDED_CONTEXT_FACT_TYPES = _CALENDAR_CONTEXT_FACT_TYPES | _RESULT_CONTEXT_FACT_TYPES
PREVIOUS_CONTEXT_MECHANISM_CHARACTERS = 800
PREVIOUS_CONTEXT_TRANSMISSION_CHARACTERS = 500
PREVIOUS_CONTEXT_INVALIDATION_CHARACTERS = 200


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


def _analysis_time(value: datetime) -> str:
    """Represent daily observations as dates; retain time for intraday evidence."""

    if value.hour == value.minute == value.second == value.microsecond == 0:
        return value.date().isoformat()
    return value.isoformat()


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


def _analysis_intelligence_event(item: PacketIntelligenceEvent) -> dict[str, object]:
    """Keep inference-bearing event evidence, not selector/audit metadata.

    The packet's legacy-named ``impact`` field stores discovery priority;
    discovery priority, novelty and relevance rank a lead before selection.
    Once selected they do not become evidence about the real-world event.  The
    immutable packet retains them for audit.  The analyst only needs the event,
    its source, its epistemic eligibility and the time at which it happened.
    """

    projected: dict[str, object] = {
        "evidence_ref": item.evidence_ref,
        "source": item.source,
        "event_time": item.event_time.isoformat(),
        "title": item.title,
        "directional_support_eligible": item.directional_support_eligible,
    }
    if item.body.strip() != item.title.strip():
        projected["body"] = item.body
    return projected


def _continuous_fact_feature_type(item: PacketFact) -> str:
    """Map a continuous fact to one of the existing WorldModel feature families."""

    if item.fact_type == TREASURY_AUCTION_FACT_TYPE:
        return "FINANCING_STATE"
    if any(
        marker in risk_factor
        for risk_factor in item.risk_factors
        for marker in ("FLOW", "HOLDINGS")
    ):
        return "FLOW_STATE"
    return "REGIME_STATE"


def _compact_continuous_fact_state(item: PacketFact) -> str:
    """Project a typed metric snapshot without its transport/audit boilerplate.

    Continuous fact claims are generated by our own metric projectors as
    semicolon-separated fields.  Keep empirical change context and
    decision-bearing values; the outer ``at`` field, source provenance and full claim
    remain available through ``input_ref`` in the immutable packet.
    """

    claim = item.claim.split(". This is", 1)[0].rstrip(". ")
    parts = tuple(part.strip() for part in claim.split(";") if part.strip())
    selected: list[str] = []
    deferred: list[str] = []
    for part in parts:
        key = part.split("=", 1)[0].strip()
        if key in {"aggregator", "effective_date", "lookback"} or key.startswith("sample_size"):
            continue
        if key == "change_context":
            context = part.split("=", 1)[1]
            metric, _, values = context.partition(":")
            fields = {
                key: value
                for token in values.split(",")
                if "=" in token
                for key, value in (token.split("=", 1),)
            }
            selected.append(
                f"change={metric}:percentile={fields.get('absolute_percentile', 'UNKNOWN')},"
                f"n={fields.get('sample_size', 'UNKNOWN')}"
            )
            continue
        if "_change_" in key or key in {
            "finalized_daily_net_inflow_usd_m",
            "absolute_flow_percentile",
        }:
            selected.append(part)
        elif key not in {"cumulative_inflow_usd_m", "value_traded_usd_m"}:
            deferred.append(part)
    selected.extend(deferred)
    # Metric names are a typed schema (``*_pct``, ``*_bps``, ``*_usd_m`` and
    # holdings-specific names). Repeating the unit after every numeric value
    # spends attention without adding information; the immutable claim retains
    # the original units for audit and replay.
    unit_compacted: list[str] = []
    for part in selected:
        key, separator, raw = part.partition("=")
        candidate = raw.split(maxsplit=1)[0] if separator else ""
        try:
            Decimal(candidate)
        except ArithmeticError:
            unit_compacted.append(part)
        else:
            unit_compacted.append(f"{key}={candidate}")
    selected = unit_compacted
    maximum_characters = 400 if item.fact_type == TREASURY_AUCTION_FACT_TYPE else 200
    compacted: list[str] = []
    used = 0
    for part in selected:
        cost = len(part) + (2 if compacted else 0)
        if used + cost > maximum_characters:
            break
        compacted.append(part)
        used += cost
    if not compacted:
        text, _ = sanitize_external_text(claim, maximum_length=200)
        return text
    return "; ".join(compacted)


def _analysis_state_feature(item: PacketFact) -> dict[str, object]:
    projected: dict[str, object] = {
        "type": item.fact_type,
        "at": _analysis_time(item.event_time or item.observed_at),
        "state": _compact_continuous_fact_state(item),
        "ref": item.revision_id,
    }
    if item.highest_source_tier != SourceTier.FIRST_PARTY:
        projected["tier"] = item.highest_source_tier.value
    if item.decision_materiality == FactDecisionMateriality.CANDIDATE:
        projected["materiality"] = item.decision_materiality.value
    return projected


def _analysis_policy_state(item: PacketFact) -> dict[str, object]:
    return {
        "type": item.fact_type,
        "at": _analysis_time(item.event_time or item.observed_at),
        "document": item.headline,
        "state": item.claim,
        "ref": item.revision_id,
    }


def _is_durable_policy_state(item: VisibleFact | PacketFact) -> bool:
    """A verified policy stance remains current until superseded by a new release."""

    fact_type = item.fact.fact_type if isinstance(item, VisibleFact) else item.fact_type
    claim = item.fact.claim if isinstance(item, VisibleFact) else item.claim
    return fact_type == FED_MONETARY_RELEASE_FACT_TYPE and claim.startswith("action=")


def _continuous_fact_is_redundant(
    index: int,
    facts: list[PacketFact],
) -> bool:
    """Allow capacity fallback only when every causal channel remains represented."""

    item = facts[index]
    if item.fact_type not in CONTINUOUS_CONTEXT_FACT_TYPES:
        return False
    other_risk_factors = {
        risk_factor
        for other_index, other in enumerate(facts)
        if other_index != index and other.fact_type in CONTINUOUS_CONTEXT_FACT_TYPES
        for risk_factor in other.risk_factors
    }
    return bool(item.risk_factors) and set(item.risk_factors).issubset(other_risk_factors)


def continuous_fact_numeric_values(item: PacketFact) -> dict[str, Decimal]:
    """Recover numeric fields from deterministic canonical metric claims."""

    if item.fact_type not in CONTINUOUS_CONTEXT_FACT_TYPES:
        return {}
    claim = item.claim.split(". This is", 1)[0].rstrip(". ")
    values: dict[str, Decimal] = {}
    for part in claim.split(";"):
        key, separator, raw = part.strip().partition("=")
        if not separator or not key or key == "change_context":
            continue
        candidate = raw.strip().split(maxsplit=1)[0].rstrip(".")
        try:
            values[key] = Decimal(candidate)
        except ArithmeticError:
            continue
    return values


def _analysis_verification_test(test: dict) -> tuple[object, ...]:
    """Keep one prior test executable while removing structurally empty fields."""

    def predicate(value: dict) -> tuple[object, ...]:
        return tuple(
            item
            for item in (
                value["operator"],
                value["value"],
                value.get("upper_value"),
                value["persistence_observations"],
            )
            if item is not None
        )

    projected: list[object] = [
        test["feature_selector"],
        test["evaluation_window_minutes"],
        predicate(test["supports_predicate"]),
        predicate(test["contradicts_predicate"]),
    ]
    observation = test.get("latest_observation")
    if observation is not None:
        projected.append(
            (
                _analysis_decimal(Decimal(str(observation["value"]))),
                observation["match"],
                observation.get("support_streak", 0),
                observation.get("contradiction_streak", 0),
                observation["resolution"],
            )
        )
    return tuple(projected)


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
        "portfolio",
        "schema_version",
        "state_id",
        "trigger_ids",
    ):
        payload.pop(field_name)
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
                "spot_mid_range_bps",
                "reference_spot_mid_deviation_bps",
                "widest_spot_spread_bps",
                "positioning_observed_at",
                "positioning_window_minutes",
                "open_interest_change_fraction",
                "global_long_account_fraction",
                "taker_buy_sell_ratio",
            ),
        )
        for item in packet.derivative_states
    )
    payload["intelligence_events"] = tuple(
        _analysis_intelligence_event(item) for item in packet.intelligence_events
    )
    continuous_facts = tuple(
        item for item in packet.facts if item.fact_type in CONTINUOUS_CONTEXT_FACT_TYPES
    )
    policy_facts = tuple(item for item in packet.facts if _is_durable_policy_state(item))
    payload["facts"] = tuple(
        _analysis_fact(item)
        for item in packet.facts
        if item.fact_type not in CONTINUOUS_CONTEXT_FACT_TYPES and item not in policy_facts
    )
    if continuous_facts or policy_facts:
        feature_items = tuple(_analysis_state_feature(item) for item in continuous_facts)
        payload["state_features"] = {
            "algorithm_version": "decision-state-feature-v2",
            "regime_states": tuple(
                projected
                for item, projected in zip(continuous_facts, feature_items, strict=True)
                if _continuous_fact_feature_type(item) == "REGIME_STATE"
            ),
            "flow_states": tuple(
                projected
                for item, projected in zip(continuous_facts, feature_items, strict=True)
                if _continuous_fact_feature_type(item) == "FLOW_STATE"
            ),
            "financing_states": tuple(
                projected
                for item, projected in zip(continuous_facts, feature_items, strict=True)
                if _continuous_fact_feature_type(item) == "FINANCING_STATE"
            ),
            "policy_states": tuple(_analysis_policy_state(item) for item in policy_facts),
        }
    previous = payload.get("previous_context")
    if previous is not None and not previous_context_is_decision_relevant(packet.previous_context):
        payload.pop("previous_context")
    elif previous is not None:
        test_catalog: list[tuple[object, ...]] = []
        test_index: dict[str, int] = {}
        for mechanism in previous["mechanisms"]:
            for test in mechanism["verification_tests"]:
                test_key = canonical_json(test)
                if test_key in test_index:
                    continue
                test_index[test_key] = len(test_catalog)
                test_catalog.append(_analysis_verification_test(test))
        payload["previous_context"] = {
            "assessment_id": previous["assessment_id"],
            "as_of": previous["as_of"],
            "test_catalog": tuple(test_catalog),
            "mechanisms": tuple(
                {
                    "id": mechanism["mechanism_id"],
                    "continuity": mechanism["continuity_ref"],
                    "relationship": mechanism["relationship"],
                    "claim": mechanism["claim"],
                    "horizon_h": mechanism["horizon_hours"],
                    "stage": mechanism["transmission_stage"],
                    "tests": tuple(
                        test_index[canonical_json(test)] for test in mechanism["verification_tests"]
                    ),
                    "review_at": mechanism["next_review_at"],
                }
                for mechanism in previous["mechanisms"]
            ),
            "event_references": tuple(
                {
                    "evidence_id": item["evidence_id"],
                    "source": item["source"],
                    "title": item["title"],
                    "event_time": item["event_time"],
                    "rationale": item["rationale"],
                }
                for item in previous["event_references"]
                if item["impact_state"] == "ACTIVE"
            ),
        }
    if not packet.review_requests:
        payload.pop("review_requests", None)
    payload["capability_summary"] = {
        item.domain.value: {
            **({"status": item.status.value} if item.status != CoverageStatus.PARTIAL else {}),
            **({"missing": item.missing_capabilities} if item.missing_capabilities else {}),
        }
        for item in packet.information_coverage
        if item.status != CoverageStatus.CURRENT
    }
    payload.pop("information_coverage", None)
    return payload


class MandateExposure(FrozenModel):
    economic_exposure: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    asset: str = Field(pattern=r"^[A-Z0-9._-]+$")

    @property
    def key(self) -> tuple[str, str]:
        return self.economic_exposure, self.asset


class ObservationAsset(FrozenModel):
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


class AnalysisMandate(FrozenModel):
    version: str = Field(min_length=1)
    analysis_scope: str = Field(min_length=1)
    question: str = Field(min_length=1, max_length=500)
    mandate_exposures: tuple[MandateExposure, ...] = Field(min_length=1)
    observation_assets: tuple[ObservationAsset, ...] = Field(min_length=1)
    required_risk_factors: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def mandate_identity_must_be_unique_and_sorted(self):
        exposure_keys = tuple(item.key for item in self.mandate_exposures)
        if tuple(sorted(set(exposure_keys))) != exposure_keys:
            raise ValueError("Mandate economic exposures 必须唯一且排序")
        asset_keys = tuple(item.asset for item in self.observation_assets)
        symbol_keys = tuple(item.market_symbol for item in self.observation_assets)
        if tuple(sorted(set(asset_keys))) != asset_keys:
            raise ValueError("Mandate observation assets 必须唯一且排序")
        if len(set(symbol_keys)) != len(symbol_keys):
            raise ValueError("Mandate observation market_symbol 必须唯一")
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
    cross_venue_observed_at: datetime | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    spot_venue_count: int | None = Field(
        default=None,
        ge=3,
        exclude_if=lambda value: value is None,
    )
    spot_mid_range_bps: Decimal | None = Field(
        default=None,
        ge=0,
        exclude_if=lambda value: value is None,
    )
    reference_spot_mid_deviation_bps: Decimal | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    widest_spot_spread_bps: Decimal | None = Field(
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
    _utc_cross_venue_observed = field_validator("cross_venue_observed_at")(optional_utc)
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
        cross_venue_values = (
            self.cross_venue_observed_at,
            self.spot_venue_count,
            self.spot_mid_range_bps,
            self.reference_spot_mid_deviation_bps,
            self.widest_spot_spread_bps,
        )
        if any(value is not None for value in cross_venue_values) and not all(
            value is not None for value in cross_venue_values
        ):
            raise ValueError("决策包跨场所现货摘要必须完整或全部缺省")
        if (
            self.cross_venue_observed_at is not None
            and self.cross_venue_observed_at > self.observed_at
        ):
            raise ValueError("决策包跨场所现货摘要不能晚于结构状态")
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


class PacketPreviousVerificationPredicate(FrozenModel):
    operator: Literal["GT", "GTE", "LT", "LTE", "BETWEEN"]
    value: Decimal
    upper_value: Decimal | None = None
    persistence_observations: int = Field(default=1, ge=1, le=24)


class PacketPreviousVerificationObservation(FrozenModel):
    observed_at: datetime
    value: Decimal
    match: Literal["SUPPORTS", "CONTRADICTS", "NEITHER", "AMBIGUOUS"]
    support_streak: int = Field(ge=0)
    contradiction_streak: int = Field(ge=0)
    resolution: Literal["PENDING", "SUPPORTED", "CONTRADICTED", "AMBIGUOUS"]

    _utc_observed_at = field_validator("observed_at")(require_utc)


class PacketPreviousVerificationTest(FrozenModel):
    feature_selector: str = Field(min_length=1, max_length=240)
    evaluation_window_minutes: int = Field(gt=0, le=525_600)
    supports_predicate: PacketPreviousVerificationPredicate
    contradicts_predicate: PacketPreviousVerificationPredicate
    latest_observation: PacketPreviousVerificationObservation | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class PacketPreviousMechanism(FrozenModel):
    mechanism_id: str = Field(min_length=1)
    continuity_ref: str | None = Field(default=None, min_length=1)
    relationship: Literal["SUPPORTS", "OFFSETS", "THREATENS", "ALTERNATIVE"]
    claim: str = Field(min_length=1, max_length=1_200)
    horizon_hours: int = Field(gt=0, le=17_520)
    causal_chain: tuple[PacketPreviousCausalNode, ...] = Field(min_length=2)
    transmission_stage: Literal["PENDING", "PROPAGATING", "PRICED", "REVERSING"]
    conflicting_evidence_ids: tuple[str, ...] = ()
    verification_tests: tuple[PacketPreviousVerificationTest, ...] = Field(min_length=1)
    invalidation_conditions: tuple[str, ...] = Field(min_length=1)
    next_review_at: datetime

    _utc_next_review_at = field_validator("next_review_at")(require_utc)


class PacketPreviousContext(FrozenModel):
    """Latest inherited world model; derived evidence, never a first-party fact."""

    assessment_id: str = Field(min_length=1)
    analysis_scope: str = Field(min_length=1)
    mandate_version: str = Field(min_length=1)
    analysis_behavior_hash: str = Field(pattern=SHA256_PATTERN)
    decision_packet_hash: str = Field(pattern=SHA256_PATTERN)
    as_of: datetime
    available_at: datetime
    event_references: tuple[PacketPreviousEventReference, ...] = ()
    synthesis: str = Field(min_length=1, max_length=2_000)
    synthesis_horizon_hours: int = Field(gt=0, le=17_520)
    mechanisms: tuple[PacketPreviousMechanism, ...] = Field(min_length=1)

    _utc_as_of = field_validator("as_of")(require_utc)
    _utc_available_at = field_validator("available_at")(require_utc)

    @model_validator(mode="after")
    def timeline_and_identity_are_consistent(self):
        if self.available_at < self.as_of:
            raise ValueError("上一轮世界认知的可用时间不能早于分析时点")
        event_ids = tuple(item.evidence_id for item in self.event_references)
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("上一轮世界认知不能重复引用事件")
        mechanism_ids = tuple(item.mechanism_id for item in self.mechanisms)
        if len(set(mechanism_ids)) != len(mechanism_ids):
            raise ValueError("上一轮世界认知不能重复 mechanism_id")
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
    trigger_ids: tuple[str, ...] = Field(min_length=1)
    # v19 历史快照没有该字段；v20 起它是 Capital 可投资经济暴露的唯一模型输入。
    mandate_exposures: tuple[MandateExposure, ...] = ()
    required_views: tuple[RequiredView, ...] = Field(min_length=1)
    # Read-only historical field. Current WorldModel packets do not carry
    # portfolio state; capital truth belongs exclusively to Portfolio.
    portfolio: PacketPortfolioState | None = None
    asset_states: tuple[PacketAssetState, ...] = Field(min_length=1)
    derivative_states: tuple[PacketDerivativeState, ...] = ()
    deltas: tuple[PacketDelta, ...] = ()
    review_requests: tuple[PacketReviewRequest, ...] = ()
    facts: tuple[PacketFact, ...]
    intelligence_events: tuple[PacketIntelligenceEvent, ...] = ()
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
        exposure_keys = tuple(item.key for item in self.mandate_exposures)
        if tuple(sorted(set(exposure_keys))) != exposure_keys:
            raise ValueError("DecisionPacket mandate exposures 必须唯一且排序")
        schema_prefix = "decision-packet-v"
        schema_generation = (
            int(self.schema_version.removeprefix(schema_prefix))
            if self.schema_version.startswith(schema_prefix)
            else None
        )
        if schema_generation is not None and schema_generation >= 20 and not exposure_keys:
            raise ValueError("当前 DecisionPacket 必须冻结 Capital mandate exposures")
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


def _trim_one_packet_input_for_capacity(
    *,
    selected_facts: list[PacketFact],
    selected_intelligence: list[PacketIntelligenceEvent],
    omitted_facts: set[str],
    omitted_intelligence: set[str],
) -> bool:
    """Remove the lowest-value non-direct input using one canonical order."""

    removable_fact = next(
        (
            index
            for index in range(len(selected_facts) - 1, -1, -1)
            if not selected_facts[index].directly_triggered
            and selected_facts[index].fact_type not in CONTINUOUS_CONTEXT_FACT_TYPES
            and not _is_durable_policy_state(selected_facts[index])
        ),
        None,
    )
    if removable_fact is not None:
        removed = selected_facts.pop(removable_fact)
        omitted_facts.add(removed.revision_id)
        return True
    removable_event = next(
        (
            index
            for index in range(len(selected_intelligence) - 1, -1, -1)
            if not selected_intelligence[index].directly_triggered
        ),
        None,
    )
    if removable_event is not None:
        removed = selected_intelligence.pop(removable_event)
        omitted_intelligence.add(removed.evidence_ref)
        return True
    removable_redundant_state = next(
        (
            index
            for index in range(len(selected_facts) - 1, -1, -1)
            if not selected_facts[index].directly_triggered
            and _continuous_fact_is_redundant(index, selected_facts)
        ),
        None,
    )
    if removable_redundant_state is not None:
        removed = selected_facts.pop(removable_redundant_state)
        omitted_facts.add(removed.revision_id)
        return True
    # A previous WorldModel is a derived hypothesis, not a current observation.
    # Never evict the last representative of a causal channel merely because a
    # previous model exists.  If the minimum current baseline cannot fit after
    # redundant state and non-direct background have been removed, the Release
    # capacity contract is invalid and must fail deterministically.
    return False


def replace_packet_previous_context(
    packet: DecisionPacket,
    previous_context: PacketPreviousContext,
    *,
    maximum_analysis_characters: int,
) -> DecisionPacket:
    """Re-freeze a prepared packet after deterministic context verification.

    Market/fact preparation must happen before mechanism predicates can be
    evaluated.  Rebuilding the immutable identity keeps the actual analyst
    input and the audited packet identical after those observations are added.
    """

    if maximum_analysis_characters <= 0:
        raise ValueError("DecisionPacket 最终分析容量必须为正")
    content = {
        name: getattr(packet, name)
        for name in DecisionPacket.model_fields
        if name
        not in {
            "packet_id",
            "content_hash",
            "previous_context",
            "facts",
            "intelligence_events",
            "omitted_fact_revision_ids",
            "omitted_intelligence_event_refs",
        }
    }
    selected_facts = list(packet.facts)
    selected_intelligence = list(packet.intelligence_events)
    omitted_facts = set(packet.omitted_fact_revision_ids)
    omitted_intelligence = set(packet.omitted_intelligence_event_refs)
    while True:
        candidate = DecisionPacket.create(
            **content,
            facts=tuple(selected_facts),
            intelligence_events=tuple(selected_intelligence),
            omitted_fact_revision_ids=tuple(sorted(omitted_facts)),
            omitted_intelligence_event_refs=tuple(sorted(omitted_intelligence)),
            previous_context=previous_context,
        )
        if (
            len(canonical_json(decision_packet_analysis_projection(candidate)))
            <= maximum_analysis_characters
        ):
            return candidate
        if _trim_one_packet_input_for_capacity(
            selected_facts=selected_facts,
            selected_intelligence=selected_intelligence,
            omitted_facts=omitted_facts,
            omitted_intelligence=omitted_intelligence,
        ):
            continue
        raise DecisionPacketCapacityError(
            "DecisionPacket final verified projection exceeds maximum_packet_characters"
        )


def _decision_packet_content_hash(packet: DecisionPacket) -> str:
    return content_hash(
        packet.model_dump(
            mode="json",
            exclude={"packet_id", "content_hash"},
        )
    )


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
        account: AccountSnapshot | None = None,
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
        visible_by_id = {item.fact.revision_id: item for item in facts}
        reviewed_evidence_ids = {
            evidence_id for review in ordered_reviews for evidence_id in review.evidence_ids
        }
        direct_fact_ids = tuple(
            sorted(
                {
                    *(fact_id for delta in ordered_deltas for fact_id in delta.fact_revision_ids),
                    *(item for item in reviewed_evidence_ids if item in visible_by_id),
                }
            )
        )
        missing_fact_ids = tuple(
            fact_id for fact_id in direct_fact_ids if fact_id not in visible_by_id
        )
        selected, omitted, fact_candidates = self._select_facts(
            mandate=mandate,
            facts=facts,
            direct_fact_ids=frozenset(direct_fact_ids),
            as_of=state.as_of,
        )
        direct_event_refs = frozenset(
            {
                *(
                    event_ref
                    for delta in ordered_deltas
                    for event_ref in delta.intelligence_event_refs
                ),
                *(
                    content_hash(event)
                    for event in intelligence_events
                    if event.evidence_id in reviewed_evidence_ids
                ),
            }
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
            for item in mandate.observation_assets
        )
        derivative_states = tuple(
            self._derivative_state(item)
            for item in sorted(derivatives, key=lambda value: value.asset)
        )
        required_views = tuple(
            RequiredView(asset=item.asset, horizon_minutes=horizon)
            for item in mandate.observation_assets
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
            "trigger_ids": trigger_ids,
            "mandate_exposures": mandate.mandate_exposures,
            "required_views": required_views,
            "portfolio": (self._portfolio_state(account) if account is not None else None),
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
                if self._refill_one_compact_fact(
                    payload=payload,
                    candidates=fact_candidates,
                    selected_facts=selected_facts,
                    selected_intelligence=selected_intelligence,
                    omitted_facts=omitted_facts,
                    omitted_intelligence=omitted_intelligence,
                ):
                    continue
                return packet
            # The selector protects directly triggered evidence and preserves a
            # causal baseline. A previous WorldModel can carry unchanged typed
            # state; an initial baseline cannot.
            if _trim_one_packet_input_for_capacity(
                selected_facts=selected_facts,
                selected_intelligence=selected_intelligence,
                omitted_facts=omitted_facts,
                omitted_intelligence=omitted_intelligence,
            ):
                continue
            raise DecisionPacketCapacityError(
                "DecisionPacket causal baseline and directly triggered content "
                "exceed maximum_packet_characters"
            )

    def _refill_one_compact_fact(
        self,
        *,
        payload: dict[str, object],
        candidates: tuple[PacketFact, ...],
        selected_facts: list[PacketFact],
        selected_intelligence: list[PacketIntelligenceEvent],
        omitted_facts: set[str],
        omitted_intelligence: set[str],
    ) -> bool:
        """Use final model-visible cost to fill capacity left by raw preselection.

        ``maximum_fact_characters`` bounds the verbose auditable Packet before
        projection.  Continuous facts are sent as compact typed state, so that
        intermediate bound can reject a useful state that still fits the real
        Codex input.  Reconsider candidates in the original causal order after
        the final projection is known; never replace an existing higher-ranked
        input or exceed the fact-count contract.
        """

        if len(selected_facts) >= self._policy.maximum_facts:
            return False
        selected_ids = {item.revision_id for item in selected_facts}
        for candidate in candidates:
            if (
                candidate.revision_id in selected_ids
                or candidate.fact_type not in CONTINUOUS_CONTEXT_FACT_TYPES
            ):
                continue
            trial_ids = {*selected_ids, candidate.revision_id}
            trial_facts = [item for item in candidates if item.revision_id in trial_ids]
            trial_omitted = omitted_facts - {candidate.revision_id}
            trial_payload = {
                **payload,
                "facts": tuple(trial_facts),
                "intelligence_events": tuple(selected_intelligence),
                "omitted_fact_revision_ids": tuple(sorted(trial_omitted)),
                "omitted_intelligence_event_refs": tuple(sorted(omitted_intelligence)),
            }
            trial = DecisionPacket.create(**trial_payload)
            if (
                len(canonical_json(decision_packet_analysis_projection(trial)))
                > self._policy.maximum_packet_characters
            ):
                continue
            selected_facts[:] = trial_facts
            omitted_facts.discard(candidate.revision_id)
            return True
        return False

    def _validate_inputs(
        self,
        *,
        mandate: AnalysisMandate,
        state: StateSnapshot,
        deltas: tuple[MaterialDelta, ...],
        review_requests: tuple[PacketReviewRequest, ...],
        facts: tuple[VisibleFact, ...],
        intelligence_events: tuple[IntelligenceEvent, ...],
        account: AccountSnapshot | None,
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
        if account is None:
            if state.account_snapshot_ref is not None:
                raise ValueError("StateSnapshot 有账户引用但缺少账户事实")
        else:
            if account.as_of > state.as_of or account.observed_at > state.as_of:
                raise ValueError("账户事实晚于 StateSnapshot as_of")
            if state.account_snapshot_ref != content_hash(account):
                raise ValueError("账户事实与 StateSnapshot account_snapshot_ref 不一致")
        symbols = tuple(item.market_symbol for item in mandate.observation_assets)
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
            set(derivative_assets) != {item.asset for item in mandate.observation_assets}
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
                event.attention_priority < self._policy.minimum_background_attention_priority
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
                -item.attention_priority,
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
                if evidence_ref in direct_event_refs:
                    raise DecisionPacketCapacityError(
                        "direct intelligence events exceed intelligence capacity"
                    )
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
                    # Frozen packet field retained for historical schema/hash
                    # compatibility; its value is discovery priority, not impact.
                    impact=event.attention_priority,
                    source_reliability=event.source_reliability,
                    novelty=event.novelty,
                    prompt_injection_suspected=True,
                    directly_triggered=evidence_ref in direct_event_refs,
                    directional_support_eligible=self.intelligence_directional_support_eligible(
                        event
                    ),
                )
            )
            used_characters += character_cost
        return tuple(selected), tuple(sorted(omitted))

    def intelligence_directional_support_eligible(
        self,
        event: IntelligenceEvent,
    ) -> bool:
        """Keep weak leads visible to a triggered review without promoting them.

        Direct triggering is a latency decision, not an epistemic promotion.
        Evidence eligibility is frozen by the source/enrichment contract and is
        never reconstructed from discovery rank or source reliability here.
        """

        return event.directional_support_eligible

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
        return context.model_copy(update=update)

    def _select_facts(
        self,
        *,
        mandate: AnalysisMandate,
        facts: tuple[VisibleFact, ...],
        direct_fact_ids: frozenset[str],
        as_of: datetime,
    ) -> tuple[
        tuple[PacketFact, ...],
        tuple[str, ...],
        tuple[PacketFact, ...],
    ]:
        relevant_assets = {item.asset for item in mandate.observation_assets}
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
                or _is_durable_policy_state(item)
                or (
                    item.fact.decision_materiality == FactDecisionMateriality.CANDIDATE
                    and distance <= self._policy.maximum_calendar_context_distance_seconds
                )
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
                causal_channel_rank(item),
                item.fact.fact_type not in _RESULT_CONTEXT_FACT_TYPES,
                item.fact.fact_type not in CONTINUOUS_CONTEXT_FACT_TYPES,
                not _is_durable_policy_state(item),
                item.fact.fact_type not in _CALENDAR_CONTEXT_FACT_TYPES,
                _SOURCE_RANK[item.highest_source_tier],
                item.fact.status.value != "ACTIVE",
                abs(((item.fact.event_time or item.fact.observed_at) - as_of).total_seconds()),
                -item.fact.observed_at.timestamp(),
                item.fact.revision_id,
            )
        )
        # Calendar, result and candidate streams can contain many durable records
        # of one semantic type. One nearest/current representative is sufficient
        # for background context; directly triggered facts are never collapsed.
        # Repeated old records would evict orthogonal liquidity/flow states while
        # adding no new causal channel.
        deduplicated: list[VisibleFact] = []
        represented_context_types: set[str] = set()
        for item in eligible:
            is_collapsible_context = (
                item.fact.fact_type in _EXTENDED_CONTEXT_FACT_TYPES
                or item.fact.decision_materiality == FactDecisionMateriality.CANDIDATE
                or _is_durable_policy_state(item)
            ) and item.fact.revision_id not in direct_fact_ids
            if is_collapsible_context and item.fact.fact_type in represented_context_types:
                omitted.append(item.fact.revision_id)
                continue
            deduplicated.append(item)
            if is_collapsible_context:
                represented_context_types.add(item.fact.fact_type)
        eligible = deduplicated
        # Preserve causal coverage inside each epistemic class. The mandate order
        # ranks channels before fact-type conveniences; otherwise continuous
        # downstream snapshots can evict a fresh official macro result even when
        # the mandate explicitly needs that upstream channel. Fact type and source
        # tier only rank comparable evidence within a channel. Repeated facts from
        # one channel remain available in later rounds.
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
        candidates: list[tuple[PacketFact, int]] = []
        for item in eligible:
            headline, headline_suspicious = sanitize_external_text(
                item.fact.headline,
                maximum_length=min(240, self._policy.maximum_characters_per_fact),
            )
            claim, claim_suspicious = sanitize_external_text(
                item.fact.claim,
                maximum_length=self._policy.maximum_characters_per_fact,
            )
            required = item.fact.revision_id in direct_fact_ids
            candidates.append(
                (
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
                            item.prompt_injection_suspected
                            or headline_suspicious
                            or claim_suspicious
                        ),
                        directly_triggered=required,
                    ),
                    len(headline) + len(claim),
                )
            )
        selected: list[PacketFact] = []
        used_characters = 0
        for candidate, character_cost in candidates:
            required = candidate.directly_triggered
            if len(selected) >= self._policy.maximum_facts or (
                used_characters + character_cost > self._policy.maximum_fact_characters
            ):
                if required:
                    raise DecisionPacketCapacityError("direct facts exceed fact character capacity")
                omitted.append(candidate.revision_id)
                continue
            selected.append(candidate)
            used_characters += character_cost
        return (
            tuple(selected),
            tuple(sorted(omitted)),
            tuple(item for item, _cost in candidates),
        )

    @staticmethod
    def _asset_state(
        *,
        asset: ObservationAsset,
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
            cross_venue_observed_at=snapshot.cross_venue_observed_at,
            spot_venue_count=snapshot.spot_venue_count,
            spot_mid_range_bps=snapshot.spot_mid_range_bps,
            reference_spot_mid_deviation_bps=(snapshot.reference_spot_mid_deviation_bps),
            widest_spot_spread_bps=snapshot.widest_spot_spread_bps,
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
