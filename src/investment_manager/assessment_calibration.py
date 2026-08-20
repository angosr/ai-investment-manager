from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from investment_manager.assessment_forecast import (
    AssessmentForecastPolicy,
    AssessmentViewCalibration,
    build_assessment_view_calibration,
)
from investment_manager.assessment_outcome import AssessmentViewOutcome
from investment_manager.asset_management import AssessmentUncertainty, PricedState
from investment_manager.domain import DirectionalView, ForecastOutcomeStatus, _require_utc


@dataclass(frozen=True, slots=True)
class AssessmentCalibrationBuildSpec:
    analysis_scope: str
    analysis_behavior_hash: str
    outcome_evaluation_version: str
    asset: str
    symbol: str
    horizon_minutes: int
    direction: DirectionalView
    already_priced: PricedState
    uncertainty: AssessmentUncertainty
    training_start: datetime
    training_end: datetime
    published_at: datetime
    expected_edge_half_life_seconds: int

    def __post_init__(self) -> None:
        for name in ("training_start", "training_end", "published_at"):
            object.__setattr__(self, name, _require_utc(getattr(self, name)))
        if not self.analysis_scope or not self.outcome_evaluation_version:
            raise ValueError("Assessment calibration scope/version 不能为空")
        if not (
            len(self.analysis_behavior_hash) == 64
            and all(
                character in "0123456789abcdef"
                for character in self.analysis_behavior_hash
            )
        ):
            raise ValueError("analysis_behavior_hash 必须是 64 位十六进制摘要")
        if self.horizon_minutes <= 0 or self.expected_edge_half_life_seconds <= 0:
            raise ValueError("Assessment calibration 周期与半衰期必须为正数")
        if not self.training_start < self.training_end <= self.published_at:
            raise ValueError("Assessment calibration 训练窗口与发布时间顺序非法")


@dataclass(frozen=True, slots=True)
class AssessmentCalibrationBuilder:
    policy: AssessmentForecastPolicy

    def build(
        self,
        outcomes: Iterable[AssessmentViewOutcome],
        spec: AssessmentCalibrationBuildSpec,
    ) -> AssessmentViewCalibration:
        matched = tuple(
            sorted(
                (item for item in outcomes if self._eligible(item, spec)),
                key=lambda item: (item.signal_observed_at, item.outcome_id),
            )
        )
        if len(matched) < self.policy.minimum_sample_size:
            raise ValueError("Assessment calibration 成熟样本总数不足")
        independent = _non_overlapping(matched)
        if (
            len(independent)
            < self.policy.minimum_non_overlapping_sample_size
        ):
            raise ValueError("Assessment calibration 非重叠成熟样本不足")
        returns = tuple(_directional_return(item) for item in independent)
        count = Decimal(len(returns))
        mean = sum(returns, Decimal("0")) / count
        variance = sum(
            ((value - mean) ** 2 for value in returns),
            Decimal("0"),
        ) / (count - 1)
        dispersion = variance.sqrt()
        conservative = mean - self.policy.lower_confidence_z * (
            variance / count
        ).sqrt()
        return build_assessment_view_calibration(
            version=self.policy.version,
            analysis_scope=spec.analysis_scope,
            analysis_behavior_hash=spec.analysis_behavior_hash,
            outcome_evaluation_version=spec.outcome_evaluation_version,
            method_version=self.policy.calibration_method_version,
            lower_confidence_z=self.policy.lower_confidence_z,
            asset=spec.asset,
            symbol=spec.symbol,
            horizon_minutes=spec.horizon_minutes,
            direction=spec.direction,
            already_priced=spec.already_priced,
            uncertainty=spec.uncertainty,
            training_start=spec.training_start,
            training_end=spec.training_end,
            trained_through=max(item.evaluation_at for item in independent),
            available_at=spec.published_at,
            expected_edge_half_life_seconds=(
                spec.expected_edge_half_life_seconds
            ),
            expected_gross_bps=mean,
            conservative_gross_bps=conservative,
            dispersion_bps=dispersion,
            sample_size=len(matched),
            non_overlapping_sample_size=len(independent),
            source_refs=tuple(sorted(item.outcome_id for item in matched)),
            non_overlapping_source_refs=tuple(
                sorted(item.outcome_id for item in independent)
            ),
        )

    @staticmethod
    def _eligible(
        outcome: AssessmentViewOutcome,
        spec: AssessmentCalibrationBuildSpec,
    ) -> bool:
        return (
            outcome.status == ForecastOutcomeStatus.SETTLED
            and outcome.analysis_scope == spec.analysis_scope
            and outcome.analysis_behavior_hash == spec.analysis_behavior_hash
            and outcome.evaluation_version == spec.outcome_evaluation_version
            and outcome.asset == spec.asset
            and outcome.symbol == spec.symbol
            and outcome.horizon_minutes == spec.horizon_minutes
            and outcome.direction == spec.direction
            and outcome.already_priced == spec.already_priced
            and outcome.uncertainty == spec.uncertainty
            and spec.training_start <= outcome.signal_observed_at
            and outcome.evaluation_at <= spec.training_end
            and outcome.settled_at <= spec.published_at
        )


def _non_overlapping(
    outcomes: tuple[AssessmentViewOutcome, ...],
) -> tuple[AssessmentViewOutcome, ...]:
    selected: list[AssessmentViewOutcome] = []
    last_evaluation_at: datetime | None = None
    for outcome in outcomes:
        if (
            last_evaluation_at is not None
            and outcome.signal_observed_at < last_evaluation_at
        ):
            continue
        selected.append(outcome)
        last_evaluation_at = outcome.evaluation_at
    return tuple(selected)


def _directional_return(outcome: AssessmentViewOutcome) -> Decimal:
    if outcome.directional_return_bps is None:
        raise ValueError("SETTLED Assessment calibration 样本缺少方向收益")
    return outcome.directional_return_bps
