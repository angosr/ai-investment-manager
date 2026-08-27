"""Cost-after outcome diagnostics for one immutable portfolio choice."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from investment_manager.forecast.models import ExposureDirection
from investment_manager.kernel.time import require_utc
from investment_manager.portfolio.models import CapitalCycleOutcome, CapitalCycleRecord

CAPITAL_CHOICE_EVALUATION_VERSION = "capital-choice-outcome-v4"
_FORECAST_DECISION_TRIGGERS = frozenset({"FORECAST_CADENCE", "FORECAST_EVENT_DUE"})


def is_full_forecast_capital_choice(
    record: CapitalCycleRecord,
    *,
    capital_behavior_id: str,
) -> bool:
    """Whether a receipt owns a fresh Forecast-to-capital comparison.

    Holding/risk reviews may legally retain, resize, or exit current Sleeves, but
    they do not reconstruct every unheld candidate and therefore cannot support
    a cross-exposure missed-opportunity claim.
    """

    if not capital_behavior_id:
        raise ValueError("Capital choice 评价必须指定资本行为身份")
    return (
        record.pipeline_id == capital_behavior_id
        and record.outcome == CapitalCycleOutcome.TARGET_DECIDED
        and not _FORECAST_DECISION_TRIGGERS.isdisjoint(record.trigger_types)
    )


@dataclass(frozen=True, slots=True)
class CapitalChoiceCase:
    """One product candidate exactly as compared with cash at decision time."""

    decision_id: str
    decision_at: datetime
    evaluation_at: datetime
    economic_exposure_id: str
    projection_id: str
    instrument_key: str
    direction: ExposureDirection
    selected: bool
    predicted_net_bps: Decimal
    decision_gross_bps: Decimal
    projection_gross_bps: Decimal
    decision_cost_bps: Decimal
    realized_product_gross_bps: Decimal

    def __post_init__(self) -> None:
        require_utc(self.decision_at)
        require_utc(self.evaluation_at)
        if self.decision_at >= self.evaluation_at:
            raise ValueError("Capital choice 决策时间必须早于结算时间")
        for value, label in (
            (self.decision_id, "decision_id"),
            (self.economic_exposure_id, "economic_exposure_id"),
            (self.projection_id, "projection_id"),
            (self.instrument_key, "instrument_key"),
        ):
            if not value:
                raise ValueError(f"Capital choice {label} 不能为空")
        if self.decision_cost_bps < 0:
            raise ValueError("Capital choice 决策成本不能为负数")
        if self.predicted_net_bps != self.decision_gross_bps - self.decision_cost_bps:
            raise ValueError("Capital choice 预测净收益必须等于决策毛收益减冻结成本")

    @property
    def realized_remaining_gross_bps(self) -> Decimal:
        """Re-anchor the product outcome from its entry anchor to decision time."""

        return (
            self.realized_product_gross_bps
            + self.decision_gross_bps
            - self.projection_gross_bps
        )

    @property
    def realized_net_bps(self) -> Decimal:
        return self.realized_remaining_gross_bps - self.decision_cost_bps


@dataclass(frozen=True, slots=True)
class CapitalChoiceCandidateOutcome:
    projection_id: str
    instrument_key: str
    direction: ExposureDirection
    predicted_net_bps: Decimal
    realized_net_bps: Decimal


@dataclass(frozen=True, slots=True)
class CapitalChoiceExposureOutcome:
    economic_exposure_id: str
    selected: CapitalChoiceCandidateOutcome | None
    best_realized: CapitalChoiceCandidateOutcome
    opportunity_gap_bps: Decimal
    missed_profitable_exposure: bool
    selected_unprofitable_exposure: bool


@dataclass(frozen=True, slots=True)
class CapitalChoiceEvidence:
    evaluation_version: str
    capital_behavior_id: str
    decision_id: str
    decision_at: datetime
    evaluation_at: datetime
    candidate_count: int
    exposures: tuple[CapitalChoiceExposureOutcome, ...]

    @property
    def missed_profitable_exposure_count(self) -> int:
        return sum(item.missed_profitable_exposure for item in self.exposures)

    @property
    def selected_unprofitable_exposure_count(self) -> int:
        return sum(item.selected_unprofitable_exposure for item in self.exposures)


def evaluate_capital_choice(
    cases: tuple[CapitalChoiceCase, ...],
    *,
    capital_behavior_id: str,
) -> CapitalChoiceEvidence:
    """Compare the chosen product or cash with hindsight products per exposure.

    This is a diagnostic of the frozen decision.  The hindsight winner is never a
    forecast, benchmark, strategy, or source of capital permission.
    """

    if not cases:
        raise ValueError("Capital choice 评价至少需要一个候选")
    if not capital_behavior_id:
        raise ValueError("Capital choice 评价必须指定资本行为身份")
    decision_ids = {item.decision_id for item in cases}
    decision_times = {item.decision_at for item in cases}
    evaluation_times = {item.evaluation_at for item in cases}
    projection_ids = tuple(item.projection_id for item in cases)
    if len(decision_ids) != 1 or len(decision_times) != 1 or len(evaluation_times) != 1:
        raise ValueError("Capital choice 候选必须属于同一决策与共同结算时点")
    if len(set(projection_ids)) != len(projection_ids):
        raise ValueError("Capital choice 候选 projection 不得重复")

    by_exposure: defaultdict[str, list[CapitalChoiceCase]] = defaultdict(list)
    for case in cases:
        by_exposure[case.economic_exposure_id].append(case)

    exposures = []
    for exposure_id, candidates in sorted(by_exposure.items()):
        selected_cases = [item for item in candidates if item.selected]
        if len(selected_cases) > 1:
            raise ValueError("同一经济暴露不得选择多个产品表达")
        best = max(candidates, key=lambda item: (item.realized_net_bps, item.projection_id))
        selected_case = selected_cases[0] if selected_cases else None
        selected_net = (
            selected_case.realized_net_bps if selected_case is not None else Decimal("0")
        )
        opportunity_gap = max(Decimal("0"), best.realized_net_bps - selected_net)
        exposures.append(
            CapitalChoiceExposureOutcome(
                economic_exposure_id=exposure_id,
                selected=(
                    _candidate_outcome(selected_case)
                    if selected_case is not None
                    else None
                ),
                best_realized=_candidate_outcome(best),
                opportunity_gap_bps=opportunity_gap,
                missed_profitable_exposure=(
                    selected_case is None and best.realized_net_bps > 0
                ),
                selected_unprofitable_exposure=(
                    selected_case is not None and selected_case.realized_net_bps < 0
                ),
            )
        )

    return CapitalChoiceEvidence(
        evaluation_version=CAPITAL_CHOICE_EVALUATION_VERSION,
        capital_behavior_id=capital_behavior_id,
        decision_id=next(iter(decision_ids)),
        decision_at=next(iter(decision_times)),
        evaluation_at=next(iter(evaluation_times)),
        candidate_count=len(cases),
        exposures=tuple(exposures),
    )


def _candidate_outcome(case: CapitalChoiceCase) -> CapitalChoiceCandidateOutcome:
    return CapitalChoiceCandidateOutcome(
        projection_id=case.projection_id,
        instrument_key=case.instrument_key,
        direction=case.direction,
        predicted_net_bps=case.predicted_net_bps,
        realized_net_bps=case.realized_net_bps,
    )


__all__ = [
    "CAPITAL_CHOICE_EVALUATION_VERSION",
    "CapitalChoiceCandidateOutcome",
    "CapitalChoiceCase",
    "CapitalChoiceEvidence",
    "CapitalChoiceExposureOutcome",
    "evaluate_capital_choice",
    "is_full_forecast_capital_choice",
]
