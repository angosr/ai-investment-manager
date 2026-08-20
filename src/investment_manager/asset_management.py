from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from investment_manager.domain import DirectionalView
from investment_manager.kernel.identity import SHA256_PATTERN
from investment_manager.kernel.time import (
    require_utc,
)
from investment_manager.kernel.types import (
    FrozenModel,
    Money,
    PositiveDecimal,
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


class ForecastRole(StrEnum):
    PROGRAM_BASE = "PROGRAM_BASE"
    AI_EVENT = "AI_EVENT"
    AI_ADJUSTED = "AI_ADJUSTED"


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
