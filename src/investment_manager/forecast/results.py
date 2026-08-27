from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import AliasChoices, Field, field_validator, model_validator

from investment_manager.forecast.contracts import ForecastOrientation, ForecastPriceAnchor
from investment_manager.forecast.models import ExposureDirection, ForecastTarget
from investment_manager.kernel.identity import canonical_json, content_hash, stable_id
from investment_manager.kernel.time import optional_utc, require_utc
from investment_manager.kernel.types import FrozenModel, PositiveDecimal


class ForecastResultKind(StrEnum):
    BASE = "BASE"
    CALIBRATED = "CALIBRATED"


class ForecastOutcomeStatus(StrEnum):
    SETTLED = "SETTLED"
    OUTCOME_UNAVAILABLE = "OUTCOME_UNAVAILABLE"


class ForecastBucketProbability(FrozenModel):
    bucket_id: str = Field(min_length=1, max_length=64)
    probability: Decimal = Field(ge=0, le=1)


class ForecastMechanismEffect(StrEnum):
    UPSIDE = "UPSIDE"
    DOWNSIDE = "DOWNSIDE"
    UNCERTAINTY = "UNCERTAINTY"
    NO_MATERIAL_EFFECT = "NO_MATERIAL_EFFECT"


class ForecastMechanismContribution(FrozenModel):
    """Auditable link from one persisted WorldModel mechanism to a distribution."""

    mechanism_id: str = Field(min_length=1)
    effect: ForecastMechanismEffect
    rationale: str = Field(min_length=1, max_length=600)


class BaseForecast(FrozenModel):
    """One producer's immutable probability estimate for a shared decision slot."""

    forecast_id: str = Field(min_length=1)
    contract_id: str = Field(min_length=1)
    decision_slot_id: str = Field(min_length=1)
    producer_id: str = Field(min_length=1)
    producer_behavior_id: str = Field(min_length=1)
    outcome_family_id: str = Field(min_length=1)
    target: ForecastTarget
    horizon_minutes: int = Field(gt=0)
    orientation: ForecastOrientation = ForecastOrientation.CANONICAL
    cutoff_prices: tuple[ForecastPriceAnchor, ...] = Field(min_length=1)
    entry_prices: tuple[ForecastPriceAnchor, ...] = Field(min_length=1)
    information_cutoff_at: datetime
    input_observed_at: datetime
    available_at: datetime
    valid_until: datetime
    outcome_probabilities: tuple[ForecastBucketProbability, ...] = Field(min_length=3)
    expected_gross_bps: Decimal
    input_refs: tuple[str, ...] = Field(min_length=1)
    world_model_id: str | None = Field(default=None, min_length=1)
    mechanism_contributions: tuple[ForecastMechanismContribution, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    invalidation_conditions: tuple[str, ...] = ()
    analysis_input_json: str | None = None
    analysis_input_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    program_input_json: str | None = None
    program_input_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    _utc_information_cutoff_at = field_validator("information_cutoff_at")(require_utc)
    _utc_input_observed_at = field_validator("input_observed_at")(require_utc)
    _utc_available_at = field_validator("available_at")(require_utc)
    _utc_valid_until = field_validator("valid_until")(require_utc)

    @model_validator(mode="after")
    def identity_timing_distribution_and_refs_are_canonical(self):
        if not (
            self.input_observed_at
            <= self.information_cutoff_at
            <= self.available_at
            < self.valid_until
            <= self.economic_horizon_end
        ):
            raise ValueError("BaseForecast 时间顺序非法")
        _require_complete_price_anchors(self.target, self.cutoff_prices, "cutoff")
        _require_complete_price_anchors(self.target, self.entry_prices, "entry")
        if any(item.available_at > self.information_cutoff_at for item in self.cutoff_prices):
            raise ValueError("BaseForecast cutoff price 不得晚于信息截止")
        if any(item.available_at < self.available_at for item in self.entry_prices):
            raise ValueError("BaseForecast entry price 必须来自预测完成之后")
        bucket_ids = tuple(item.bucket_id for item in self.outcome_probabilities)
        if len(set(bucket_ids)) != len(bucket_ids):
            raise ValueError("BaseForecast bucket probability 必须唯一")
        if sum((item.probability for item in self.outcome_probabilities), Decimal("0")) != 1:
            raise ValueError("BaseForecast 概率之和必须为 1")
        if tuple(sorted(set(self.input_refs))) != self.input_refs:
            raise ValueError("BaseForecast input_refs 必须唯一且排序")
        mechanism_ids = tuple(item.mechanism_id for item in self.mechanism_contributions)
        if len(set(mechanism_ids)) != len(mechanism_ids):
            raise ValueError("BaseForecast mechanism_contributions 不能重复")
        for name in ("evidence_refs", "invalidation_conditions"):
            values = getattr(self, name)
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"BaseForecast {name} 必须唯一且排序")
        has_context_provenance = bool(
            self.mechanism_contributions or self.evidence_refs or self.invalidation_conditions
        )
        if (self.world_model_id is not None) != has_context_provenance:
            raise ValueError("BaseForecast WorldModel 身份与 Context 来源链必须同时存在")
        if (self.analysis_input_json is None) != (self.analysis_input_hash is None):
            raise ValueError("BaseForecast AI 输入原文与哈希必须同时存在")
        if (self.program_input_json is None) != (self.program_input_hash is None):
            raise ValueError("BaseForecast 程序输入原文与哈希必须同时存在")
        if self.analysis_input_json is not None and self.program_input_json is not None:
            raise ValueError("BaseForecast AI 与程序输入快照不能同时存在")
        if self.world_model_id is not None and self.analysis_input_json is None:
            raise ValueError("Context BaseForecast 必须保存真实 AI 输入快照")
        if self.world_model_id is None and self.analysis_input_json is not None:
            raise ValueError("非 Context BaseForecast 不得伪造 AI 输入快照")
        if self.analysis_input_json is not None:
            try:
                parsed_input = json.loads(self.analysis_input_json)
            except json.JSONDecodeError as exc:
                raise ValueError("BaseForecast AI 输入快照不是有效 JSON") from exc
            if canonical_json(parsed_input) != self.analysis_input_json:
                raise ValueError("BaseForecast AI 输入快照必须是规范 JSON")
            if content_hash(parsed_input) != self.analysis_input_hash:
                raise ValueError("BaseForecast AI 输入快照哈希不一致")
        if self.program_input_json is not None:
            try:
                parsed_program_input = json.loads(self.program_input_json)
            except json.JSONDecodeError as exc:
                raise ValueError("BaseForecast 程序输入快照不是有效 JSON") from exc
            if canonical_json(parsed_program_input) != self.program_input_json:
                raise ValueError("BaseForecast 程序输入快照必须是规范 JSON")
            if content_hash(parsed_program_input) != self.program_input_hash:
                raise ValueError("BaseForecast 程序输入快照哈希不一致")
        expected = stable_id(
            "base_forecast",
            self.decision_slot_id,
            self.producer_behavior_id,
        )
        if self.forecast_id != expected:
            raise ValueError("BaseForecast forecast_id 与槽/行为不一致")
        return self

    @property
    def economic_horizon_end(self) -> datetime:
        return self.information_cutoff_at + timedelta(minutes=self.horizon_minutes)


class CalibratedForecast(FrozenModel):
    """One prospectively frozen policy's authoritative capital distribution."""

    forecast_id: str = Field(min_length=1)
    contract_id: str = Field(min_length=1)
    decision_slot_id: str = Field(min_length=1)
    base_forecast_id: str = Field(min_length=1)
    forecast_policy_id: str = Field(min_length=1)
    producer_id: str = Field(min_length=1)
    producer_behavior_id: str = Field(min_length=1)
    outcome_family_id: str = Field(min_length=1)
    target: ForecastTarget
    horizon_minutes: int = Field(gt=0)
    orientation: ForecastOrientation = ForecastOrientation.CANONICAL
    cutoff_prices: tuple[ForecastPriceAnchor, ...] = Field(min_length=1)
    entry_prices: tuple[ForecastPriceAnchor, ...] = Field(min_length=1)
    information_cutoff_at: datetime
    available_at: datetime
    valid_until: datetime
    outcome_probabilities: tuple[ForecastBucketProbability, ...] = Field(min_length=3)
    expected_gross_bps: Decimal
    conservative_gross_bps: Decimal
    dispersion_bps: Decimal = Field(ge=0)
    calibration_sample_size: int = Field(gt=0)
    non_overlapping_sample_size: int = Field(gt=0)
    input_refs: tuple[str, ...] = Field(min_length=1)

    _utc_information_cutoff_at = field_validator("information_cutoff_at")(require_utc)
    _utc_available_at = field_validator("available_at")(require_utc)
    _utc_valid_until = field_validator("valid_until")(require_utc)

    @model_validator(mode="after")
    def identity_timing_distribution_and_evidence_are_canonical(self):
        if not (
            self.information_cutoff_at
            <= self.available_at
            < self.valid_until
            <= self.economic_horizon_end
        ):
            raise ValueError("CalibratedForecast 时间顺序非法")
        if self.conservative_gross_bps > self.expected_gross_bps:
            raise ValueError("保守毛收益不能高于期望毛收益")
        if self.non_overlapping_sample_size > self.calibration_sample_size:
            raise ValueError("非重叠校准样本不能超过总样本")
        _require_complete_price_anchors(self.target, self.cutoff_prices, "cutoff")
        _require_complete_price_anchors(self.target, self.entry_prices, "entry")
        bucket_ids = tuple(item.bucket_id for item in self.outcome_probabilities)
        if len(set(bucket_ids)) != len(bucket_ids):
            raise ValueError("CalibratedForecast bucket probability 必须唯一")
        if sum((item.probability for item in self.outcome_probabilities), Decimal("0")) != 1:
            raise ValueError("CalibratedForecast 概率之和必须为 1")
        if tuple(sorted(set(self.input_refs))) != self.input_refs:
            raise ValueError("CalibratedForecast input_refs 必须唯一且排序")
        expected = stable_id(
            "calibrated_forecast",
            self.decision_slot_id,
            self.forecast_policy_id,
            self.base_forecast_id,
        )
        if self.forecast_id != expected:
            raise ValueError("CalibratedForecast forecast_id 与槽/Policy/Base 不一致")
        return self

    @property
    def economic_horizon_end(self) -> datetime:
        return self.information_cutoff_at + timedelta(minutes=self.horizon_minutes)


class ForecastLegOutcome(FrozenModel):
    instrument_id: str = Field(min_length=1)
    direction: ExposureDirection
    gross_weight: Decimal = Field(gt=0, le=1)
    reference_price: PositiveDecimal = Field(
        validation_alias=AliasChoices("reference_price", "cutoff_reference_price")
    )
    exit_price: PositiveDecimal
    price_return_bps: Decimal
    funding_return_bps: Decimal = Decimal("0")
    funding_settlement_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def funding_refs_are_unique(self):
        if len(set(self.funding_settlement_ids)) != len(self.funding_settlement_ids):
            raise ValueError("ForecastLegOutcome Funding 引用不能重复")
        return self


class ForecastOutcome(FrozenModel):
    """One source-independent realized outcome for one decision slot."""

    outcome_id: str = Field(min_length=1)
    contract_id: str = Field(min_length=1)
    decision_slot_id: str = Field(min_length=1)
    evaluation_version: str = Field(min_length=1)
    status: ForecastOutcomeStatus
    information_cutoff_at: datetime
    outcome_start_at: datetime | None = None
    evaluation_at: datetime
    settled_at: datetime
    legs: tuple[ForecastLegOutcome, ...] = ()
    gross_target_return_bps: Decimal | None = None
    realized_bucket_id: str | None = Field(default=None, min_length=1)
    reason_code: str = Field(min_length=1)

    _utc_information_cutoff_at = field_validator("information_cutoff_at")(require_utc)
    _utc_outcome_start_at = field_validator("outcome_start_at")(optional_utc)
    _utc_evaluation_at = field_validator("evaluation_at")(require_utc)
    _utc_settled_at = field_validator("settled_at")(require_utc)

    @model_validator(mode="after")
    def identity_timing_and_status_are_canonical(self):
        economic_start = self.outcome_start_at or self.information_cutoff_at
        if not (
            self.information_cutoff_at
            <= economic_start
            < self.evaluation_at
            <= self.settled_at
        ):
            raise ValueError("ForecastOutcome 时间顺序非法")
        has_result = self.status == ForecastOutcomeStatus.SETTLED
        if has_result != bool(
            self.legs and self.gross_target_return_bps is not None and self.realized_bucket_id
        ):
            raise ValueError("ForecastOutcome 状态与收益结果不一致")
        expected = stable_id(
            "forecast_outcome",
            self.decision_slot_id,
            self.evaluation_version,
        )
        if self.outcome_id != expected:
            raise ValueError("ForecastOutcome outcome_id 与槽/评价版本不一致")
        return self

    @property
    def permission_evidence_eligible(self) -> bool:
        return self.outcome_start_at is not None


Forecast = BaseForecast | CalibratedForecast


def forecast_kind(forecast: Forecast) -> ForecastResultKind:
    return (
        ForecastResultKind.BASE
        if isinstance(forecast, BaseForecast)
        else ForecastResultKind.CALIBRATED
    )


def _require_complete_price_anchors(
    target: ForecastTarget,
    anchors: tuple[ForecastPriceAnchor, ...],
    name: str,
) -> None:
    anchor_ids = tuple(item.instrument_id for item in anchors)
    target_ids = tuple(item.instrument.key for item in target.legs)
    if anchor_ids != target_ids:
        raise ValueError(f"Forecast {name} prices 必须逐 Leg 唯一、排序且完整")
