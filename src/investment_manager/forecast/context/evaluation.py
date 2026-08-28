"""Deterministic evidence summary for one immutable Context Forecast cohort."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from investment_manager.forecast.contracts import ForecastSlotStratum
from investment_manager.kernel.time import require_utc

FORECAST_EVIDENCE_EVALUATION_VERSION = "context-forecast-evidence-v6"
FORECAST_PAIR_EVALUATION_VERSION = "context-forecast-pair-evidence-v2"
DYNAMIC_BASELINE_MINIMUM_HISTORY = 5
DYNAMIC_BASELINE_PRIOR_STRENGTH = Decimal("3")
PAIRED_SKILL_INTERVAL_Z = Decimal("1.96")


class ForecastEvidenceStatus(StrEnum):
    NO_SETTLED_SAMPLES = "NO_SETTLED_SAMPLES"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    ABOVE_BENCHMARK = "ABOVE_BENCHMARK"
    BELOW_BENCHMARK = "BELOW_BENCHMARK"
    INCONCLUSIVE = "INCONCLUSIVE"
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
    cohort_key: str = "default"
    market_state_key: str | None = None
    outcome_available_at: datetime | None = None
    source_stratum: ForecastSlotStratum = ForecastSlotStratum.CADENCE_ONLY

    def __post_init__(self) -> None:
        if not self.cohort_key:
            raise ValueError("Forecast 评分 cohort_key 不能为空")
        require_utc(self.information_cutoff_at)
        require_utc(self.evaluation_at)
        if self.outcome_available_at is not None:
            require_utc(self.outcome_available_at)
            if self.outcome_available_at < self.evaluation_at:
                raise ValueError("Forecast Outcome 可见时间不能早于评价时间")
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
    evaluation_version: str
    status: ForecastEvidenceStatus
    terminal_result_count: int
    due_slot_count: int
    forecast_count: int
    no_estimate_count: int
    settled_forecast_count: int
    non_overlapping_sample_count: int
    required_non_overlapping_samples: int
    permission_evidence_eligible: bool
    mean_ranked_probability_score: Decimal | None
    benchmark_mean_ranked_probability_score: Decimal | None
    ranked_probability_skill: Decimal | None
    rolling_benchmark_mean_ranked_probability_score: Decimal | None
    rolling_ranked_probability_skill: Decimal | None
    rolling_ranked_probability_skill_lower_bound: Decimal | None
    rolling_ranked_probability_skill_upper_bound: Decimal | None
    market_benchmark_mean_ranked_probability_score: Decimal | None
    market_ranked_probability_skill: Decimal | None
    market_ranked_probability_skill_lower_bound: Decimal | None
    market_ranked_probability_skill_upper_bound: Decimal | None
    mean_brier_score: Decimal | None
    benchmark_mean_brier_score: Decimal | None
    brier_skill: Decimal | None
    rolling_benchmark_mean_brier_score: Decimal | None
    rolling_brier_skill: Decimal | None
    rolling_brier_skill_lower_bound: Decimal | None
    rolling_brier_skill_upper_bound: Decimal | None
    rolling_baseline_ready_count: int
    market_benchmark_mean_brier_score: Decimal | None
    market_brier_skill: Decimal | None
    market_brier_skill_lower_bound: Decimal | None
    market_brier_skill_upper_bound: Decimal | None
    market_baseline_ready_count: int
    mean_expected_gross_bps: Decimal | None
    mean_realized_gross_bps: Decimal | None
    mean_absolute_return_error_bps: Decimal | None
    expected_realized_return_correlation: Decimal | None
    source_evidence: tuple[ForecastSourceEvidence, ...] = ()

    @property
    def result_coverage(self) -> Decimal | None:
        if self.due_slot_count == 0:
            return None
        return Decimal(self.terminal_result_count) / Decimal(self.due_slot_count)


@dataclass(frozen=True, slots=True)
class ForecastSourceEvidence:
    stratum: ForecastSlotStratum
    evidence: ForecastEvidence


@dataclass(frozen=True, slots=True)
class ForecastPairPanelCase:
    """One jointly-produced target panel scored on a shared settled outcome set."""

    panel_id: str
    information_cutoff_at: datetime
    evaluation_at: datetime
    source_stratum: ForecastSlotStratum
    paired_target_count: int
    candidate_ranked_probability_score: Decimal
    comparator_ranked_probability_score: Decimal
    candidate_brier_score: Decimal
    comparator_brier_score: Decimal
    mean_max_bucket_probability_delta: Decimal
    mean_expected_gross_bps_delta: Decimal

    def __post_init__(self) -> None:
        if not self.panel_id or self.paired_target_count < 1:
            raise ValueError("Forecast 配对面板身份和目标数必须有效")
        require_utc(self.information_cutoff_at)
        require_utc(self.evaluation_at)
        if self.information_cutoff_at >= self.evaluation_at:
            raise ValueError("Forecast 配对面板截止时间必须早于评价时间")
        if min(
            self.candidate_ranked_probability_score,
            self.comparator_ranked_probability_score,
            self.candidate_brier_score,
            self.comparator_brier_score,
        ) < 0:
            raise ValueError("Forecast 配对 proper score 不能为负数")
        if not Decimal("0") <= self.mean_max_bucket_probability_delta <= Decimal("1"):
            raise ValueError("Forecast 配对概率变化必须位于 [0, 1]")

    @property
    def brier_improvement(self) -> Decimal:
        """Positive means the candidate beat the comparator on the same panel."""

        return self.comparator_brier_score - self.candidate_brier_score

    @property
    def ranked_probability_improvement(self) -> Decimal:
        """Positive means the candidate beat the comparator on ordered distance."""

        return (
            self.comparator_ranked_probability_score
            - self.candidate_ranked_probability_score
        )


@dataclass(frozen=True, slots=True)
class ForecastPairEvidence:
    evaluation_version: str
    settled_panel_count: int
    paired_target_count: int
    non_overlapping_panel_count: int
    mean_candidate_ranked_probability_score: Decimal | None
    mean_comparator_ranked_probability_score: Decimal | None
    mean_ranked_probability_improvement: Decimal | None
    ranked_probability_improvement_lower_bound: Decimal | None
    ranked_probability_improvement_upper_bound: Decimal | None
    mean_candidate_brier_score: Decimal | None
    mean_comparator_brier_score: Decimal | None
    mean_brier_improvement: Decimal | None
    brier_improvement_lower_bound: Decimal | None
    brier_improvement_upper_bound: Decimal | None
    candidate_better_panel_count: int
    equal_panel_count: int
    candidate_worse_panel_count: int
    mean_max_bucket_probability_delta: Decimal | None
    mean_expected_gross_bps_delta: Decimal | None


def evaluate_forecast_pair_evidence(
    cases: tuple[ForecastPairPanelCase, ...],
) -> ForecastPairEvidence:
    """Compare two producers only on their shared, settled, non-overlapping panels."""

    independent = select_non_overlapping_intervals(
        cases,
        identity=lambda item: item.panel_id,
        information_cutoff_at=lambda item: item.information_cutoff_at,
        evaluation_at=lambda item: item.evaluation_at,
        stratum=lambda item: item.source_stratum.value,
    )
    candidate_ranked_scores = tuple(
        item.candidate_ranked_probability_score for item in independent
    )
    comparator_ranked_scores = tuple(
        item.comparator_ranked_probability_score for item in independent
    )
    ranked_improvements = tuple(
        item.ranked_probability_improvement for item in independent
    )
    ranked_interval = (
        _mean_confidence_interval(ranked_improvements)
        if len(ranked_improvements) >= 2
        else None
    )
    candidate_scores = tuple(item.candidate_brier_score for item in independent)
    comparator_scores = tuple(item.comparator_brier_score for item in independent)
    improvements = tuple(item.brier_improvement for item in independent)
    interval = _mean_confidence_interval(improvements) if len(improvements) >= 2 else None
    return ForecastPairEvidence(
        evaluation_version=FORECAST_PAIR_EVALUATION_VERSION,
        settled_panel_count=len(cases),
        paired_target_count=sum(item.paired_target_count for item in cases),
        non_overlapping_panel_count=len(independent),
        mean_candidate_ranked_probability_score=_mean(candidate_ranked_scores),
        mean_comparator_ranked_probability_score=_mean(comparator_ranked_scores),
        mean_ranked_probability_improvement=_mean(ranked_improvements),
        ranked_probability_improvement_lower_bound=(
            None if ranked_interval is None else ranked_interval[0]
        ),
        ranked_probability_improvement_upper_bound=(
            None if ranked_interval is None else ranked_interval[1]
        ),
        mean_candidate_brier_score=_mean(candidate_scores),
        mean_comparator_brier_score=_mean(comparator_scores),
        mean_brier_improvement=_mean(improvements),
        brier_improvement_lower_bound=None if interval is None else interval[0],
        brier_improvement_upper_bound=None if interval is None else interval[1],
        candidate_better_panel_count=sum(item > 0 for item in ranked_improvements),
        equal_panel_count=sum(item == 0 for item in ranked_improvements),
        candidate_worse_panel_count=sum(item < 0 for item in ranked_improvements),
        mean_max_bucket_probability_delta=_mean(
            tuple(item.mean_max_bucket_probability_delta for item in independent)
        ),
        mean_expected_gross_bps_delta=_mean(
            tuple(item.mean_expected_gross_bps_delta for item in independent)
        ),
    )


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
            evaluation_version=FORECAST_EVIDENCE_EVALUATION_VERSION,
            status=ForecastEvidenceStatus.NO_SETTLED_SAMPLES,
            terminal_result_count=terminal,
            due_slot_count=due_slot_count,
            forecast_count=forecast_count,
            no_estimate_count=no_estimate_count,
            settled_forecast_count=len(cases),
            non_overlapping_sample_count=0,
            required_non_overlapping_samples=required_non_overlapping_samples,
            permission_evidence_eligible=permission_evidence_eligible,
            mean_ranked_probability_score=None,
            benchmark_mean_ranked_probability_score=None,
            ranked_probability_skill=None,
            rolling_benchmark_mean_ranked_probability_score=None,
            rolling_ranked_probability_skill=None,
            rolling_ranked_probability_skill_lower_bound=None,
            rolling_ranked_probability_skill_upper_bound=None,
            market_benchmark_mean_ranked_probability_score=None,
            market_ranked_probability_skill=None,
            market_ranked_probability_skill_lower_bound=None,
            market_ranked_probability_skill_upper_bound=None,
            mean_brier_score=None,
            benchmark_mean_brier_score=None,
            brier_skill=None,
            rolling_benchmark_mean_brier_score=None,
            rolling_brier_skill=None,
            rolling_brier_skill_lower_bound=None,
            rolling_brier_skill_upper_bound=None,
            rolling_baseline_ready_count=0,
            market_benchmark_mean_brier_score=None,
            market_brier_skill=None,
            market_brier_skill_lower_bound=None,
            market_brier_skill_upper_bound=None,
            market_baseline_ready_count=0,
            mean_expected_gross_bps=None,
            mean_realized_gross_bps=None,
            mean_absolute_return_error_bps=None,
            expected_realized_return_correlation=None,
        )

    model_ranked = _mean(
        tuple(
            ordinal_ranked_probability_score(
                item.probabilities,
                item.realized_bucket_id,
            )
            for item in independent
        )
    )
    benchmark_ranked = _mean(
        tuple(
            ordinal_ranked_probability_score(
                item.benchmark_probabilities,
                item.realized_bucket_id,
            )
            for item in independent
        )
    )
    model_brier = _mean(
        tuple(
            multiclass_brier_score(item.probabilities, item.realized_bucket_id)
            for item in independent
        )
    )
    benchmark_brier = _mean(
        tuple(
            multiclass_brier_score(
                item.benchmark_probabilities,
                item.realized_bucket_id,
            )
            for item in independent
        )
    )
    assert (
        model_ranked is not None
        and benchmark_ranked is not None
        and model_brier is not None
        and benchmark_brier is not None
    )
    model_ranked_scores = tuple(
        ordinal_ranked_probability_score(item.probabilities, item.realized_bucket_id)
        for item in independent
    )
    model_scores = tuple(
        multiclass_brier_score(item.probabilities, item.realized_bucket_id) for item in independent
    )
    rolling_cases = _dynamic_benchmarks(cases, independent, condition_on_market=False)
    market_cases = _dynamic_benchmarks(cases, independent, condition_on_market=True)
    rolling_ranked_scores = tuple(
        (
            ordinal_ranked_probability_score(probabilities, item.realized_bucket_id),
            model_score,
        )
        for (item, probabilities, ready), model_score in zip(
            rolling_cases,
            model_ranked_scores,
            strict=True,
        )
        if ready
    )
    market_ranked_scores = tuple(
        (
            ordinal_ranked_probability_score(probabilities, item.realized_bucket_id),
            model_score,
        )
        for (item, probabilities, ready), model_score in zip(
            market_cases,
            model_ranked_scores,
            strict=True,
        )
        if ready
    )
    rolling_scores = tuple(
        (
            multiclass_brier_score(probabilities, item.realized_bucket_id),
            model_score,
        )
        for (item, probabilities, ready), model_score in zip(
            rolling_cases,
            model_scores,
            strict=True,
        )
        if ready
    )
    market_scores = tuple(
        (
            multiclass_brier_score(probabilities, item.realized_bucket_id),
            model_score,
        )
        for (item, probabilities, ready), model_score in zip(
            market_cases,
            model_scores,
            strict=True,
        )
        if ready
    )
    rolling_ranked = _mean(
        tuple(baseline for baseline, _model in rolling_ranked_scores)
    )
    market_ranked = _mean(tuple(baseline for baseline, _model in market_ranked_scores))
    rolling_ranked_differences = tuple(
        baseline - model for baseline, model in rolling_ranked_scores
    )
    market_ranked_differences = tuple(
        baseline - model for baseline, model in market_ranked_scores
    )
    rolling_ranked_skill = _mean(rolling_ranked_differences)
    market_ranked_skill = _mean(market_ranked_differences)
    rolling_ranked_interval = _optional_mean_confidence_interval(
        rolling_ranked_differences
    )
    market_ranked_interval = _optional_mean_confidence_interval(
        market_ranked_differences
    )
    rolling_ranked_lower = (
        None if rolling_ranked_interval is None else rolling_ranked_interval[0]
    )
    rolling_ranked_upper = (
        None if rolling_ranked_interval is None else rolling_ranked_interval[1]
    )
    market_ranked_lower = (
        None if market_ranked_interval is None else market_ranked_interval[0]
    )
    market_ranked_upper = (
        None if market_ranked_interval is None else market_ranked_interval[1]
    )
    rolling_brier = _mean(tuple(baseline for baseline, _model in rolling_scores))
    market_brier = _mean(tuple(baseline for baseline, _model in market_scores))
    rolling_differences = tuple(baseline - model for baseline, model in rolling_scores)
    market_differences = tuple(baseline - model for baseline, model in market_scores)
    rolling_skill = _mean(rolling_differences)
    market_skill = _mean(market_differences)
    rolling_interval = _optional_mean_confidence_interval(rolling_differences)
    market_interval = _optional_mean_confidence_interval(market_differences)
    rolling_lower = None if rolling_interval is None else rolling_interval[0]
    rolling_upper = None if rolling_interval is None else rolling_interval[1]
    market_lower = None if market_interval is None else market_interval[0]
    market_upper = None if market_interval is None else market_interval[1]
    rolling_ready_count = sum(ready for _item, _probabilities, ready in rolling_cases)
    market_ready_count = sum(ready for _item, _probabilities, ready in market_cases)
    status = (
        ForecastEvidenceStatus.DIAGNOSTIC_ONLY
        if not permission_evidence_eligible
        else ForecastEvidenceStatus.INSUFFICIENT_EVIDENCE
        if (
            len(independent) < required_non_overlapping_samples
            or rolling_ready_count < required_non_overlapping_samples
            or market_ready_count < required_non_overlapping_samples
        )
        else ForecastEvidenceStatus.ABOVE_BENCHMARK
        if (
            rolling_ranked_lower is not None
            and market_ranked_lower is not None
            and rolling_ranked_lower > 0
            and market_ranked_lower > 0
        )
        else ForecastEvidenceStatus.BELOW_BENCHMARK
        if (
            (rolling_ranked_upper is not None and rolling_ranked_upper < 0)
            or (market_ranked_upper is not None and market_ranked_upper < 0)
        )
        else ForecastEvidenceStatus.INCONCLUSIVE
    )
    return ForecastEvidence(
        evaluation_version=FORECAST_EVIDENCE_EVALUATION_VERSION,
        status=status,
        terminal_result_count=terminal,
        due_slot_count=due_slot_count,
        forecast_count=forecast_count,
        no_estimate_count=no_estimate_count,
        settled_forecast_count=len(cases),
        non_overlapping_sample_count=len(independent),
        required_non_overlapping_samples=required_non_overlapping_samples,
        permission_evidence_eligible=permission_evidence_eligible,
        mean_ranked_probability_score=model_ranked,
        benchmark_mean_ranked_probability_score=benchmark_ranked,
        ranked_probability_skill=benchmark_ranked - model_ranked,
        rolling_benchmark_mean_ranked_probability_score=rolling_ranked,
        rolling_ranked_probability_skill=rolling_ranked_skill,
        rolling_ranked_probability_skill_lower_bound=rolling_ranked_lower,
        rolling_ranked_probability_skill_upper_bound=rolling_ranked_upper,
        market_benchmark_mean_ranked_probability_score=market_ranked,
        market_ranked_probability_skill=market_ranked_skill,
        market_ranked_probability_skill_lower_bound=market_ranked_lower,
        market_ranked_probability_skill_upper_bound=market_ranked_upper,
        mean_brier_score=model_brier,
        benchmark_mean_brier_score=benchmark_brier,
        brier_skill=benchmark_brier - model_brier,
        rolling_benchmark_mean_brier_score=rolling_brier,
        rolling_brier_skill=rolling_skill,
        rolling_brier_skill_lower_bound=rolling_lower,
        rolling_brier_skill_upper_bound=rolling_upper,
        rolling_baseline_ready_count=rolling_ready_count,
        market_benchmark_mean_brier_score=market_brier,
        market_brier_skill=market_skill,
        market_brier_skill_lower_bound=market_lower,
        market_brier_skill_upper_bound=market_upper,
        market_baseline_ready_count=market_ready_count,
        mean_expected_gross_bps=_mean(tuple(item.expected_gross_bps for item in independent)),
        mean_realized_gross_bps=_mean(tuple(item.realized_gross_bps for item in independent)),
        mean_absolute_return_error_bps=_mean(
            tuple(
                abs(item.expected_gross_bps - item.realized_gross_bps)
                for item in independent
            )
        ),
        expected_realized_return_correlation=_return_correlation(independent),
    )


def _return_correlation(
    cases: tuple[ForecastScoringCase, ...],
) -> Decimal | None:
    if len(cases) < 2:
        return None
    expected_mean = sum(
        (item.expected_gross_bps for item in cases), Decimal("0")
    ) / Decimal(len(cases))
    realized_mean = sum(
        (item.realized_gross_bps for item in cases), Decimal("0")
    ) / Decimal(len(cases))
    expected_variance = sum(
        ((item.expected_gross_bps - expected_mean) ** 2 for item in cases),
        Decimal("0"),
    )
    realized_variance = sum(
        ((item.realized_gross_bps - realized_mean) ** 2 for item in cases),
        Decimal("0"),
    )
    if expected_variance == 0 or realized_variance == 0:
        return None
    covariance = sum(
        (
            (item.expected_gross_bps - expected_mean)
            * (item.realized_gross_bps - realized_mean)
            for item in cases
        ),
        Decimal("0"),
    )
    correlation = covariance / (expected_variance * realized_variance).sqrt()
    return max(Decimal("-1"), min(Decimal("1"), correlation))


def _non_overlapping(cases: tuple[ForecastScoringCase, ...]) -> tuple[ForecastScoringCase, ...]:
    cohorts = tuple(sorted({item.cohort_key for item in cases}))
    return tuple(
        item
        for cohort in cohorts
        for item in select_non_overlapping_intervals(
            tuple(value for value in cases if value.cohort_key == cohort),
            identity=lambda value: value.forecast_id,
            information_cutoff_at=lambda value: value.information_cutoff_at,
            evaluation_at=lambda value: value.evaluation_at,
            stratum=lambda value: value.source_stratum.value,
        )
    )


def select_non_overlapping_intervals[T](
    values: tuple[T, ...],
    *,
    identity: Callable[[T], str],
    information_cutoff_at: Callable[[T], datetime],
    evaluation_at: Callable[[T], datetime],
    stratum: Callable[[T], str],
) -> tuple[T, ...]:
    """Greedily freeze one deterministic sample from overlapping outcome windows."""

    interval_keys: set[tuple[datetime, datetime, str]] = set()
    for item in values:
        interval_key = (
            information_cutoff_at(item),
            evaluation_at(item),
            stratum(item),
        )
        if interval_key in interval_keys:
            raise ValueError("同一 Forecast 来源层不能重复记录同一评价区间")
        interval_keys.add(interval_key)

    selected: list[T] = []
    prior_evaluation_at: datetime | None = None
    for item in sorted(
        values,
        key=lambda value: (
            information_cutoff_at(value),
            evaluation_at(value),
            stratum(value),
            identity(value),
        ),
    ):
        if prior_evaluation_at is not None and information_cutoff_at(item) < prior_evaluation_at:
            continue
        selected.append(item)
        prior_evaluation_at = evaluation_at(item)
    return tuple(selected)


def multiclass_brier_score(
    probabilities: tuple[tuple[str, Decimal], ...],
    realized: str,
) -> Decimal:
    if realized not in {bucket_id for bucket_id, _probability in probabilities}:
        raise ValueError("Brier 真实 bucket 不属于预测分布")
    return sum(
        (
            (probability - (Decimal("1") if bucket_id == realized else Decimal("0"))) ** 2
            for bucket_id, probability in probabilities
        ),
        Decimal("0"),
    )


def ordinal_ranked_probability_score(
    probabilities: tuple[tuple[str, Decimal], ...],
    realized: str,
) -> Decimal:
    """Normalized ranked probability score for an ordered outcome contract."""

    bucket_ids = tuple(bucket_id for bucket_id, _probability in probabilities)
    if realized not in bucket_ids:
        raise ValueError("有序概率真实 bucket 不属于预测分布")
    if len(bucket_ids) < 2:
        raise ValueError("有序概率评分至少需要两个 bucket")
    realized_index = bucket_ids.index(realized)
    cumulative = Decimal("0")
    score = Decimal("0")
    for index, (_bucket_id, probability) in enumerate(probabilities[:-1]):
        cumulative += probability
        observed_cumulative = Decimal("1") if realized_index <= index else Decimal("0")
        score += (cumulative - observed_cumulative) ** 2
    return score / Decimal(len(bucket_ids) - 1)


def _dynamic_benchmarks(
    all_cases: tuple[ForecastScoringCase, ...],
    evaluated_cases: tuple[ForecastScoringCase, ...],
    *,
    condition_on_market: bool,
) -> tuple[
    tuple[
        ForecastScoringCase,
        tuple[tuple[str, Decimal], ...],
        bool,
    ],
    ...,
]:
    """Build each baseline only from outcomes knowable at that case's cutoff.

    Five earlier outcomes are required before the empirical baseline replaces the
    frozen contract prior.  The rolling baseline uses all visible outcomes; the
    market baseline uses only the same frozen regime.  A three-case prior keeps
    the early empirical distribution finite without looking ahead.
    """

    results = []
    for current in evaluated_cases:
        visible = tuple(
            item
            for item in all_cases
            if item.cohort_key == current.cohort_key
            and item.forecast_id != current.forecast_id
            and (item.outcome_available_at or item.evaluation_at) <= current.information_cutoff_at
        )
        matched = tuple(
            item
            for item in visible
            if current.market_state_key is not None
            and item.market_state_key == current.market_state_key
        )
        history = matched if condition_on_market else visible
        if len(history) < DYNAMIC_BASELINE_MINIMUM_HISTORY:
            results.append((current, current.benchmark_probabilities, False))
            continue
        counts = {
            bucket_id: sum(item.realized_bucket_id == bucket_id for item in history)
            for bucket_id, _probability in current.benchmark_probabilities
        }
        denominator = Decimal(len(history)) + DYNAMIC_BASELINE_PRIOR_STRENGTH
        probabilities = tuple(
            (
                bucket_id,
                (Decimal(counts[bucket_id]) + DYNAMIC_BASELINE_PRIOR_STRENGTH * prior_probability)
                / denominator,
            )
            for bucket_id, prior_probability in current.benchmark_probabilities
        )
        results.append((current, probabilities, True))
    return tuple(results)


def _mean_confidence_interval(
    values: tuple[Decimal, ...],
) -> tuple[Decimal, Decimal]:
    """Return a conservative paired 95% normal interval for mean skill."""

    mean = _mean(values)
    assert mean is not None
    if len(values) < 2:
        return mean, mean
    variance = sum(
        ((item - mean) ** 2 for item in values),
        Decimal("0"),
    ) / Decimal(len(values) - 1)
    standard_error = (variance / Decimal(len(values))).sqrt()
    margin = PAIRED_SKILL_INTERVAL_Z * standard_error
    return mean - margin, mean + margin


def _optional_mean_confidence_interval(
    values: tuple[Decimal, ...],
) -> tuple[Decimal, Decimal] | None:
    if not values:
        return None
    return _mean_confidence_interval(values)


def _mean(values: tuple[Decimal, ...]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0")) / Decimal(len(values))
