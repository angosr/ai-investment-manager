from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from investment_manager.domain import DirectionalView
from investment_manager.kernel.identity import SHA256_PATTERN
from investment_manager.kernel.time import (
    optional_utc,
    require_utc,
)
from investment_manager.kernel.types import (
    FrozenModel,
    Money,
    PositiveDecimal,
)


class FactRevisionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    CANCELLED = "CANCELLED"
    RETRACTED = "RETRACTED"
    CONFLICTED = "CONFLICTED"


class DeltaCategory(StrEnum):
    FIRST_PARTY_FACT = "FIRST_PARTY_FACT"
    MARKET = "MARKET"
    DERIVATIVES = "DERIVATIVES"
    CROSS_ASSET = "CROSS_ASSET"
    PORTFOLIO = "PORTFOLIO"
    DATA_QUALITY = "DATA_QUALITY"


class Materiality(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


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


class ForecastRole(StrEnum):
    PROGRAM_BASE = "PROGRAM_BASE"
    AI_EVENT = "AI_EVENT"
    AI_ADJUSTED = "AI_ADJUSTED"


class CanonicalFactRevision(FrozenModel):
    fact_id: str = Field(min_length=1)
    revision_id: str = Field(min_length=1)
    previous_revision_id: str | None = Field(default=None, min_length=1)
    projection_version: str = Field(min_length=1)
    fact_type: str = Field(min_length=1, max_length=80)
    status: FactRevisionStatus
    event_time: datetime | None = None
    observed_at: datetime
    headline: str = Field(min_length=1, max_length=500)
    claim: str = Field(min_length=1, max_length=2_000)
    affected_assets: tuple[str, ...] = ()
    risk_factors: tuple[str, ...] = Field(min_length=1)
    source_observation_ids: tuple[str, ...] = Field(min_length=1)
    revision_hash: str = Field(pattern=SHA256_PATTERN)

    _utc_event_time = field_validator("event_time")(optional_utc)
    _utc_observed_at = field_validator("observed_at")(require_utc)

    @model_validator(mode="after")
    def revision_identity_and_refs_must_be_consistent(self):
        if self.previous_revision_id == self.revision_id:
            raise ValueError("事实修订不能引用自身")
        if len(set(self.source_observation_ids)) != len(self.source_observation_ids):
            raise ValueError("事实修订不能重复引用来源观测")
        if tuple(sorted(set(self.affected_assets))) != self.affected_assets:
            raise ValueError("affected_assets 必须唯一且排序")
        if tuple(sorted(set(self.risk_factors))) != self.risk_factors:
            raise ValueError("risk_factors 必须唯一且排序")
        return self


class StateSnapshot(FrozenModel):
    state_id: str = Field(min_length=1)
    projection_version: str = Field(min_length=1)
    analysis_scope: str = Field(min_length=1)
    as_of: datetime
    built_at: datetime
    fact_revision_ids: tuple[str, ...] = ()
    market_snapshot_refs: tuple[str, ...] = ()
    feature_snapshot_refs: tuple[str, ...] = ()
    account_snapshot_ref: str | None = Field(default=None, min_length=1)
    data_quality_codes: tuple[str, ...] = ()
    coverage_gap_codes: tuple[str, ...] = ()
    content_hash: str = Field(pattern=SHA256_PATTERN)

    _utc_as_of = field_validator("as_of")(require_utc)
    _utc_built_at = field_validator("built_at")(require_utc)

    @model_validator(mode="after")
    def snapshot_must_be_deterministic_and_point_in_time_safe(self):
        if self.built_at < self.as_of:
            raise ValueError("StateSnapshot built_at 不能早于 as_of")
        for name in (
            "fact_revision_ids",
            "market_snapshot_refs",
            "feature_snapshot_refs",
            "data_quality_codes",
            "coverage_gap_codes",
        ):
            values = getattr(self, name)
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"{name} 必须唯一且排序")
        return self


class MaterialDelta(FrozenModel):
    delta_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    analysis_scope: str = Field(min_length=1)
    previous_state_id: str | None = Field(default=None, min_length=1)
    current_state_id: str = Field(min_length=1)
    observed_at: datetime
    expires_at: datetime
    category: DeltaCategory
    materiality: Materiality
    affected_assets: tuple[str, ...] = ()
    risk_factors: tuple[str, ...] = Field(min_length=1)
    horizons_minutes: tuple[int, ...] = Field(min_length=1)
    fact_revision_ids: tuple[str, ...] = ()
    feature_snapshot_refs: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = Field(min_length=1)
    content_hash: str = Field(pattern=SHA256_PATTERN)

    _utc_observed_at = field_validator("observed_at")(require_utc)
    _utc_expires_at = field_validator("expires_at")(require_utc)

    @model_validator(mode="after")
    def delta_must_reference_a_real_bounded_change(self):
        if self.previous_state_id == self.current_state_id:
            raise ValueError("MaterialDelta 必须比较不同状态")
        if self.observed_at >= self.expires_at:
            raise ValueError("MaterialDelta 必须具有未来有效期")
        for name in (
            "affected_assets",
            "risk_factors",
            "horizons_minutes",
            "fact_revision_ids",
            "feature_snapshot_refs",
            "reason_codes",
        ):
            values = getattr(self, name)
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"{name} 必须唯一且排序")
        if any(value <= 0 for value in self.horizons_minutes):
            raise ValueError("horizons_minutes 必须全部为正数")
        if not self.fact_revision_ids and not self.feature_snapshot_refs:
            raise ValueError("MaterialDelta 必须引用事实或特征变化")
        return self


class ContextView(FrozenModel):
    asset: str = Field(min_length=1)
    horizon_minutes: int = Field(gt=0)
    direction: DirectionalView
    already_priced: PricedState
    uncertainty: AssessmentUncertainty
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    invalidation_conditions: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def refs_must_be_unique(self):
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("ContextView 不能重复引用证据")
        if len(set(self.invalidation_conditions)) != len(
            self.invalidation_conditions
        ):
            raise ValueError("ContextView 不能重复失效条件")
        return self


class ContextAssessment(FrozenModel):
    assessment_id: str = Field(min_length=1)
    analysis_scope: str = Field(min_length=1)
    mandate_version: str = Field(min_length=1)
    as_of: datetime
    available_at: datetime
    analysis_behavior_hash: str = Field(pattern=SHA256_PATTERN)
    decision_packet_hash: str = Field(pattern=SHA256_PATTERN)
    trigger_ids: tuple[str, ...] = Field(min_length=1)
    market_mechanism: str = Field(min_length=1, max_length=2_000)
    views: tuple[ContextView, ...] = Field(min_length=1)
    contradictions: tuple[str, ...] = ()
    data_gaps: tuple[str, ...] = ()

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
        return self


class BaseForecast(FrozenModel):
    forecast_id: str = Field(min_length=1)
    producer_id: str = Field(min_length=1)
    producer_version: str = Field(min_length=1)
    forecast_family: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    horizon_minutes: int = Field(gt=0)
    direction: DirectionalView
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
        if len(set(self.input_refs)) != len(self.input_refs):
            raise ValueError("BaseForecast 不能重复引用输入")
        return self


class CalibratedForecast(FrozenModel):
    forecast_id: str = Field(min_length=1)
    role: ForecastRole
    producer_id: str = Field(min_length=1)
    producer_version: str = Field(min_length=1)
    forecast_family: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    horizon_minutes: int = Field(gt=0)
    direction: DirectionalView
    reference_price: PositiveDecimal
    expected_edge_half_life_seconds: int = Field(gt=0, le=604_800)
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


class AssetTarget(FrozenModel):
    symbol: str = Field(min_length=1)
    desired_quote_notional: Money
    forecast_ids: tuple[str, ...] = Field(min_length=1)
    conservative_gross_bps: Decimal
    estimated_variable_cost_bps: Money
    conservative_net_bps: Decimal
    reason_codes: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def target_economics_and_refs_must_be_consistent(self):
        expected_net = self.conservative_gross_bps - self.estimated_variable_cost_bps
        if self.conservative_net_bps != expected_net:
            raise ValueError("AssetTarget 净收益必须等于保守毛收益减可变成本")
        if len(set(self.forecast_ids)) != len(self.forecast_ids):
            raise ValueError("AssetTarget 不能重复引用预测")
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("AssetTarget 不能重复原因码")
        return self


class PortfolioTarget(FrozenModel):
    target_id: str = Field(min_length=1)
    cycle_id: str = Field(min_length=1)
    portfolio_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    as_of: datetime
    valid_until: datetime
    reference_equity: PositiveDecimal
    targets: tuple[AssetTarget, ...] = ()

    _utc_as_of = field_validator("as_of")(require_utc)
    _utc_valid_until = field_validator("valid_until")(require_utc)

    @model_validator(mode="after")
    def target_set_must_be_bounded_and_unambiguous(self):
        if self.as_of >= self.valid_until:
            raise ValueError("PortfolioTarget 必须具有未来有效期")
        symbols = tuple(item.symbol for item in self.targets)
        if tuple(sorted(set(symbols))) != symbols:
            raise ValueError("PortfolioTarget 资产必须唯一且排序")
        if sum(item.desired_quote_notional for item in self.targets) > (
            self.reference_equity
        ):
            raise ValueError("无杠杆 MVP 的目标名义金额不能超过参考权益")
        return self
