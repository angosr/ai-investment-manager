from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from investment_manager.execution.models import Side
from investment_manager.kernel.identity import SHA256_PATTERN, content_hash, stable_id
from investment_manager.kernel.time import optional_utc, require_utc
from investment_manager.kernel.types import FrozenModel
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


class DirectionalView(StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    UNCERTAIN = "UNCERTAIN"


class AssessmentOutcomeStatus(StrEnum):
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


class ContextEventImpactState(StrEnum):
    ACTIVE = "ACTIVE"
    STALE = "STALE"


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
    verification_policy_version: str | None = Field(default=None, min_length=1)
    packet_id: str = Field(min_length=1)
    feature_selector: str = Field(min_length=1, max_length=240)
    feature_observation_ref: str | None = Field(default=None, min_length=1)
    feature_observed_at: datetime | None = None
    observed_at: datetime
    value: Decimal
    match: ContextVerificationMatch
    support_streak: int = Field(ge=0)
    contradiction_streak: int = Field(ge=0)
    resolution: ContextVerificationResolution

    _utc_observed_at = field_validator("observed_at")(require_utc)
    _utc_feature_observed_at = field_validator("feature_observed_at")(optional_utc)

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
        if self.feature_observation_ref is not None:
            identity = (
                *identity,
                self.feature_observation_ref,
                self.feature_observed_at,
            )
        expected = (
            stable_id(
                "world_mechanism_observation",
                *identity,
                self.test_contract_hash,
                self.verification_policy_version,
            )
            if self.verification_policy_version is not None
            else stable_id(
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


class ContextAssessment(FrozenModel):
    """The sole current WorldModel schema; historical shapes live only in archives."""

    schema_version: Literal["world-model-assessment-v2"] = "world-model-assessment-v2"
    assessment_id: str = Field(min_length=1)
    analysis_scope: str = Field(min_length=1)
    mandate_version: str = Field(min_length=1)
    as_of: datetime
    available_at: datetime
    analysis_behavior_hash: str = Field(pattern=SHA256_PATTERN)
    decision_packet_hash: str = Field(pattern=SHA256_PATTERN)
    trigger_ids: tuple[str, ...] = Field(min_length=1)
    event_references: tuple[ContextEventReference, ...] = ()
    synthesis: str = Field(min_length=1, max_length=2_000)
    synthesis_horizon_hours: int = Field(gt=0, le=17_520)
    mechanisms: tuple[ContextMechanism, ...] = Field(min_length=1)

    _utc_as_of = field_validator("as_of")(require_utc)
    _utc_available_at = field_validator("available_at")(require_utc)

    @model_validator(mode="after")
    def completion_and_identity_must_be_unambiguous(self):
        if self.available_at < self.as_of:
            raise ValueError("ContextAssessment available_at 不能早于 as_of")
        if len(set(self.trigger_ids)) != len(self.trigger_ids):
            raise ValueError("ContextAssessment 不能重复引用触发")
        event_ids = tuple(item.evidence_id for item in self.event_references)
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("ContextAssessment 不能重复引用事件")
        mechanism_ids = tuple(item.mechanism_id for item in self.mechanisms)
        if len(set(mechanism_ids)) != len(mechanism_ids):
            raise ValueError("WorldModel 不能重复 mechanism_id")
        if any(item.next_review_at <= self.as_of for item in self.mechanisms):
            raise ValueError("世界假设 next_review_at 必须晚于分析时点")
        return self
