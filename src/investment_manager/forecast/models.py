from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from investment_manager.execution.models import Side
from investment_manager.kernel.identity import SHA256_PATTERN, content_hash, stable_id
from investment_manager.kernel.time import optional_utc, require_utc
from investment_manager.kernel.types import FrozenModel, Money, PositiveDecimal
from investment_manager.market.models import InstrumentId, InstrumentProduct


class ExposureDirection(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class ForecastQuantityMode(StrEnum):
    INDEPENDENT_NOTIONAL = "INDEPENDENT_NOTIONAL"
    SAME_BASE_QUANTITY = "SAME_BASE_QUANTITY"


class ForecastLeg(FrozenModel):
    instrument: InstrumentId
    direction: ExposureDirection
    gross_weight: Decimal = Field(gt=0, le=1)

    @model_validator(mode="after")
    def directly_short_spot_is_not_a_supported_leg(self):
        if (
            self.instrument.product == InstrumentProduct.SPOT
            and self.direction == ExposureDirection.SHORT
        ):
            raise ValueError("Spot ForecastLeg 不能表达未建模的直接做空")
        return self


class ForecastTarget(FrozenModel):
    """Normalized return object; Portfolio, not Forecast, decides its capital size."""

    target_id: str = Field(min_length=1)
    legs: tuple[ForecastLeg, ...] = Field(min_length=1)
    quantity_mode: ForecastQuantityMode = ForecastQuantityMode.INDEPENDENT_NOTIONAL

    @model_validator(mode="after")
    def legs_and_identity_must_be_canonical(self):
        keys = tuple(item.instrument.key for item in self.legs)
        if tuple(sorted(set(keys))) != keys:
            raise ValueError("ForecastTarget legs 必须按 Instrument 唯一且排序")
        if sum((item.gross_weight for item in self.legs), Decimal("0")) != Decimal("1"):
            raise ValueError("ForecastTarget gross_weight 绝对权重之和必须为 1")
        if self.quantity_mode == ForecastQuantityMode.SAME_BASE_QUANTITY and (
            len(self.legs) < 2
            or len({item.instrument.base_asset for item in self.legs}) != 1
            or len({item.instrument.contract_multiplier for item in self.legs}) != 1
            or len({item.gross_weight for item in self.legs}) != 1
        ):
            raise ValueError("SAME_BASE_QUANTITY 要求同基础资产、乘数和等权多腿")
        expected = self.identity_for(self.legs, self.quantity_mode)
        if self.target_id != expected:
            raise ValueError("ForecastTarget target_id 与规范化 Leg 内容不一致")
        return self

    @staticmethod
    def identity_for(
        legs: tuple[ForecastLeg, ...],
        quantity_mode: ForecastQuantityMode = ForecastQuantityMode.INDEPENDENT_NOTIONAL,
    ) -> str:
        return stable_id(
            "forecast_target",
            content_hash(
                {
                    "legs": [item.model_dump(mode="json") for item in legs],
                    "quantity_mode": quantity_mode.value,
                }
            ),
        )

    @classmethod
    def create(
        cls,
        legs: tuple[ForecastLeg, ...],
        *,
        quantity_mode: ForecastQuantityMode = ForecastQuantityMode.INDEPENDENT_NOTIONAL,
    ) -> ForecastTarget:
        ordered = tuple(sorted(legs, key=lambda item: item.instrument.key))
        return cls(
            target_id=cls.identity_for(ordered, quantity_mode),
            legs=ordered,
            quantity_mode=quantity_mode,
        )

    @classmethod
    def single_long(cls, instrument: InstrumentId) -> ForecastTarget:
        return cls.create(
            (
                ForecastLeg(
                    instrument=instrument,
                    direction=ExposureDirection.LONG,
                    gross_weight=Decimal("1"),
                ),
            )
        )


class ForecastReferencePrice(FrozenModel):
    instrument_id: str = Field(min_length=1)
    price: PositiveDecimal


class DirectionalView(StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    UNCERTAIN = "UNCERTAIN"


class ForecastOutcomeStatus(StrEnum):
    SETTLED = "SETTLED"
    ABSTAINED = "ABSTAINED"
    UNSCORABLE = "UNSCORABLE"


class EdgeCalibration(FrozenModel):
    """Point-in-time-safe gross-edge calibration for one forecast producer scope."""

    calibration_id: str
    producer_id: str
    producer_version: str
    symbol: str
    side: Side
    horizon_minutes: int = Field(gt=0)
    expected_gross_bps: Decimal
    conservative_gross_bps: Decimal
    sample_size: int = Field(gt=0)
    non_overlapping_sample_size: int = Field(gt=0)
    training_start: datetime
    training_end: datetime
    published_at: datetime
    valid_from: datetime
    valid_until: datetime
    evaluation_version: str
    source_calibration_ref: str
    source_execution_policy_version: str
    source_frequency_policy_version: str
    method_version: str
    dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    _utc_training_start = field_validator("training_start")(require_utc)
    _utc_training_end = field_validator("training_end")(require_utc)
    _utc_published_at = field_validator("published_at")(require_utc)
    _utc_valid_from = field_validator("valid_from")(require_utc)
    _utc_valid_until = field_validator("valid_until")(require_utc)

    @model_validator(mode="after")
    def evidence_and_time_ranges_must_be_consistent(self):
        if self.non_overlapping_sample_size > self.sample_size:
            raise ValueError("非重叠校准样本不能超过原始样本")
        if self.conservative_gross_bps > self.expected_gross_bps:
            raise ValueError("保守毛优势不能高于均值估计")
        if not (
            self.training_start
            < self.training_end
            <= self.published_at
            <= self.valid_from
            < self.valid_until
        ):
            raise ValueError("校准训练、发布和有效时间顺序非法")
        expected_hash = content_hash(self.model_dump(mode="json", exclude={"artifact_hash"}))
        if self.artifact_hash != expected_hash:
            raise ValueError("校准制品哈希与内容不一致")
        return self

    @property
    def scope(self) -> tuple[str, str, str, Side, int]:
        return (
            self.producer_id,
            self.producer_version,
            self.symbol,
            self.side,
            self.horizon_minutes,
        )


class PricedState(StrEnum):
    NOT_PRICED = "NOT_PRICED"
    PARTIAL = "PARTIAL"
    MOSTLY_PRICED = "MOSTLY_PRICED"
    UNKNOWN = "UNKNOWN"


class AssessmentUncertainty(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


class ContextDriverStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    INFERRED = "INFERRED"
    UNVERIFIED = "UNVERIFIED"


class ContextEventImpactState(StrEnum):
    ACTIVE = "ACTIVE"
    STALE = "STALE"


class ContextAssessmentSchemaVersion(StrEnum):
    """Explicitly separates immutable historical payloads from current writes."""

    LEGACY = "legacy-context-assessment-v1"
    WORLD_MODEL_V1 = "world-model-assessment-v1"
    WORLD_MODEL_V2 = "world-model-assessment-v2"


class ContextMechanismRelationship(StrEnum):
    SUPPORTS = "SUPPORTS"
    OFFSETS = "OFFSETS"
    THREATENS = "THREATENS"
    ALTERNATIVE = "ALTERNATIVE"


class ContextTransmissionStage(StrEnum):
    PENDING = "PENDING"
    PROPAGATING = "PROPAGATING"
    PRICED = "PRICED"
    REVERSING = "REVERSING"


class ContextPredicateOperator(StrEnum):
    GT = "GT"
    GTE = "GTE"
    LT = "LT"
    LTE = "LTE"
    BETWEEN = "BETWEEN"


class ContextHypothesisRole(StrEnum):
    PRIMARY = "PRIMARY"
    ALTERNATIVE = "ALTERNATIVE"
    TAIL_RISK = "TAIL_RISK"


class ContextCapitalEffect(StrEnum):
    """Research-only effect relative to Program Base; never capital authority."""

    SUPPORT = "SUPPORT"
    NEUTRAL = "NEUTRAL"
    CAUTION = "CAUTION"
    OPPOSE = "OPPOSE"
    INSUFFICIENT = "INSUFFICIENT"


class ContextCausalNode(FrozenModel):
    statement: str = Field(min_length=1, max_length=600)
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def evidence_must_be_unique(self):
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("因果节点不能重复引用证据")
        return self


class ContextVerificationPredicate(FrozenModel):
    operator: ContextPredicateOperator
    value: Decimal
    upper_value: Decimal | None = None
    persistence_observations: int = Field(default=1, ge=1, le=24)

    @model_validator(mode="after")
    def range_operator_must_have_a_valid_upper_bound(self):
        if self.operator == ContextPredicateOperator.BETWEEN:
            if self.upper_value is None or self.upper_value <= self.value:
                raise ValueError("BETWEEN 验证条件必须提供更大的 upper_value")
        elif self.upper_value is not None:
            raise ValueError("只有 BETWEEN 验证条件可以提供 upper_value")
        return self


class ContextVerificationTest(FrozenModel):
    """A deterministic, point-in-time-settleable mechanism test."""

    feature_selector: str = Field(min_length=1, max_length=240)
    evaluation_window_minutes: int = Field(gt=0, le=525_600)
    supports_predicate: ContextVerificationPredicate
    contradicts_predicate: ContextVerificationPredicate

    @model_validator(mode="after")
    def predicates_must_differ(self):
        if self.supports_predicate == self.contradicts_predicate:
            raise ValueError("支持与反驳条件不能相同")
        return self


class ContextVerificationMatch(StrEnum):
    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"
    NEITHER = "NEITHER"
    AMBIGUOUS = "AMBIGUOUS"


class ContextVerificationResolution(StrEnum):
    PENDING = "PENDING"
    SUPPORTED = "SUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    AMBIGUOUS = "AMBIGUOUS"


class ContextMechanismObservation(FrozenModel):
    """One immutable program evaluation of a WorldModel verification test."""

    observation_id: str = Field(min_length=1)
    assessment_id: str = Field(min_length=1)
    mechanism_id: str = Field(min_length=1)
    test_id: str = Field(min_length=1)
    test_contract_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    packet_id: str = Field(min_length=1)
    feature_selector: str = Field(min_length=1, max_length=240)
    observed_at: datetime
    value: Decimal
    match: ContextVerificationMatch
    support_streak: int = Field(ge=0)
    contradiction_streak: int = Field(ge=0)
    resolution: ContextVerificationResolution

    _utc_observed_at = field_validator("observed_at")(require_utc)

    @model_validator(mode="after")
    def identity_and_resolution_are_consistent(self):
        if self.match == ContextVerificationMatch.SUPPORTS:
            if self.support_streak < 1 or self.contradiction_streak != 0:
                raise ValueError("支持观测的连续计数不一致")
        elif self.match == ContextVerificationMatch.CONTRADICTS:
            if self.contradiction_streak < 1 or self.support_streak != 0:
                raise ValueError("反驳观测的连续计数不一致")
        elif self.support_streak or self.contradiction_streak:
            raise ValueError("非单向观测不得延续连续计数")
        identity = (
            self.assessment_id,
            self.mechanism_id,
            self.test_id,
            self.packet_id,
            self.observed_at,
            str(self.value),
            self.match,
            self.support_streak,
            self.contradiction_streak,
            self.resolution,
        )
        expected = (
            stable_id(
                "world_mechanism_observation",
                *identity,
                self.test_contract_hash,
            )
            if self.test_contract_hash is not None
            else stable_id("world_mechanism_observation", *identity)
        )
        if self.observation_id != expected:
            raise ValueError("世界机制观测 ID 与内容不一致")
        return self


class ContextMechanism(FrozenModel):
    """One parallel causal force contributing to the current synthesis."""

    mechanism_id: str = Field(min_length=1)
    continuity_ref: str | None = Field(default=None, min_length=1)
    relationship: ContextMechanismRelationship
    claim: str = Field(min_length=1, max_length=1_200)
    horizon_hours: int = Field(gt=0, le=17_520)
    causal_chain: tuple[ContextCausalNode, ...] = Field(min_length=2)
    transmission_stage: ContextTransmissionStage
    conflicting_evidence_ids: tuple[str, ...] = ()
    verification_tests: tuple[ContextVerificationTest, ...] = Field(min_length=1)
    invalidation_conditions: tuple[str, ...] = Field(min_length=1)
    next_review_at: datetime

    _utc_next_review_at = field_validator("next_review_at")(require_utc)

    @model_validator(mode="after")
    def references_and_conditions_must_be_unique(self):
        if len(set(self.conflicting_evidence_ids)) != len(self.conflicting_evidence_ids):
            raise ValueError("世界机制不能重复引用反向证据")
        if len(set(self.invalidation_conditions)) != len(self.invalidation_conditions):
            raise ValueError("世界机制不能重复失效条件")
        selectors = tuple(item.feature_selector for item in self.verification_tests)
        if len(set(selectors)) != len(selectors):
            raise ValueError("同一世界机制不能重复验证同一特征")
        return self


class ContextHypothesis(FrozenModel):
    """A falsifiable explanation of the current world, not a price forecast."""

    hypothesis_id: str = Field(min_length=1)
    continuity_ref: str | None = Field(default=None, min_length=1)
    role: ContextHypothesisRole
    claim: str = Field(min_length=1, max_length=1_000)
    horizon_hours: int = Field(gt=0, le=4_380)
    causal_chain: tuple[ContextCausalNode, ...] = Field(min_length=2, max_length=5)
    conflicting_evidence_ids: tuple[str, ...] = Field(default=(), max_length=12)
    next_observation: str = Field(min_length=1, max_length=600)
    invalidation_conditions: tuple[str, ...] = Field(min_length=1, max_length=5)
    next_review_at: datetime

    _utc_next_review_at = field_validator("next_review_at")(require_utc)

    @model_validator(mode="after")
    def references_and_invalidations_must_be_unique(self):
        if len(set(self.conflicting_evidence_ids)) != len(self.conflicting_evidence_ids):
            raise ValueError("世界假设不能重复引用反向证据")
        if len(set(self.invalidation_conditions)) != len(self.invalidation_conditions):
            raise ValueError("世界假设不能重复失效条件")
        return self


class ContextCapitalImplication(FrozenModel):
    """A testable research recommendation relative to one program objective."""

    objective_id: str = Field(min_length=1)
    effect: ContextCapitalEffect
    incremental_reason: str = Field(min_length=1, max_length=800)
    transmission: str = Field(min_length=1, max_length=1_200)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=12)
    invalidation_conditions: tuple[str, ...] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def evidence_and_invalidations_must_be_canonical(self):
        if (
            self.effect
            in {
                ContextCapitalEffect.SUPPORT,
                ContextCapitalEffect.CAUTION,
                ContextCapitalEffect.OPPOSE,
            }
            and not self.evidence_ids
        ):
            raise ValueError("非中性资本含义必须引用当前证据")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("资本含义不能重复引用证据")
        if len(set(self.invalidation_conditions)) != len(self.invalidation_conditions):
            raise ValueError("资本含义不能重复失效条件")
        return self


class ContextDecisionBlocker(FrozenModel):
    """Only a missing observation that can change the named capital action."""

    question: str = Field(min_length=1, max_length=500)
    action_if_yes: str = Field(min_length=1, max_length=500)
    action_if_no: str = Field(min_length=1, max_length=500)
    observation_needed: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def outcomes_must_change_the_action(self):
        if self.action_if_yes == self.action_if_no:
            raise ValueError("决策阻断项的两种观测结果必须改变资本动作")
        return self


class ContextCapitalRelevanceStatus(StrEnum):
    """Research stance relative to the program decision, never capital authority."""

    BASE_UNCHANGED = "BASE_UNCHANGED"
    ENTRY_VETO_CANDIDATE = "ENTRY_VETO_CANDIDATE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class ContextCapitalRelevance(FrozenModel):
    objective_id: str = Field(min_length=1)
    status: ContextCapitalRelevanceStatus
    thesis: str = Field(min_length=1, max_length=800)
    transmission: str = Field(min_length=1, max_length=1_200)
    evidence_ids: tuple[str, ...] = ()
    invalidation_conditions: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def evidence_and_invalidation_are_canonical(self):
        if (
            self.status == ContextCapitalRelevanceStatus.ENTRY_VETO_CANDIDATE
            and not self.evidence_ids
        ):
            raise ValueError("入场否决候选必须引用当前证据")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("资本相关性不能重复引用证据")
        if len(set(self.invalidation_conditions)) != len(self.invalidation_conditions):
            raise ValueError("资本相关性不能重复失效条件")
        return self


class ContextEventReference(FrozenModel):
    """Derived event relevance in one immutable world-cognition snapshot."""

    evidence_id: str = Field(pattern=SHA256_PATTERN)
    source: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=1_000)
    event_time: datetime
    impact_state: ContextEventImpactState
    rationale: str = Field(min_length=1, max_length=600)
    stale_at: datetime | None = None

    _utc_event_time = field_validator("event_time")(require_utc)
    _utc_stale_at = field_validator("stale_at")(optional_utc)

    @model_validator(mode="after")
    def stale_time_must_match_state(self):
        if self.impact_state == ContextEventImpactState.STALE:
            if self.stale_at is None:
                raise ValueError("过时事件引用必须记录首次过时时间")
        elif self.stale_at is not None:
            raise ValueError("仍有效事件引用不得记录过时时间")
        return self


class ForecastRole(StrEnum):
    PROGRAM_BASE = "PROGRAM_BASE"
    AI_EVENT = "AI_EVENT"
    AI_ADJUSTED = "AI_ADJUSTED"


class ForecastKind(StrEnum):
    BASE = "BASE"
    CALIBRATED = "CALIBRATED"


class ContextView(FrozenModel):
    asset: str = Field(min_length=1)
    horizon_minutes: int = Field(gt=0)
    direction: DirectionalView
    already_priced: PricedState
    uncertainty: AssessmentUncertainty
    evidence_ids: tuple[str, ...] = ()
    invalidation_conditions: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def refs_must_be_unique(self):
        if self.direction != DirectionalView.UNCERTAIN and not self.evidence_ids:
            raise ValueError("方向性 ContextView 必须引用证据")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("ContextView 不能重复引用证据")
        if len(set(self.invalidation_conditions)) != len(self.invalidation_conditions):
            raise ValueError("ContextView 不能重复失效条件")
        return self


class ContextDriver(FrozenModel):
    """One decision-relevant driver with an explicit epistemic boundary."""

    statement: str = Field(min_length=1, max_length=600)
    status: ContextDriverStatus
    transmission: str = Field(min_length=1, max_length=1_200)
    evidence_ids: tuple[str, ...] = ()
    invalidation_conditions: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def evidence_and_invalidation_must_be_unambiguous(self):
        if self.status != ContextDriverStatus.UNVERIFIED and not self.evidence_ids:
            raise ValueError("已确认事实或有证据推断必须引用证据")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("ContextDriver 不能重复引用证据")
        if len(set(self.invalidation_conditions)) != len(self.invalidation_conditions):
            raise ValueError("ContextDriver 不能重复失效条件")
        return self


class ContextAssessment(FrozenModel):
    schema_version: ContextAssessmentSchemaVersion = ContextAssessmentSchemaVersion.LEGACY
    assessment_id: str = Field(min_length=1)
    analysis_scope: str = Field(min_length=1)
    mandate_version: str = Field(min_length=1)
    as_of: datetime
    available_at: datetime
    analysis_behavior_hash: str = Field(pattern=SHA256_PATTERN)
    decision_packet_hash: str = Field(pattern=SHA256_PATTERN)
    trigger_ids: tuple[str, ...] = Field(min_length=1)
    # The fields below are immutable legacy read compatibility. New assessments
    # use hypotheses/capital_implication/decision_blockers exclusively.
    market_mechanism: str | None = Field(default=None, min_length=1, max_length=2_000)
    # New assessments anchor the mechanism itself. Empty remains readable for
    # historical payloads created before the field existed.
    mechanism_evidence_ids: tuple[str, ...] = ()
    # Empty is a first-class result when no baseline-changing driver is known.
    # Market state must not be promoted merely to fill a narrative slot.
    drivers: tuple[ContextDriver, ...] = ()
    event_references: tuple[ContextEventReference, ...] = ()
    capital_relevance: ContextCapitalRelevance | None = None
    # Directional views remain readable for immutable historical assessments.
    # Capital-objective behavior no longer produces them.
    views: tuple[ContextView, ...] = ()
    contradictions: tuple[str, ...] = ()
    data_gaps: tuple[str, ...] = ()
    hypotheses: tuple[ContextHypothesis, ...] = Field(default=(), max_length=3)
    capital_implication: ContextCapitalImplication | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    decision_blockers: tuple[ContextDecisionBlocker, ...] = Field(
        default=(),
        max_length=2,
    )
    synthesis: str | None = Field(default=None, min_length=1, max_length=2_000)
    synthesis_horizon_hours: int | None = Field(default=None, gt=0, le=17_520)
    mechanisms: tuple[ContextMechanism, ...] = ()

    _utc_as_of = field_validator("as_of")(require_utc)
    _utc_available_at = field_validator("available_at")(require_utc)

    @model_validator(mode="after")
    def completion_and_view_identity_must_be_unambiguous(self):
        if self.available_at < self.as_of:
            raise ValueError("ContextAssessment available_at 不能早于 as_of")
        view_keys = tuple((item.asset, item.horizon_minutes) for item in self.views)
        if tuple(sorted(set(view_keys))) != view_keys:
            raise ValueError("ContextView 必须按资产/时域唯一且排序")
        if len(set(self.trigger_ids)) != len(self.trigger_ids):
            raise ValueError("ContextAssessment 不能重复引用触发")
        if len(set(self.mechanism_evidence_ids)) != len(self.mechanism_evidence_ids):
            raise ValueError("ContextAssessment 机制证据不能重复")
        event_ids = tuple(item.evidence_id for item in self.event_references)
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("ContextAssessment 不能重复引用事件")
        if self.schema_version == ContextAssessmentSchemaVersion.LEGACY:
            if self.market_mechanism is None:
                raise ValueError("历史 ContextAssessment 必须包含 market_mechanism")
            if self.hypotheses or self.capital_implication or self.decision_blockers:
                raise ValueError("历史 ContextAssessment 不得混入新世界模型字段")
            return self
        if self.schema_version == ContextAssessmentSchemaVersion.WORLD_MODEL_V2:
            if any(
                (
                    self.market_mechanism is not None,
                    bool(self.mechanism_evidence_ids),
                    bool(self.drivers),
                    self.capital_relevance is not None,
                    bool(self.views),
                    bool(self.contradictions),
                    bool(self.data_gaps),
                    bool(self.hypotheses),
                    self.capital_implication is not None,
                    bool(self.decision_blockers),
                )
            ):
                raise ValueError("WorldModel v2 不得混入历史或资本复核字段")
            if self.synthesis is None or self.synthesis_horizon_hours is None:
                raise ValueError("WorldModel v2 必须包含综合判断及其时域")
            if not self.mechanisms:
                raise ValueError("WorldModel v2 至少需要一个可验证机制")
            mechanism_ids = tuple(item.mechanism_id for item in self.mechanisms)
            if len(set(mechanism_ids)) != len(mechanism_ids):
                raise ValueError("WorldModel v2 不能重复 mechanism_id")
            if any(item.next_review_at <= self.as_of for item in self.mechanisms):
                raise ValueError("世界机制 next_review_at 必须晚于分析时点")
            return self
        if (
            self.synthesis is not None
            or self.synthesis_horizon_hours is not None
            or self.mechanisms
        ):
            raise ValueError("WorldModel v1 不得混入 v2 字段")
        if any(
            (
                self.market_mechanism is not None,
                bool(self.mechanism_evidence_ids),
                bool(self.drivers),
                self.capital_relevance is not None,
                bool(self.views),
                bool(self.contradictions),
                bool(self.data_gaps),
            )
        ):
            raise ValueError("新世界模型不得继续写入已废弃的段落式字段")
        if not self.hypotheses:
            raise ValueError("新世界模型至少需要一个可证伪假设")
        primary_count = sum(item.role == ContextHypothesisRole.PRIMARY for item in self.hypotheses)
        if primary_count != 1:
            raise ValueError("新世界模型必须且只能包含一个 PRIMARY 假设")
        hypothesis_ids = tuple(item.hypothesis_id for item in self.hypotheses)
        if len(set(hypothesis_ids)) != len(hypothesis_ids):
            raise ValueError("新世界模型不能重复 hypothesis_id")
        if any(item.next_review_at <= self.as_of for item in self.hypotheses):
            raise ValueError("世界假设 next_review_at 必须晚于分析时点")
        return self


class BaseForecast(FrozenModel):
    """Point-in-time thesis; valid_until gates entry, horizon gates settlement."""

    forecast_id: str = Field(min_length=1)
    producer_id: str = Field(min_length=1)
    producer_version: str = Field(min_length=1)
    forecast_family: str = Field(min_length=1)
    target: ForecastTarget
    horizon_minutes: int = Field(gt=0)
    direction: DirectionalView
    reference_prices: tuple[ForecastReferencePrice, ...] = Field(min_length=1)
    observed_at: datetime
    available_at: datetime
    valid_until: datetime
    raw_score: Decimal
    input_refs: tuple[str, ...] = Field(min_length=1)
    unknowns: tuple[str, ...] = ()

    _utc_observed_at = field_validator("observed_at")(require_utc)
    _utc_available_at = field_validator("available_at")(require_utc)
    _utc_valid_until = field_validator("valid_until")(require_utc)

    @model_validator(mode="after")
    def forecast_timing_and_refs_must_be_valid(self):
        if not self.observed_at <= self.available_at < self.valid_until:
            raise ValueError("BaseForecast 时间顺序非法")
        _require_complete_reference_prices(self.target, self.reference_prices)
        if len(set(self.input_refs)) != len(self.input_refs):
            raise ValueError("BaseForecast 不能重复引用输入")
        return self

    @property
    def economic_horizon_end(self) -> datetime:
        return self.available_at + timedelta(minutes=self.horizon_minutes)


class CalibratedForecast(FrozenModel):
    """Investable thesis; valid_until gates entry, horizon gates settlement."""

    forecast_id: str = Field(min_length=1)
    role: ForecastRole
    producer_id: str = Field(min_length=1)
    producer_version: str = Field(min_length=1)
    forecast_family: str = Field(min_length=1)
    target: ForecastTarget
    horizon_minutes: int = Field(gt=0)
    direction: DirectionalView
    reference_prices: tuple[ForecastReferencePrice, ...] = Field(min_length=1)
    expected_edge_half_life_seconds: int = Field(gt=0, le=31_536_000)
    available_at: datetime
    valid_until: datetime
    base_forecast_id: str | None = Field(default=None, min_length=1)
    assessment_id: str | None = Field(default=None, min_length=1)
    expected_gross_bps: Decimal
    conservative_gross_bps: Decimal
    dispersion_bps: Money
    calibration_ref: str = Field(min_length=1)
    calibration_sample_size: int = Field(gt=0)
    non_overlapping_sample_size: int = Field(gt=0)
    input_refs: tuple[str, ...] = Field(min_length=1)

    _utc_available_at = field_validator("available_at")(require_utc)
    _utc_valid_until = field_validator("valid_until")(require_utc)

    @model_validator(mode="after")
    def role_evidence_and_calibration_must_match(self):
        if self.available_at >= self.valid_until:
            raise ValueError("CalibratedForecast 必须在有效期前可用")
        if self.conservative_gross_bps > self.expected_gross_bps:
            raise ValueError("保守收益不能高于均值")
        if self.non_overlapping_sample_size > self.calibration_sample_size:
            raise ValueError("非重叠样本不能超过总样本")
        _require_complete_reference_prices(self.target, self.reference_prices)
        required_refs = {
            ForecastRole.PROGRAM_BASE: (True, False),
            ForecastRole.AI_EVENT: (False, True),
            ForecastRole.AI_ADJUSTED: (True, True),
        }
        needs_base, needs_assessment = required_refs[self.role]
        if (self.base_forecast_id is not None) != needs_base:
            raise ValueError("ForecastRole 与 base_forecast_id 不匹配")
        if (self.assessment_id is not None) != needs_assessment:
            raise ValueError("ForecastRole 与 assessment_id 不匹配")
        if len(set(self.input_refs)) != len(self.input_refs):
            raise ValueError("CalibratedForecast 不能重复引用输入")
        return self

    @property
    def economic_horizon_end(self) -> datetime:
        return self.available_at + timedelta(minutes=self.horizon_minutes)


def _require_complete_reference_prices(
    target: ForecastTarget,
    reference_prices: tuple[ForecastReferencePrice, ...],
) -> None:
    reference_ids = tuple(item.instrument_id for item in reference_prices)
    target_ids = tuple(item.instrument.key for item in target.legs)
    if reference_ids != target_ids:
        raise ValueError("Forecast 参考价必须逐 Leg 唯一、排序且完整")


class ForecastLegOutcome(FrozenModel):
    instrument_id: str = Field(min_length=1)
    direction: ExposureDirection
    gross_weight: Decimal = Field(gt=0, le=1)
    reference_price: PositiveDecimal
    exit_price: PositiveDecimal
    price_return_bps: Decimal
    funding_return_bps: Decimal = Decimal("0")
    funding_settlement_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def funding_refs_must_be_unique(self):
        if len(set(self.funding_settlement_ids)) != len(self.funding_settlement_ids):
            raise ValueError("ForecastLegOutcome 不能重复引用 Funding 结算")
        return self


class ForecastOutcome(FrozenModel):
    outcome_id: str = Field(min_length=1)
    forecast_id: str = Field(min_length=1)
    forecast_kind: ForecastKind
    producer_id: str = Field(min_length=1)
    producer_version: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    direction: DirectionalView
    horizon_minutes: int = Field(gt=0)
    evaluation_version: str = Field(min_length=1)
    status: ForecastOutcomeStatus
    available_at: datetime
    evaluation_at: datetime
    settled_at: datetime
    legs: tuple[ForecastLegOutcome, ...] = ()
    gross_target_return_bps: Decimal | None = None
    directional_return_bps: Decimal | None = None
    reason_code: str = Field(min_length=1)

    _utc_available_at = field_validator("available_at")(require_utc)
    _utc_evaluation_at = field_validator("evaluation_at")(require_utc)
    _utc_settled_at = field_validator("settled_at")(require_utc)

    @model_validator(mode="after")
    def identity_timing_and_returns_must_match(self):
        expected_id = stable_id(
            "forecast_outcome",
            self.forecast_id,
            self.evaluation_version,
        )
        if self.outcome_id != expected_id:
            raise ValueError("ForecastOutcome outcome_id 不匹配")
        if self.evaluation_at != self.available_at + timedelta(minutes=self.horizon_minutes):
            raise ValueError("ForecastOutcome 评价时间与预测周期不一致")
        if self.settled_at < self.evaluation_at:
            raise ValueError("ForecastOutcome 不能提前结算")
        if self.status == ForecastOutcomeStatus.UNSCORABLE:
            if self.legs or any(
                value is not None
                for value in (
                    self.gross_target_return_bps,
                    self.directional_return_bps,
                )
            ):
                raise ValueError("不可结算 ForecastOutcome 不能包含收益")
            return self
        if not self.legs or self.gross_target_return_bps is None:
            raise ValueError("可结算 ForecastOutcome 必须包含逐 Leg 收益")
        leg_ids = tuple(item.instrument_id for item in self.legs)
        if tuple(sorted(set(leg_ids))) != leg_ids:
            raise ValueError("ForecastOutcome Leg 必须唯一且排序")
        if sum(
            (item.gross_weight for item in self.legs),
            Decimal("0"),
        ) != Decimal("1"):
            raise ValueError("ForecastOutcome Leg 权重之和必须为 1")
        total = sum(
            (item.price_return_bps + item.funding_return_bps for item in self.legs),
            Decimal("0"),
        )
        if total != self.gross_target_return_bps:
            raise ValueError("ForecastOutcome 总收益与逐 Leg 收益不一致")
        if self.status == ForecastOutcomeStatus.ABSTAINED:
            if (
                self.direction != DirectionalView.UNCERTAIN
                or self.directional_return_bps is not None
            ):
                raise ValueError("只有 UNCERTAIN Forecast 可以记为 ABSTAINED")
            return self
        if self.direction == DirectionalView.UNCERTAIN:
            raise ValueError("UNCERTAIN Forecast 不能记为 SETTLED")
        expected_directional = (
            self.gross_target_return_bps
            if self.direction == DirectionalView.UP
            else -self.gross_target_return_bps
        )
        if self.directional_return_bps != expected_directional:
            raise ValueError("ForecastOutcome 方向收益不一致")
        return self
