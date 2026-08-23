"""Candidate-specific use of a WorldModel without granting AI trading authority."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from investment_manager.forecast.models import (
    BaseForecast,
    ContextAssessment,
    ContextAssessmentSchemaVersion,
    ContextCapitalEffect,
)
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel, UnitInterval


class OpportunityReviewInput(FrozenModel):
    """Frozen incremental question; BaseForecast is the sole opportunity fact."""

    review_id: str = Field(min_length=1)
    forecast: BaseForecast
    world_model: ContextAssessment
    estimated_variable_cost_bps: Decimal = Field(ge=0)
    baseline_net_edge_bps: Decimal
    portfolio_id: str = Field(min_length=1)
    account_snapshot_id: str = Field(min_length=1)
    account_equity: Decimal = Field(gt=0)
    created_at: datetime
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    _utc_created_at = field_validator("created_at")(require_utc)

    @classmethod
    def create(cls, **values: object) -> OpportunityReviewInput:
        payload = dict(values)
        digest = content_hash(payload)
        return cls(
            review_id=stable_id("opportunity_review", digest),
            content_hash=digest,
            **payload,
        )

    @model_validator(mode="after")
    def input_is_point_in_time_and_economically_consistent(self):
        if self.world_model.schema_version != ContextAssessmentSchemaVersion.WORLD_MODEL_V2:
            raise ValueError("机会复核只能消费 WorldModel v2")
        if self.world_model.available_at > self.created_at:
            raise ValueError("机会复核不能读取创建时尚不可见的世界模型")
        if self.forecast.available_at > self.created_at:
            raise ValueError("机会复核不能读取创建时尚不可见的 Forecast")
        if self.forecast.valid_until <= self.created_at:
            raise ValueError("机会复核不得处理已过入场有效期的 Forecast")
        expected_net = self.forecast.raw_score - self.estimated_variable_cost_bps
        if self.baseline_net_edge_bps != expected_net:
            raise ValueError("机会复核的程序净 Edge 与 Forecast/成本不一致")
        payload = self.model_dump(mode="json", exclude={"review_id", "content_hash"})
        if self.content_hash != content_hash(payload):
            raise ValueError("机会复核输入哈希不一致")
        if self.review_id != stable_id("opportunity_review", self.content_hash):
            raise ValueError("机会复核输入 ID 不一致")
        return self


class MechanismOpportunityEffect(StrEnum):
    SUPPORTS = "SUPPORTS"
    NEUTRAL = "NEUTRAL"
    CAUTIONS = "CAUTIONS"
    OPPOSES = "OPPOSES"


class MechanismOpportunityImpact(FrozenModel):
    mechanism_id: str = Field(min_length=1)
    effect: MechanismOpportunityEffect
    transmission_to_opportunity: str = Field(min_length=1, max_length=1_000)
    evidence_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def evidence_is_unique(self):
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("机会机制影响不能重复引用证据")
        if self.effect != MechanismOpportunityEffect.NEUTRAL and not self.evidence_ids:
            raise ValueError("非中性机会机制影响必须引用证据")
        return self


class OpportunityAssessmentDraft(FrozenModel):
    effect: ContextCapitalEffect
    incremental_reason: str = Field(min_length=1, max_length=1_000)
    mechanism_impacts: tuple[MechanismOpportunityImpact, ...] = Field(min_length=1)
    invalidation_conditions: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def impacts_are_unique(self):
        ids = tuple(item.mechanism_id for item in self.mechanism_impacts)
        if len(set(ids)) != len(ids):
            raise ValueError("机会复核不能重复评价同一机制")
        return self


class OpportunityReviewStructuredOutput(FrozenModel):
    opportunity_assessment: OpportunityAssessmentDraft


class OpportunityAssessment(FrozenModel):
    """Research-only incremental result, never an order or target."""

    assessment_id: str = Field(min_length=1)
    review_id: str = Field(min_length=1)
    opportunity_id: str = Field(min_length=1)
    world_model_id: str = Field(min_length=1)
    analysis_behavior_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    available_at: datetime
    effect: ContextCapitalEffect
    incremental_reason: str = Field(min_length=1, max_length=1_000)
    mechanism_impacts: tuple[MechanismOpportunityImpact, ...] = Field(min_length=1)
    invalidation_conditions: tuple[str, ...] = Field(min_length=1)

    _utc_available_at = field_validator("available_at")(require_utc)

    @model_validator(mode="after")
    def identity_is_reproducible(self):
        payload = self.model_dump(mode="json", exclude={"assessment_id"})
        if self.assessment_id != stable_id(
            "opportunity_assessment", content_hash(payload)
        ):
            raise ValueError("OpportunityAssessment ID 与内容不一致")
        return self


class ContextOverlayPolicy(FrozenModel):
    """Versioned research mapping. It can only preserve or reduce Program Base."""

    version: str = "context-overlay-research-v1"
    support_multiplier: UnitInterval = Decimal("1")
    neutral_multiplier: UnitInterval = Decimal("1")
    insufficient_multiplier: UnitInterval = Decimal("1")
    caution_multiplier: UnitInterval = Decimal("1")
    oppose_multiplier: UnitInterval = Decimal("0")


class OpportunityOverlayDecision(FrozenModel):
    decision_id: str = Field(min_length=1)
    opportunity_assessment_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    baseline_allocation_fraction: UnitInterval
    overlay_allocation_fraction: UnitInterval
    reason_code: str = Field(min_length=1)

    @model_validator(mode="after")
    def overlay_never_amplifies_baseline(self):
        if self.overlay_allocation_fraction > self.baseline_allocation_fraction:
            raise ValueError("未经晋升的 Context overlay 不得放大 Program Base")
        expected = stable_id(
            "opportunity_overlay_decision",
            self.opportunity_assessment_id,
            self.policy_version,
            str(self.baseline_allocation_fraction),
            str(self.overlay_allocation_fraction),
            self.reason_code,
        )
        if self.decision_id != expected:
            raise ValueError("Opportunity overlay decision ID 不一致")
        return self


def finalize_opportunity_assessment(
    *,
    output: OpportunityReviewStructuredOutput,
    review: OpportunityReviewInput,
    analysis_behavior_hash: str,
    available_at: datetime,
) -> OpportunityAssessment:
    available = require_utc(available_at)
    if available < review.created_at:
        raise ValueError("机会复核完成时间不能早于输入冻结时间")
    draft = output.opportunity_assessment
    mechanisms = {item.mechanism_id: item for item in review.world_model.mechanisms}
    impact_ids = {item.mechanism_id for item in draft.mechanism_impacts}
    unknown = tuple(sorted(impact_ids - mechanisms.keys()))
    if unknown:
        raise ValueError(f"机会复核引用未知世界机制: {unknown}")
    visible_evidence = {
        evidence_id
        for mechanism in mechanisms.values()
        for node in mechanism.causal_chain
        for evidence_id in node.evidence_ids
    } | {
        evidence_id
        for mechanism in mechanisms.values()
        for evidence_id in mechanism.conflicting_evidence_ids
    }
    cited = {
        evidence_id
        for impact in draft.mechanism_impacts
        for evidence_id in impact.evidence_ids
    }
    unknown_evidence = tuple(sorted(cited - visible_evidence))
    if unknown_evidence:
        raise ValueError(f"机会复核引用世界模型外证据: {unknown_evidence}")
    payload = {
        "review_id": review.review_id,
        "opportunity_id": review.forecast.forecast_id,
        "world_model_id": review.world_model.assessment_id,
        "analysis_behavior_hash": analysis_behavior_hash,
        "available_at": available,
        **draft.model_dump(),
    }
    return OpportunityAssessment(
        assessment_id=stable_id("opportunity_assessment", content_hash(payload)),
        **payload,
    )


def apply_context_overlay(
    assessment: OpportunityAssessment,
    *,
    baseline_allocation_fraction: Decimal,
    policy: ContextOverlayPolicy,
) -> OpportunityOverlayDecision:
    multiplier = {
        ContextCapitalEffect.SUPPORT: policy.support_multiplier,
        ContextCapitalEffect.NEUTRAL: policy.neutral_multiplier,
        ContextCapitalEffect.INSUFFICIENT: policy.insufficient_multiplier,
        ContextCapitalEffect.CAUTION: policy.caution_multiplier,
        ContextCapitalEffect.OPPOSE: policy.oppose_multiplier,
    }[assessment.effect]
    overlay = baseline_allocation_fraction * multiplier
    reason = f"CONTEXT_{assessment.effect.value}"
    decision_id = stable_id(
        "opportunity_overlay_decision",
        assessment.assessment_id,
        policy.version,
        str(baseline_allocation_fraction),
        str(overlay),
        reason,
    )
    return OpportunityOverlayDecision(
        decision_id=decision_id,
        opportunity_assessment_id=assessment.assessment_id,
        policy_version=policy.version,
        baseline_allocation_fraction=baseline_allocation_fraction,
        overlay_allocation_fraction=overlay,
        reason_code=reason,
    )
