from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from investment_manager.execution.models import Side
from investment_manager.kernel.identity import SHA256_PATTERN, content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel, Money, PositiveDecimal
from investment_manager.market.models import InstrumentId, InstrumentProduct


class ExposureDirection(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


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

    @model_validator(mode="after")
    def legs_and_identity_must_be_canonical(self):
        keys = tuple(item.instrument.key for item in self.legs)
        if tuple(sorted(set(keys))) != keys:
            raise ValueError("ForecastTarget legs 必须按 Instrument 唯一且排序")
        if sum((item.gross_weight for item in self.legs), Decimal("0")) != Decimal("1"):
            raise ValueError("ForecastTarget gross_weight 绝对权重之和必须为 1")
        expected = self.identity_for(self.legs)
        if self.target_id != expected:
            raise ValueError("ForecastTarget target_id 与规范化 Leg 内容不一致")
        return self

    @staticmethod
    def identity_for(legs: tuple[ForecastLeg, ...]) -> str:
        return stable_id(
            "forecast_target",
            content_hash({"legs": [item.model_dump(mode="json") for item in legs]}),
        )

    @classmethod
    def create(cls, legs: tuple[ForecastLeg, ...]) -> ForecastTarget:
        ordered = tuple(sorted(legs, key=lambda item: item.instrument.key))
        return cls(target_id=cls.identity_for(ordered), legs=ordered)

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


class CalibratedForecast(FrozenModel):
    forecast_id: str = Field(min_length=1)
    role: ForecastRole
    producer_id: str = Field(min_length=1)
    producer_version: str = Field(min_length=1)
    forecast_family: str = Field(min_length=1)
    target: ForecastTarget
    horizon_minutes: int = Field(gt=0)
    direction: DirectionalView
    reference_prices: tuple[ForecastReferencePrice, ...] = Field(min_length=1)
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
