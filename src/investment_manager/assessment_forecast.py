from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import Field, field_validator, model_validator

from investment_manager.asset_management import (
    AssessmentUncertainty,
    CalibratedForecast,
    ContextAssessment,
    ContextView,
    ForecastRole,
    PricedState,
)
from investment_manager.decision_packet import DecisionPacket
from investment_manager.domain import DirectionalView
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import (
    FrozenModel,
    Money,
)
from investment_manager.market_data import MarketTrade


class AssessmentForecastPolicy(FrozenModel):
    version: str = Field(min_length=1)
    producer_id: str = Field(default="codex-context-assessment", min_length=1)
    forecast_family: str = Field(default="AI_EVENT_CONTEXT", min_length=1)
    calibration_method_version: str = Field(
        default="assessment-mean-lower-bound-v1",
        min_length=1,
    )
    lower_confidence_z: Decimal = Field(default=Decimal("1.96"), gt=0)
    maximum_age_seconds: int = Field(gt=0, le=604_800)
    maximum_reference_market_age_seconds: int = Field(gt=0, le=3_600)
    minimum_sample_size: int = Field(ge=2)
    minimum_non_overlapping_sample_size: int = Field(ge=2)

    @model_validator(mode="after")
    def sample_thresholds_must_be_possible(self):
        if self.minimum_non_overlapping_sample_size > self.minimum_sample_size:
            raise ValueError("非重叠样本门槛不能超过总样本门槛")
        return self


class AssessmentViewCalibration(FrozenModel):
    calibration_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    analysis_scope: str = Field(min_length=1)
    analysis_behavior_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome_evaluation_version: str = Field(min_length=1)
    method_version: str = Field(min_length=1)
    lower_confidence_z: Decimal = Field(gt=0)
    asset: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    horizon_minutes: int = Field(gt=0)
    direction: DirectionalView
    already_priced: PricedState
    uncertainty: AssessmentUncertainty
    training_start: datetime
    training_end: datetime
    trained_through: datetime
    available_at: datetime
    expected_edge_half_life_seconds: int = Field(gt=0, le=604_800)
    expected_gross_bps: Decimal
    conservative_gross_bps: Decimal
    dispersion_bps: Money
    sample_size: int = Field(gt=0)
    non_overlapping_sample_size: int = Field(gt=0)
    source_refs: tuple[str, ...] = Field(min_length=1)
    non_overlapping_source_refs: tuple[str, ...] = Field(min_length=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    _utc_training_start = field_validator("training_start")(require_utc)
    _utc_training_end = field_validator("training_end")(require_utc)
    _utc_trained_through = field_validator("trained_through")(require_utc)
    _utc_available_at = field_validator("available_at")(require_utc)

    @model_validator(mode="after")
    def identity_and_evidence_must_be_valid(self):
        if not (
            self.training_start
            < self.trained_through
            <= self.training_end
            <= self.available_at
        ):
            raise ValueError("校准训练窗口、标签截止和制品可用时间顺序非法")
        if self.conservative_gross_bps > self.expected_gross_bps:
            raise ValueError("校准保守收益不能高于均值")
        if self.non_overlapping_sample_size > self.sample_size:
            raise ValueError("校准非重叠样本不能超过总样本")
        for name in ("source_refs", "non_overlapping_source_refs"):
            values = getattr(self, name)
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"校准 {name} 必须唯一且排序")
        if (
            len(self.source_refs) != self.sample_size
            or len(self.non_overlapping_source_refs)
            != self.non_overlapping_sample_size
            or not set(self.non_overlapping_source_refs).issubset(self.source_refs)
        ):
            raise ValueError("校准样本数量与 Outcome 引用不一致")
        expected_hash = content_hash(_calibration_payload(self))
        if self.content_hash != expected_hash:
            raise ValueError("AssessmentViewCalibration content_hash 不匹配")
        if self.calibration_id != stable_id("assessment_view_calibration", expected_hash):
            raise ValueError("AssessmentViewCalibration calibration_id 不匹配")
        return self


def build_assessment_view_calibration(
    *,
    version: str,
    analysis_scope: str,
    analysis_behavior_hash: str,
    outcome_evaluation_version: str,
    method_version: str,
    lower_confidence_z: Decimal,
    asset: str,
    symbol: str,
    horizon_minutes: int,
    direction: DirectionalView,
    already_priced: PricedState,
    uncertainty: AssessmentUncertainty,
    training_start: datetime,
    training_end: datetime,
    trained_through: datetime,
    available_at: datetime,
    expected_edge_half_life_seconds: int,
    expected_gross_bps: Decimal,
    conservative_gross_bps: Decimal,
    dispersion_bps: Decimal,
    sample_size: int,
    non_overlapping_sample_size: int,
    source_refs: tuple[str, ...],
    non_overlapping_source_refs: tuple[str, ...],
) -> AssessmentViewCalibration:
    payload = {
        "version": version,
        "analysis_scope": analysis_scope,
        "analysis_behavior_hash": analysis_behavior_hash,
        "outcome_evaluation_version": outcome_evaluation_version,
        "method_version": method_version,
        "lower_confidence_z": lower_confidence_z,
        "asset": asset,
        "symbol": symbol,
        "horizon_minutes": horizon_minutes,
        "direction": direction.value,
        "already_priced": already_priced.value,
        "uncertainty": uncertainty.value,
        "training_start": require_utc(training_start).isoformat(),
        "training_end": require_utc(training_end).isoformat(),
        "trained_through": require_utc(trained_through).isoformat(),
        "available_at": require_utc(available_at).isoformat(),
        "expected_edge_half_life_seconds": expected_edge_half_life_seconds,
        "expected_gross_bps": expected_gross_bps,
        "conservative_gross_bps": conservative_gross_bps,
        "dispersion_bps": dispersion_bps,
        "sample_size": sample_size,
        "non_overlapping_sample_size": non_overlapping_sample_size,
        "source_refs": tuple(sorted(source_refs)),
        "non_overlapping_source_refs": tuple(
            sorted(non_overlapping_source_refs)
        ),
    }
    digest = content_hash(payload)
    return AssessmentViewCalibration(
        calibration_id=stable_id("assessment_view_calibration", digest),
        content_hash=digest,
        **payload,
    )


@dataclass(frozen=True, slots=True)
class AssessmentForecastProjectionResult:
    forecasts: tuple[CalibratedForecast, ...]
    uncalibrated_views: tuple[tuple[str, int], ...]


class AssessmentForecastProjector:
    """Map exact, point-in-time calibrated assessment cohorts to AI_EVENT forecasts."""

    def __init__(self, policy: AssessmentForecastPolicy) -> None:
        self._policy = policy

    def project(
        self,
        *,
        packet: DecisionPacket,
        assessment: ContextAssessment,
        calibrations: tuple[AssessmentViewCalibration, ...],
        reference_trades: tuple[MarketTrade, ...],
    ) -> AssessmentForecastProjectionResult:
        self._require_assessment_binding(packet=packet, assessment=assessment)
        calibration_by_key: dict[tuple, AssessmentViewCalibration] = {}
        for calibration in calibrations:
            key = _calibration_key(calibration)
            if key in calibration_by_key:
                raise ValueError("同一 Assessment view cohort 只能有一个校准制品")
            if calibration.available_at > assessment.as_of:
                raise ValueError("Assessment 不能使用 as_of 之后才可用的校准制品")
            if calibration.analysis_behavior_hash != assessment.analysis_behavior_hash:
                raise ValueError("Assessment 不能使用其他分析行为 cohort 的校准制品")
            if calibration.analysis_scope != assessment.analysis_scope:
                raise ValueError("Assessment 不能使用其他 analysis scope 的校准制品")
            calibration_by_key[key] = calibration

        asset_states = {item.asset: item for item in packet.asset_states}
        trades_by_symbol = self._reference_trades(
            packet=packet,
            assessment=assessment,
            reference_trades=reference_trades,
        )
        forecasts: list[CalibratedForecast] = []
        uncalibrated: list[tuple[str, int]] = []
        for view in assessment.views:
            asset_state = asset_states[view.asset]
            calibration = calibration_by_key.get(
                (
                    view.asset,
                    asset_state.market_symbol,
                    view.horizon_minutes,
                    view.direction,
                    view.already_priced,
                    view.uncertainty,
                )
            )
            if calibration is None or not self._passes_sample_gate(calibration):
                uncalibrated.append((view.asset, view.horizon_minutes))
                continue
            forecasts.append(
                self._forecast(
                    packet=packet,
                    assessment=assessment,
                    view=view,
                    reference_trade=trades_by_symbol[asset_state.market_symbol],
                    calibration=calibration,
                )
            )
        return AssessmentForecastProjectionResult(
            forecasts=tuple(forecasts),
            uncalibrated_views=tuple(uncalibrated),
        )

    def _passes_sample_gate(self, calibration: AssessmentViewCalibration) -> bool:
        return (
            calibration.sample_size >= self._policy.minimum_sample_size
            and calibration.non_overlapping_sample_size
            >= self._policy.minimum_non_overlapping_sample_size
        )

    def _forecast(
        self,
        *,
        packet: DecisionPacket,
        assessment: ContextAssessment,
        view: ContextView,
        reference_trade: MarketTrade,
        calibration: AssessmentViewCalibration,
    ) -> CalibratedForecast:
        validity_seconds = min(
            self._policy.maximum_age_seconds,
            view.horizon_minutes * 60,
        )
        payload = {
            "role": ForecastRole.AI_EVENT.value,
            "producer_id": self._policy.producer_id,
            "producer_version": self._policy.version,
            "forecast_family": self._policy.forecast_family,
            "symbol": calibration.symbol,
            "horizon_minutes": view.horizon_minutes,
            "direction": view.direction.value,
            "reference_price": reference_trade.price,
            "expected_edge_half_life_seconds": (
                calibration.expected_edge_half_life_seconds
            ),
            "available_at": assessment.available_at.isoformat(),
            "valid_until": (
                assessment.available_at + timedelta(seconds=validity_seconds)
            ).isoformat(),
            "base_forecast_id": None,
            "assessment_id": assessment.assessment_id,
            "expected_gross_bps": calibration.expected_gross_bps,
            "conservative_gross_bps": calibration.conservative_gross_bps,
            "dispersion_bps": calibration.dispersion_bps,
            "calibration_ref": calibration.calibration_id,
            "calibration_sample_size": calibration.sample_size,
            "non_overlapping_sample_size": (
                calibration.non_overlapping_sample_size
            ),
            "input_refs": tuple(
                sorted(
                    (
                        packet.packet_id,
                        packet.content_hash,
                        assessment.assessment_id,
                        calibration.calibration_id,
                        content_hash(reference_trade),
                    )
                )
            ),
        }
        return CalibratedForecast(
            forecast_id=stable_id("calibrated_forecast", content_hash(payload)),
            **payload,
        )

    def _reference_trades(
        self,
        *,
        packet: DecisionPacket,
        assessment: ContextAssessment,
        reference_trades: tuple[MarketTrade, ...],
    ) -> dict[str, MarketTrade]:
        by_symbol = {item.symbol: item for item in reference_trades}
        if len(by_symbol) != len(reference_trades):
            raise ValueError("AI_EVENT reference trade 不能重复 symbol")
        required_symbols = {item.market_symbol for item in packet.asset_states}
        missing = tuple(sorted(required_symbols - set(by_symbol)))
        if missing:
            raise ValueError(
                "AI_EVENT 缺少 Assessment 完成时的 reference trade: "
                + ", ".join(missing)
            )
        for trade in reference_trades:
            age = assessment.available_at - trade.event_time
            if (
                trade.observed_at > assessment.available_at
                or age < timedelta(0)
                or age
                > timedelta(seconds=self._policy.maximum_reference_market_age_seconds)
            ):
                raise ValueError("AI_EVENT reference trade 在 Assessment 完成时不可见或过期")
        return by_symbol

    @staticmethod
    def _require_assessment_binding(
        *,
        packet: DecisionPacket,
        assessment: ContextAssessment,
    ) -> None:
        if assessment.decision_packet_hash != packet.content_hash:
            raise ValueError("ContextAssessment 与 DecisionPacket 内容身份不一致")
        if assessment.analysis_scope != packet.analysis_scope:
            raise ValueError("ContextAssessment 与 DecisionPacket scope 不一致")
        if assessment.mandate_version != packet.mandate_version:
            raise ValueError("ContextAssessment 与 DecisionPacket mandate 不一致")
        expected = tuple(
            (item.asset, item.horizon_minutes) for item in packet.required_views
        )
        observed = tuple(
            (item.asset, item.horizon_minutes) for item in assessment.views
        )
        if observed != expected:
            raise ValueError("ContextAssessment views 与 DecisionPacket 不一致")


def _calibration_key(calibration: AssessmentViewCalibration) -> tuple:
    return (
        calibration.asset,
        calibration.symbol,
        calibration.horizon_minutes,
        calibration.direction,
        calibration.already_priced,
        calibration.uncertainty,
    )


def _calibration_payload(calibration: AssessmentViewCalibration) -> dict:
    return {
        "version": calibration.version,
        "analysis_scope": calibration.analysis_scope,
        "analysis_behavior_hash": calibration.analysis_behavior_hash,
        "outcome_evaluation_version": calibration.outcome_evaluation_version,
        "method_version": calibration.method_version,
        "lower_confidence_z": calibration.lower_confidence_z,
        "asset": calibration.asset,
        "symbol": calibration.symbol,
        "horizon_minutes": calibration.horizon_minutes,
        "direction": calibration.direction.value,
        "already_priced": calibration.already_priced.value,
        "uncertainty": calibration.uncertainty.value,
        "training_start": calibration.training_start.isoformat(),
        "training_end": calibration.training_end.isoformat(),
        "trained_through": calibration.trained_through.isoformat(),
        "available_at": calibration.available_at.isoformat(),
        "expected_edge_half_life_seconds": (
            calibration.expected_edge_half_life_seconds
        ),
        "expected_gross_bps": calibration.expected_gross_bps,
        "conservative_gross_bps": calibration.conservative_gross_bps,
        "dispersion_bps": calibration.dispersion_bps,
        "sample_size": calibration.sample_size,
        "non_overlapping_sample_size": calibration.non_overlapping_sample_size,
        "source_refs": calibration.source_refs,
        "non_overlapping_source_refs": calibration.non_overlapping_source_refs,
    }
