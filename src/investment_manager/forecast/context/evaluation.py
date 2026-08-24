"""Deterministic evidence summary for one immutable Context Forecast cohort."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from investment_manager.kernel.time import require_utc


class ForecastEvidenceStatus(StrEnum):
    NO_SETTLED_SAMPLES = "NO_SETTLED_SAMPLES"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    ABOVE_BENCHMARK = "ABOVE_BENCHMARK"
    BELOW_BENCHMARK = "BELOW_BENCHMARK"
    DIAGNOSTIC_ONLY = "DIAGNOSTIC_ONLY"


@dataclass(frozen=True, slots=True)
class ForecastScoringCase:
    forecast_id: str
    information_cutoff_at: datetime
    evaluation_at: datetime
    probabilities: tuple[tuple[str, Decimal], ...]
    benchmark_probabilities: tuple[tuple[str, Decimal], ...]
    realized_bucket_id: str
    expected_gross_bps: Decimal
    realized_gross_bps: Decimal

    def __post_init__(self) -> None:
        require_utc(self.information_cutoff_at)
        require_utc(self.evaluation_at)
        if self.information_cutoff_at >= self.evaluation_at:
            raise ValueError("Forecast 评分样本的截止时间必须早于结算时间")
        forecast_ids = tuple(item[0] for item in self.probabilities)
        benchmark_ids = tuple(item[0] for item in self.benchmark_probabilities)
        if forecast_ids != benchmark_ids or self.realized_bucket_id not in forecast_ids:
            raise ValueError("Forecast 与基准概率桶必须一致并覆盖真实结果")
        for values in (self.probabilities, self.benchmark_probabilities):
            if sum((item[1] for item in values), Decimal("0")) != Decimal("1"):
                raise ValueError("Forecast 评分概率之和必须为 1")


@dataclass(frozen=True, slots=True)
class ForecastEvidence:
    status: ForecastEvidenceStatus
    terminal_result_count: int
    due_slot_count: int
    forecast_count: int
    no_estimate_count: int
    settled_forecast_count: int
    non_overlapping_sample_count: int
    required_non_overlapping_samples: int
    permission_evidence_eligible: bool
    mean_brier_score: Decimal | None
    benchmark_mean_brier_score: Decimal | None
    brier_skill: Decimal | None
    mean_expected_gross_bps: Decimal | None
    mean_realized_gross_bps: Decimal | None

    @property
    def result_coverage(self) -> Decimal | None:
        if self.due_slot_count == 0:
            return None
        return Decimal(self.terminal_result_count) / Decimal(self.due_slot_count)


def evaluate_forecast_evidence(
    cases: tuple[ForecastScoringCase, ...],
    *,
    due_slot_count: int,
    forecast_count: int,
    no_estimate_count: int,
    required_non_overlapping_samples: int,
    permission_evidence_eligible: bool = True,
) -> ForecastEvidence:
    """Score only a non-overlapping prospective subset against the frozen benchmark."""

    if min(due_slot_count, forecast_count, no_estimate_count) < 0:
        raise ValueError("Forecast evidence 计数不能为负数")
    if required_non_overlapping_samples < 2:
        raise ValueError("Forecast evidence 最小非重叠样本数至少为 2")
    terminal = forecast_count + no_estimate_count
    if terminal > due_slot_count:
        raise ValueError("Forecast 终态结果数不能超过到期槽数")

    independent = _non_overlapping(cases)
    if not independent:
        return ForecastEvidence(
            status=ForecastEvidenceStatus.NO_SETTLED_SAMPLES,
            terminal_result_count=terminal,
            due_slot_count=due_slot_count,
            forecast_count=forecast_count,
            no_estimate_count=no_estimate_count,
            settled_forecast_count=len(cases),
            non_overlapping_sample_count=0,
            required_non_overlapping_samples=required_non_overlapping_samples,
            permission_evidence_eligible=permission_evidence_eligible,
            mean_brier_score=None,
            benchmark_mean_brier_score=None,
            brier_skill=None,
            mean_expected_gross_bps=None,
            mean_realized_gross_bps=None,
        )

    model_brier = _mean(
        tuple(_brier(item.probabilities, item.realized_bucket_id) for item in independent)
    )
    benchmark_brier = _mean(
        tuple(
            _brier(item.benchmark_probabilities, item.realized_bucket_id)
            for item in independent
        )
    )
    assert model_brier is not None and benchmark_brier is not None
    status = (
        ForecastEvidenceStatus.DIAGNOSTIC_ONLY
        if not permission_evidence_eligible
        else ForecastEvidenceStatus.INSUFFICIENT_EVIDENCE
        if len(independent) < required_non_overlapping_samples
        else ForecastEvidenceStatus.ABOVE_BENCHMARK
        if model_brier < benchmark_brier
        else ForecastEvidenceStatus.BELOW_BENCHMARK
    )
    return ForecastEvidence(
        status=status,
        terminal_result_count=terminal,
        due_slot_count=due_slot_count,
        forecast_count=forecast_count,
        no_estimate_count=no_estimate_count,
        settled_forecast_count=len(cases),
        non_overlapping_sample_count=len(independent),
        required_non_overlapping_samples=required_non_overlapping_samples,
        permission_evidence_eligible=permission_evidence_eligible,
        mean_brier_score=model_brier,
        benchmark_mean_brier_score=benchmark_brier,
        brier_skill=benchmark_brier - model_brier,
        mean_expected_gross_bps=_mean(
            tuple(item.expected_gross_bps for item in independent)
        ),
        mean_realized_gross_bps=_mean(
            tuple(item.realized_gross_bps for item in independent)
        ),
    )


def _non_overlapping(cases: tuple[ForecastScoringCase, ...]) -> tuple[ForecastScoringCase, ...]:
    selected: list[ForecastScoringCase] = []
    prior_evaluation_at: datetime | None = None
    for item in sorted(
        cases,
        key=lambda value: (
            value.information_cutoff_at,
            value.evaluation_at,
            value.forecast_id,
        ),
    ):
        if prior_evaluation_at is not None and item.information_cutoff_at < prior_evaluation_at:
            continue
        selected.append(item)
        prior_evaluation_at = item.evaluation_at
    return tuple(selected)


def _brier(probabilities: tuple[tuple[str, Decimal], ...], realized: str) -> Decimal:
    return sum(
        (
            (probability - (Decimal("1") if bucket_id == realized else Decimal("0")))
            ** 2
            for bucket_id, probability in probabilities
        ),
        Decimal("0"),
    )


def _mean(values: tuple[Decimal, ...]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0")) / Decimal(len(values))
