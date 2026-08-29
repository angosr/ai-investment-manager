"""Rejection-first evaluation for one sparse 72-hour conditional Quant prior."""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, field_validator, model_validator

from investment_manager.forecast.program.baseline import (
    ForecastBaselineTargetResult,
    load_forecast_baseline,
    probabilities_from_counts,
)
from investment_manager.forecast.scoring import (
    multiclass_brier_score,
    ordinal_ranked_probability_score,
)
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.platform.artifacts import write_json_artifact
from investment_manager.research.dataset import HistoricalDataset, HistoricalDatasetCatalog

_BPS = Decimal("10000")
_PHASE_EPOCH = date(1970, 1, 1)
_SHRINKAGE_OBSERVATIONS = Decimal("5")


class QuantPriorTarget(FrozenModel):
    symbol: str
    dataset_id: str
    bars_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class QuantPriorContract(FrozenModel):
    interval: Literal["1d"]
    horizon_days: Literal[3]
    information_cutoff: Literal["completed_bar_close"]
    outcome: Literal["close_to_close_return_bps"]
    buckets: Literal["frozen_by_baseline_artifact"]
    representatives: Literal["frozen_by_baseline_artifact"]
    evaluation_phases: tuple[int, ...] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def phases_are_complete(self):
        if self.evaluation_phases != (0, 1, 2):
            raise ValueError("72 小时 Quant 评价必须完整覆盖三个非重叠相位")
        return self


class QuantPriorChronology(FrozenModel):
    fit_end_exclusive: date
    selection_end_exclusive: date
    validation_end_exclusive: date
    held_out_end_exclusive: date
    purge_full_horizon_at_boundaries: Literal[True]

    @model_validator(mode="after")
    def boundaries_are_ordered(self):
        values = (
            self.fit_end_exclusive,
            self.selection_end_exclusive,
            self.validation_end_exclusive,
            self.held_out_end_exclusive,
        )
        if values != tuple(sorted(set(values))):
            raise ValueError("Quant 评价时间边界必须唯一递增")
        return self


class QuantExpertPlan(FrozenModel):
    expert_id: Literal[
        "time_series_momentum",
        "standardized_reversal",
        "har_realized_volatility",
    ]
    state: str
    role: str


class QuantProbabilityPlan(FrozenModel):
    expert_distribution: Literal["empirical_state_bucket_distribution"]
    shrinkage: Literal["development_global_distribution_with_five_effective_observations"]
    qualification: Literal[
        "selection_ranked_probability_score_strictly_below_unconditional"
    ]
    mixture: Literal["equal_weight_unconditional_and_selection_qualified_experts"]
    missing_state: Literal["unconditional_distribution"]
    online_adaptation: Literal["none"]


class QuantEvaluationPlan(FrozenModel):
    primary_metric: Literal["normalized_ranked_probability_score"]
    diagnostics: tuple[str, ...]
    validation_rule: str
    held_out_rule: str
    capital_rule: str


class QuantScopePlan(FrozenModel):
    candidate_count: Literal[1]
    result_permission: Literal["REJECTION_OR_FORWARD_RESEARCH_ONLY"]
    prior_search_evidence: tuple[str, ...] = Field(min_length=1)
    excluded_experts: str
    prohibited_follow_ups: str


class OrthogonalQuantPriorPlan(FrozenModel):
    schema_version: Literal["orthogonal-quant-prior-plan-v1"]
    plan_id: str
    evaluation_family_id: str
    baseline_artifact_id: str
    hypothesis: str
    targets: tuple[QuantPriorTarget, ...] = Field(min_length=1)
    contract: QuantPriorContract
    chronology: QuantPriorChronology
    experts: tuple[QuantExpertPlan, ...] = Field(min_length=3, max_length=3)
    probability_model: QuantProbabilityPlan
    evaluation: QuantEvaluationPlan
    scope: QuantScopePlan

    @model_validator(mode="after")
    def identities_are_complete(self):
        symbols = tuple(item.symbol for item in self.targets)
        if symbols != tuple(sorted(set(symbols))):
            raise ValueError("Quant prior 目标必须按 symbol 唯一排序")
        expert_ids = tuple(item.expert_id for item in self.experts)
        if expert_ids != (
            "time_series_momentum",
            "standardized_reversal",
            "har_realized_volatility",
        ):
            raise ValueError("Quant prior 专家集合或顺序与冻结计划不一致")
        return self


class ScorePanel(FrozenModel):
    sample_count: int = Field(gt=0)
    candidate_ranked_probability_score: Decimal = Field(ge=0)
    baseline_ranked_probability_score: Decimal = Field(ge=0)
    candidate_brier: Decimal = Field(ge=0)
    baseline_brier: Decimal = Field(ge=0)
    candidate_expected_return_mae_bps: Decimal = Field(ge=0)
    baseline_expected_return_mae_bps: Decimal = Field(ge=0)
    candidate_expected_realized_correlation: Decimal | None
    baseline_expected_realized_correlation: Decimal | None
    candidate_maximum_calibration_error: Decimal = Field(ge=0, le=1)
    baseline_maximum_calibration_error: Decimal = Field(ge=0, le=1)


class ExpertFit(FrozenModel):
    expert_id: str
    state_boundaries: tuple[Decimal, Decimal]
    state_probabilities: tuple[tuple[Decimal, ...], ...] = Field(min_length=3, max_length=3)
    har_coefficients: tuple[Decimal, ...] = ()
    selection_ranked_probability_score: Decimal = Field(ge=0)
    selection_baseline_ranked_probability_score: Decimal = Field(ge=0)
    qualified: bool

    @model_validator(mode="after")
    def distributions_are_valid(self):
        if self.state_boundaries != tuple(sorted(set(self.state_boundaries))):
            raise ValueError("Quant 状态边界必须唯一递增")
        for probabilities in self.state_probabilities:
            if (
                len(probabilities) != 5
                or any(item < 0 or item > 1 for item in probabilities)
                or sum(probabilities, Decimal("0")) != 1
            ):
                raise ValueError("Quant 专家必须输出共享五桶概率合同")
        return self


class QuantPriorTargetResult(FrozenModel):
    symbol: str
    dataset_id: str
    status: Literal[
        "REJECTED_ON_SELECTION",
        "REJECTED_ON_VALIDATION",
        "REJECTED_ON_HELD_OUT",
        "FORWARD_RESEARCH_ELIGIBLE",
    ]
    sample_counts: tuple[tuple[str, int], ...]
    bucket_ids: tuple[str, ...]
    bucket_boundaries_bps: tuple[Decimal, ...]
    representative_bps: tuple[Decimal, ...]
    experts: tuple[ExpertFit, ...]
    selected_experts: tuple[str, ...]
    validation: ScorePanel | None
    validation_phase_ranked_scores: tuple[tuple[int, Decimal, Decimal], ...]
    held_out_revealed: bool
    held_out: ScorePanel | None
    held_out_phase_ranked_scores: tuple[tuple[int, Decimal, Decimal], ...]


class OrthogonalQuantPriorArtifact(FrozenModel):
    schema_version: Literal["orthogonal-quant-prior-artifact-v1"] = (
        "orthogonal-quant-prior-artifact-v1"
    )
    artifact_id: str
    plan_id: str
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    evaluator_code_version: str = Field(pattern=r"^[0-9a-f]{40}$")
    evaluated_at: datetime
    baseline_artifact_id: str
    status: Literal["REJECTED", "FORWARD_RESEARCH_ELIGIBLE"]
    results: tuple[QuantPriorTargetResult, ...]
    capital_change: Literal["NONE"] = "NONE"

    _utc_evaluated = field_validator("evaluated_at")(require_utc)

    @model_validator(mode="after")
    def identity_is_stable(self):
        expected = stable_id(
            "orthogonal_quant_prior",
            content_hash(self.model_dump(mode="json", exclude={"artifact_id"})),
        )
        if self.artifact_id != expected:
            raise ValueError("Quant prior 制品 ID 与内容不一致")
        return self


@dataclass(frozen=True, slots=True)
class _Sample:
    cutoff_at: datetime
    outcome_at: datetime
    phase: int
    return_bps: Decimal
    future_variance: Decimal
    trend: Decimal
    reversal: Decimal
    har_inputs: tuple[Decimal, Decimal, Decimal]


@dataclass(frozen=True, slots=True)
class _Expert:
    expert_id: str
    boundaries: tuple[Decimal, Decimal]
    probabilities: tuple[tuple[Decimal, ...], ...]
    har_coefficients: tuple[Decimal, ...] = ()

    def distribution(self, sample: _Sample) -> tuple[Decimal, ...]:
        value = {
            "time_series_momentum": sample.trend,
            "standardized_reversal": sample.reversal,
            "har_realized_volatility": _har_prediction(
                self.har_coefficients, sample.har_inputs
            ),
        }[self.expert_id]
        return self.probabilities[bisect_right(self.boundaries, value)]


def load_orthogonal_quant_prior_plan(path: Path) -> OrthogonalQuantPriorPlan:
    return OrthogonalQuantPriorPlan.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )


def evaluate_orthogonal_quant_prior(
    plan: OrthogonalQuantPriorPlan,
    *,
    dataset_catalog: Path,
    baseline_catalog: Path,
    plan_commit: str,
    evaluator_code_version: str,
    evaluated_at: datetime,
) -> OrthogonalQuantPriorArtifact:
    baseline = load_forecast_baseline(
        baseline_catalog / f"{plan.baseline_artifact_id}.json"
    )
    if baseline.artifact_id != plan.baseline_artifact_id:
        raise ValueError("Quant 计划引用的共享 baseline 身份不一致")
    baseline_by_symbol = {item.symbol: item for item in baseline.results}
    results = tuple(
        _evaluate_target(
            plan,
            target,
            HistoricalDatasetCatalog(dataset_catalog).load(target.dataset_id),
            baseline_by_symbol.get(target.symbol),
        )
        for target in plan.targets
    )
    status = (
        "FORWARD_RESEARCH_ELIGIBLE"
        if any(item.status == "FORWARD_RESEARCH_ELIGIBLE" for item in results)
        else "REJECTED"
    )
    values = {
        "plan_id": plan.plan_id,
        "plan_hash": content_hash(plan),
        "plan_commit": plan_commit,
        "evaluator_code_version": evaluator_code_version,
        "evaluated_at": require_utc(evaluated_at),
        "baseline_artifact_id": baseline.artifact_id,
        "status": status,
        "results": results,
        "capital_change": "NONE",
    }
    pending = OrthogonalQuantPriorArtifact.model_construct(artifact_id="pending", **values)
    return OrthogonalQuantPriorArtifact(
        artifact_id=stable_id(
            "orthogonal_quant_prior",
            content_hash(pending.model_dump(mode="json", exclude={"artifact_id"})),
        ),
        **values,
    )


def store_orthogonal_quant_prior(
    artifact: OrthogonalQuantPriorArtifact, *, root: Path
) -> Path:
    target = root / f"{artifact.artifact_id}.json"
    return write_json_artifact(
        root=root,
        target=target,
        prefix=".orthogonal-quant-prior-",
        payload=artifact,
    )


def _evaluate_target(
    plan: OrthogonalQuantPriorPlan,
    target: QuantPriorTarget,
    dataset: HistoricalDataset,
    baseline: ForecastBaselineTargetResult | None,
) -> QuantPriorTargetResult:
    if baseline is None:
        raise ValueError(f"{target.symbol} 缺少共享 baseline contract")
    if (
        dataset.manifest.symbol != target.symbol
        or dataset.manifest.bars_hash != target.bars_hash
        or dataset.manifest.interval != plan.contract.interval
        or baseline.dataset_id != target.dataset_id
    ):
        raise ValueError(f"{target.symbol} 数据或共享 contract 身份不一致")
    samples = _samples(dataset, horizon_days=plan.contract.horizon_days)
    chronology = plan.chronology
    fit = _segment(samples, end=chronology.fit_end_exclusive)
    selection = _segment(
        samples,
        start=chronology.fit_end_exclusive,
        end=chronology.selection_end_exclusive,
    )
    validation = _segment(
        samples,
        start=chronology.selection_end_exclusive,
        end=chronology.validation_end_exclusive,
    )
    held_out = _segment(
        samples,
        start=chronology.validation_end_exclusive,
        end=chronology.held_out_end_exclusive,
    )
    if min(map(len, (fit, selection, validation, held_out))) < 30:
        raise ValueError(f"{target.symbol} 某个冻结时间段样本不足")

    boundaries = baseline.bucket_boundaries_bps
    bucket_ids = baseline.bucket_ids
    global_distribution = _distribution(fit, boundaries, len(bucket_ids))
    har_coefficients = _fit_har(fit)
    expert_values = {
        "time_series_momentum": tuple(item.trend for item in fit),
        "standardized_reversal": tuple(item.reversal for item in fit),
        "har_realized_volatility": tuple(
            _har_prediction(har_coefficients, item.har_inputs) for item in fit
        ),
    }
    experts: list[_Expert] = []
    expert_results: list[ExpertFit] = []
    selection_baselines = _rolling_baselines(samples, selection, boundaries, len(bucket_ids))
    baseline_selection_score = _mean_rps(
        selection,
        selection_baselines,
        boundaries,
        bucket_ids,
    )
    for expert_id in (item.expert_id for item in plan.experts):
        state_boundaries = _tertiles(expert_values[expert_id])
        state_probabilities = _state_distributions(
            fit,
            expert_id=expert_id,
            state_boundaries=state_boundaries,
            har_coefficients=har_coefficients,
            outcome_boundaries=boundaries,
            bucket_count=len(bucket_ids),
            global_distribution=global_distribution,
        )
        expert = _Expert(
            expert_id=expert_id,
            boundaries=state_boundaries,
            probabilities=state_probabilities,
            har_coefficients=har_coefficients
            if expert_id == "har_realized_volatility"
            else (),
        )
        expert_score = _mean_rps(
            selection,
            tuple(expert.distribution(item) for item in selection),
            boundaries,
            bucket_ids,
        )
        qualified = expert_score < baseline_selection_score
        if qualified:
            experts.append(expert)
        expert_results.append(
            ExpertFit(
                expert_id=expert_id,
                state_boundaries=state_boundaries,
                state_probabilities=state_probabilities,
                har_coefficients=expert.har_coefficients,
                selection_ranked_probability_score=expert_score,
                selection_baseline_ranked_probability_score=baseline_selection_score,
                qualified=qualified,
            )
        )

    sample_counts = (
        ("fit", len(fit)),
        ("selection", len(selection)),
        ("validation", len(validation)),
        ("held_out", len(held_out)),
    )
    common = {
        "symbol": target.symbol,
        "dataset_id": target.dataset_id,
        "sample_counts": sample_counts,
        "bucket_ids": bucket_ids,
        "bucket_boundaries_bps": boundaries,
        "representative_bps": baseline.representative_bps,
        "experts": tuple(expert_results),
        "selected_experts": tuple(item.expert_id for item in experts),
    }
    if not experts:
        return QuantPriorTargetResult(
            status="REJECTED_ON_SELECTION",
            validation=None,
            validation_phase_ranked_scores=(),
            held_out_revealed=False,
            held_out=None,
            held_out_phase_ranked_scores=(),
            **common,
        )

    validation_baselines = _rolling_baselines(
        samples, validation, boundaries, len(bucket_ids)
    )
    validation_candidates = tuple(
        _mixture(
            (baseline_distribution, *(expert.distribution(sample) for expert in experts))
        )
        for sample, baseline_distribution in zip(
            validation, validation_baselines, strict=True
        )
    )
    validation_panel = _score_panel(
        validation,
        validation_candidates,
        validation_baselines,
        boundaries,
        bucket_ids,
        baseline.representative_bps,
    )
    validation_phases = _phase_scores(
        validation,
        validation_candidates,
        validation_baselines,
        boundaries,
        bucket_ids,
    )
    validation_passed = (
        validation_panel.candidate_ranked_probability_score
        < validation_panel.baseline_ranked_probability_score
        and all(candidate < comparator for _phase, candidate, comparator in validation_phases)
    )
    if not validation_passed:
        return QuantPriorTargetResult(
            status="REJECTED_ON_VALIDATION",
            validation=validation_panel,
            validation_phase_ranked_scores=validation_phases,
            held_out_revealed=False,
            held_out=None,
            held_out_phase_ranked_scores=(),
            **common,
        )

    held_out_baselines = _rolling_baselines(samples, held_out, boundaries, len(bucket_ids))
    held_out_candidates = tuple(
        _mixture(
            (baseline_distribution, *(expert.distribution(sample) for expert in experts))
        )
        for sample, baseline_distribution in zip(held_out, held_out_baselines, strict=True)
    )
    held_out_panel = _score_panel(
        held_out,
        held_out_candidates,
        held_out_baselines,
        boundaries,
        bucket_ids,
        baseline.representative_bps,
    )
    held_out_phases = _phase_scores(
        held_out,
        held_out_candidates,
        held_out_baselines,
        boundaries,
        bucket_ids,
    )
    held_out_passed = (
        held_out_panel.candidate_ranked_probability_score
        < held_out_panel.baseline_ranked_probability_score
        and all(candidate < comparator for _phase, candidate, comparator in held_out_phases)
    )
    return QuantPriorTargetResult(
        status=(
            "FORWARD_RESEARCH_ELIGIBLE" if held_out_passed else "REJECTED_ON_HELD_OUT"
        ),
        validation=validation_panel,
        validation_phase_ranked_scores=validation_phases,
        held_out_revealed=True,
        held_out=held_out_panel,
        held_out_phase_ranked_scores=held_out_phases,
        **common,
    )


def _samples(dataset: HistoricalDataset, *, horizon_days: int) -> tuple[_Sample, ...]:
    bars = dataset.bars
    closes = tuple(item.close for item in bars)
    returns = tuple(closes[index] / closes[index - 1] - 1 for index in range(1, len(bars)))
    prepared: list[_Sample] = []
    for index in range(22, len(bars) - horizon_days):
        daily = returns[index - 20 : index]
        variance = sum((item * item for item in daily), Decimal("0")) / Decimal(len(daily))
        volatility = variance.sqrt()
        if volatility == 0:
            continue
        future_daily = returns[index : index + horizon_days]
        cutoff = bars[index]
        endpoint = bars[index + horizon_days]
        prepared.append(
            _Sample(
                cutoff_at=cutoff.close_time,
                outcome_at=endpoint.close_time,
                phase=(cutoff.open_time.date() - _PHASE_EPOCH).days % horizon_days,
                return_bps=(endpoint.close / cutoff.close - 1) * _BPS,
                future_variance=sum(
                    (item * item for item in future_daily), Decimal("0")
                ),
                trend=closes[index] / closes[index - 20] - 1,
                reversal=(closes[index] / closes[index - 3] - 1) / volatility,
                har_inputs=(
                    returns[index - 1] ** 2,
                    sum((item * item for item in returns[index - 5 : index]), Decimal("0"))
                    / Decimal("5"),
                    sum((item * item for item in returns[index - 22 : index]), Decimal("0"))
                    / Decimal("22"),
                ),
            )
        )
    return tuple(prepared)


def _segment(
    samples: tuple[_Sample, ...], *, end: date, start: date | None = None
) -> tuple[_Sample, ...]:
    return tuple(
        item
        for item in samples
        if (start is None or item.cutoff_at.date() >= start)
        and item.cutoff_at.date() < end
        and item.outcome_at.date() < end
    )


def _distribution(
    samples: tuple[_Sample, ...], boundaries: tuple[Decimal, ...], bucket_count: int
) -> tuple[Decimal, ...]:
    return probabilities_from_counts(
        tuple(
            sum(bisect_right(boundaries, item.return_bps) == index for item in samples)
            for index in range(bucket_count)
        )
    )


def _tertiles(values: tuple[Decimal, ...]) -> tuple[Decimal, Decimal]:
    return (_nearest_rank(values, Decimal(1) / 3), _nearest_rank(values, Decimal(2) / 3))


def _nearest_rank(values: tuple[Decimal, ...], quantile: Decimal) -> Decimal:
    ordered = tuple(sorted(values))
    if not ordered:
        raise ValueError("Quant 状态阈值样本为空")
    rank = int((Decimal(len(ordered)) * quantile).to_integral_value(rounding=ROUND_CEILING))
    return ordered[max(rank - 1, 0)]


def _fit_har(samples: tuple[_Sample, ...]) -> tuple[Decimal, ...]:
    rows = tuple((Decimal("1"), *item.har_inputs) for item in samples)
    matrix = [
        [sum((row[i] * row[j] for row in rows), Decimal("0")) for j in range(4)]
        + [
            sum(
                (
                    row[i] * item.future_variance
                    for row, item in zip(rows, samples, strict=True)
                ),
                Decimal("0"),
            )
        ]
        for i in range(4)
    ]
    for pivot in range(4):
        swap = max(range(pivot, 4), key=lambda index: abs(matrix[index][pivot]))
        if matrix[swap][pivot] == 0:
            raise ValueError("HAR-RV 设计矩阵奇异")
        matrix[pivot], matrix[swap] = matrix[swap], matrix[pivot]
        divisor = matrix[pivot][pivot]
        matrix[pivot] = [item / divisor for item in matrix[pivot]]
        for row_index in range(4):
            if row_index == pivot:
                continue
            factor = matrix[row_index][pivot]
            matrix[row_index] = [
                current - factor * reference
                for current, reference in zip(matrix[row_index], matrix[pivot], strict=True)
            ]
    return tuple(matrix[index][-1] for index in range(4))


def _har_prediction(
    coefficients: tuple[Decimal, ...], inputs: tuple[Decimal, Decimal, Decimal]
) -> Decimal:
    if len(coefficients) != 4:
        raise ValueError("HAR-RV 系数不完整")
    return max(
        Decimal("0"),
        coefficients[0]
        + sum(
            (
                coefficient * value
                for coefficient, value in zip(
                    coefficients[1:], inputs, strict=True
                )
            ),
            Decimal("0"),
        ),
    )


def _state_distributions(
    samples: tuple[_Sample, ...],
    *,
    expert_id: str,
    state_boundaries: tuple[Decimal, Decimal],
    har_coefficients: tuple[Decimal, ...],
    outcome_boundaries: tuple[Decimal, ...],
    bucket_count: int,
    global_distribution: tuple[Decimal, ...],
) -> tuple[tuple[Decimal, ...], ...]:
    states: list[list[int]] = [[], [], []]
    for sample in samples:
        value = {
            "time_series_momentum": sample.trend,
            "standardized_reversal": sample.reversal,
            "har_realized_volatility": _har_prediction(
                har_coefficients, sample.har_inputs
            ),
        }[expert_id]
        states[bisect_right(state_boundaries, value)].append(
            bisect_right(outcome_boundaries, sample.return_bps)
        )
    distributions = []
    for outcomes in states:
        denominator = Decimal(len(outcomes)) + _SHRINKAGE_OBSERVATIONS
        values = [
            (Decimal(outcomes.count(index)) + _SHRINKAGE_OBSERVATIONS * global_distribution[index])
            / denominator
            for index in range(bucket_count - 1)
        ]
        values.append(Decimal("1") - sum(values, Decimal("0")))
        distributions.append(tuple(values))
    return tuple(distributions)


def _rolling_baselines(
    all_samples: tuple[_Sample, ...],
    targets: tuple[_Sample, ...],
    boundaries: tuple[Decimal, ...],
    bucket_count: int,
) -> tuple[tuple[Decimal, ...], ...]:
    results = []
    for target in targets:
        visible = tuple(
            item
            for item in all_samples
            if item.phase == target.phase and item.outcome_at <= target.cutoff_at
        )
        results.append(_distribution(visible, boundaries, bucket_count))
    return tuple(results)


def _mixture(distributions: tuple[tuple[Decimal, ...], ...]) -> tuple[Decimal, ...]:
    count = Decimal(len(distributions))
    values = [
        sum((distribution[index] for distribution in distributions), Decimal("0")) / count
        for index in range(len(distributions[0]) - 1)
    ]
    values.append(Decimal("1") - sum(values, Decimal("0")))
    return tuple(values)


def _mean_rps(
    samples: tuple[_Sample, ...],
    predictions: tuple[tuple[Decimal, ...], ...],
    boundaries: tuple[Decimal, ...],
    bucket_ids: tuple[str, ...],
) -> Decimal:
    return sum(
        (
            ordinal_ranked_probability_score(
                tuple(zip(bucket_ids, prediction, strict=True)),
                bucket_ids[bisect_right(boundaries, sample.return_bps)],
            )
            for sample, prediction in zip(samples, predictions, strict=True)
        ),
        Decimal("0"),
    ) / Decimal(len(samples))


def _phase_scores(
    samples: tuple[_Sample, ...],
    candidates: tuple[tuple[Decimal, ...], ...],
    baselines: tuple[tuple[Decimal, ...], ...],
    boundaries: tuple[Decimal, ...],
    bucket_ids: tuple[str, ...],
) -> tuple[tuple[int, Decimal, Decimal], ...]:
    results = []
    for phase in range(3):
        indexes = tuple(index for index, item in enumerate(samples) if item.phase == phase)
        phase_samples = tuple(samples[index] for index in indexes)
        results.append(
            (
                phase,
                _mean_rps(
                    phase_samples,
                    tuple(candidates[index] for index in indexes),
                    boundaries,
                    bucket_ids,
                ),
                _mean_rps(
                    phase_samples,
                    tuple(baselines[index] for index in indexes),
                    boundaries,
                    bucket_ids,
                ),
            )
        )
    return tuple(results)


def _score_panel(
    samples: tuple[_Sample, ...],
    candidates: tuple[tuple[Decimal, ...], ...],
    baselines: tuple[tuple[Decimal, ...], ...],
    boundaries: tuple[Decimal, ...],
    bucket_ids: tuple[str, ...],
    representative_bps: tuple[Decimal, ...],
) -> ScorePanel:
    realized_ids = tuple(
        bucket_ids[bisect_right(boundaries, item.return_bps)] for item in samples
    )

    def brier(predictions: tuple[tuple[Decimal, ...], ...]) -> Decimal:
        return sum(
            (
                multiclass_brier_score(
                    tuple(zip(bucket_ids, prediction, strict=True)), realized
                )
                for prediction, realized in zip(predictions, realized_ids, strict=True)
            ),
            Decimal("0"),
        ) / Decimal(len(samples))

    def expected(predictions: tuple[tuple[Decimal, ...], ...]) -> tuple[Decimal, ...]:
        return tuple(
            sum(
                (
                    probability * representative
                    for probability, representative in zip(
                        prediction, representative_bps, strict=True
                    )
                ),
                Decimal("0"),
            )
            for prediction in predictions
        )

    def calibration(predictions: tuple[tuple[Decimal, ...], ...]) -> Decimal:
        return max(
            abs(
                sum((item[index] for item in predictions), Decimal("0"))
                / Decimal(len(predictions))
                - Decimal(realized_ids.count(bucket_id)) / Decimal(len(realized_ids))
            )
            for index, bucket_id in enumerate(bucket_ids)
        )

    candidate_expected = expected(candidates)
    baseline_expected = expected(baselines)
    realized = tuple(item.return_bps for item in samples)
    return ScorePanel(
        sample_count=len(samples),
        candidate_ranked_probability_score=_mean_rps(
            samples, candidates, boundaries, bucket_ids
        ),
        baseline_ranked_probability_score=_mean_rps(
            samples, baselines, boundaries, bucket_ids
        ),
        candidate_brier=brier(candidates),
        baseline_brier=brier(baselines),
        candidate_expected_return_mae_bps=_mae(candidate_expected, realized),
        baseline_expected_return_mae_bps=_mae(baseline_expected, realized),
        candidate_expected_realized_correlation=_correlation(candidate_expected, realized),
        baseline_expected_realized_correlation=_correlation(baseline_expected, realized),
        candidate_maximum_calibration_error=calibration(candidates),
        baseline_maximum_calibration_error=calibration(baselines),
    )


def _mae(predicted: tuple[Decimal, ...], realized: tuple[Decimal, ...]) -> Decimal:
    return sum(
        (abs(left - right) for left, right in zip(predicted, realized, strict=True)),
        Decimal("0"),
    ) / Decimal(len(predicted))


def _correlation(left: tuple[Decimal, ...], right: tuple[Decimal, ...]) -> Decimal | None:
    left_float = tuple(float(item) for item in left)
    right_float = tuple(float(item) for item in right)
    left_mean = sum(left_float) / len(left_float)
    right_mean = sum(right_float) / len(right_float)
    numerator = sum(
        (a - left_mean) * (b - right_mean)
        for a, b in zip(left_float, right_float, strict=True)
    )
    left_scale = math.sqrt(sum((item - left_mean) ** 2 for item in left_float))
    right_scale = math.sqrt(sum((item - right_mean) ** 2 for item in right_float))
    if left_scale == 0 or right_scale == 0:
        return None
    return Decimal(str(numerator / (left_scale * right_scale)))
