"""Point-in-time unconditional priors for comparing WorldModel forecasts."""

from __future__ import annotations

from bisect import bisect_right
from datetime import UTC, date, datetime, time
from decimal import ROUND_CEILING, Decimal
from itertools import pairwise
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, field_validator, model_validator

from investment_manager.forecast.program.baseline import (
    BUCKET_IDS,
    ForecastBaselineArtifact,
    ForecastBaselineTargetResult,
    probabilities_from_counts,
)
from investment_manager.forecast.scoring import (
    multiclass_brier_score,
    ordinal_ranked_probability_score,
)
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.research.dataset import HistoricalDataset, HistoricalDatasetCatalog

_BPS = Decimal("10000")
_BUCKET_IDS = BUCKET_IDS


class ForecastBaselineTarget(FrozenModel):
    symbol: str = Field(pattern=r"^[A-Z0-9._-]+$")
    dataset_id: str = Field(min_length=1)
    bars_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ForecastBaselineSampleContract(FrozenModel):
    interval: Literal["1d"]
    horizon_days: int = Field(gt=0)
    slot_phase_epoch: date
    slot_phase_days: int = Field(gt=0)
    information_cutoff: Literal["completed_bar_close"]
    outcome: Literal["close_to_close_return_bps"]
    settlement_availability: Literal["outcome_endpoint_bar_close"]
    independence: Literal["non_overlapping_cadence_slots"]

    @model_validator(mode="after")
    def cadence_matches_horizon(self):
        if self.slot_phase_days != self.horizon_days:
            raise ValueError("预测先验只接受与 horizon 等长的非重叠 cadence")
        return self


class ForecastBaselineEstimation(FrozenModel):
    development_end_exclusive: date
    bucket_quantiles: tuple[Decimal, ...] = Field(min_length=4, max_length=4)
    quantile_method: Literal["empirical_nearest_rank"]
    representative: Literal["development_bucket_median"]
    fixed_benchmark: Literal["development_empirical_distribution"]
    forecast_prior: str = Field(min_length=1)

    @model_validator(mode="after")
    def quantiles_are_canonical(self):
        if self.bucket_quantiles != tuple(sorted(set(self.bucket_quantiles))):
            raise ValueError("预测先验分位点必须唯一递增")
        if any(item <= 0 or item >= 1 for item in self.bucket_quantiles):
            raise ValueError("预测先验分位点必须位于 (0, 1)")
        return self


class ForecastBaselineEvaluation(FrozenModel):
    period: Literal["from_development_boundary_to_last_settleable_slot"]
    metrics: tuple[
        Literal[
            "multiclass_brier",
            "ordinal_ranked_probability_score",
            "bucket_frequency_calibration",
        ],
        ...,
    ] = Field(min_length=3, max_length=3)
    validity_rule: str = Field(min_length=1)


class ForecastBaselineScope(FrozenModel):
    excluded_target: str = Field(min_length=1)
    capital_change: Literal["NONE"]
    historical_claim: str = Field(min_length=1)


class ForecastBaselinePlan(FrozenModel):
    schema_version: Literal["forecast-baseline-plan-v1"]
    plan_id: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    targets: tuple[ForecastBaselineTarget, ...] = Field(min_length=1)
    sample_contract: ForecastBaselineSampleContract
    estimation: ForecastBaselineEstimation
    evaluation: ForecastBaselineEvaluation
    scope: ForecastBaselineScope

    @model_validator(mode="after")
    def targets_are_unique_and_ordered(self):
        symbols = tuple(item.symbol for item in self.targets)
        if symbols != tuple(sorted(set(symbols))):
            raise ValueError("预测先验目标必须按 symbol 唯一排序")
        required_metrics = {
            "multiclass_brier",
            "ordinal_ranked_probability_score",
            "bucket_frequency_calibration",
        }
        if set(self.evaluation.metrics) != required_metrics:
            raise ValueError("预测先验评价指标必须完整且不得重复")
        return self


class SettledReturn(FrozenModel):
    information_cutoff_at: datetime
    outcome_available_at: datetime
    return_bps: Decimal

    _utc_cutoff = field_validator("information_cutoff_at")(require_utc)
    _utc_available = field_validator("outcome_available_at")(require_utc)

    @model_validator(mode="after")
    def outcome_follows_cutoff(self):
        if self.outcome_available_at <= self.information_cutoff_at:
            raise ValueError("预测结果必须晚于信息截止时点")
        return self


def load_forecast_baseline_plan(path: Path) -> ForecastBaselinePlan:
    return ForecastBaselinePlan.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def build_non_overlapping_returns(
    dataset: HistoricalDataset,
    *,
    horizon_days: int,
    phase_epoch: date,
) -> tuple[SettledReturn, ...]:
    if dataset.manifest.interval != "1d":
        raise ValueError("72 小时预测先验要求连续 1d K 线")
    if horizon_days < 1:
        raise ValueError("预测先验 horizon_days 必须为正数")
    bars = dataset.bars
    outcomes: list[SettledReturn] = []
    for index, bar in enumerate(bars[:-horizon_days]):
        if (bar.open_time.date() - phase_epoch).days % horizon_days:
            continue
        endpoint = bars[index + horizon_days]
        outcomes.append(
            SettledReturn(
                information_cutoff_at=bar.close_time,
                outcome_available_at=endpoint.close_time,
                return_bps=(endpoint.close / bar.close - 1) * _BPS,
            )
        )
    if len(outcomes) < 2:
        raise ValueError("预测先验没有足够的非重叠结果")
    for previous, current in pairwise(outcomes):
        if previous.outcome_available_at != current.information_cutoff_at:
            raise ValueError("预测先验 cadence 未形成连续、非重叠窗口")
    return tuple(outcomes)


def expanding_prior(
    outcomes: tuple[SettledReturn, ...],
    *,
    boundaries_bps: tuple[Decimal, ...],
    information_cutoff_at: datetime,
) -> tuple[tuple[Decimal, ...], int]:
    cutoff = require_utc(information_cutoff_at)
    visible = tuple(item for item in outcomes if item.outcome_available_at <= cutoff)
    if not visible:
        raise ValueError("预测先验在当前时点没有已结算历史")
    return _frequencies(
        tuple(_bucket_index(item.return_bps, boundaries_bps) for item in visible),
        len(boundaries_bps) + 1,
    ), len(visible)


def evaluate_forecast_baseline(
    plan: ForecastBaselinePlan,
    *,
    dataset_catalog: Path,
    plan_commit: str,
    evaluator_code_version: str,
    evaluated_at: datetime,
) -> ForecastBaselineArtifact:
    evaluated_at = require_utc(evaluated_at)
    boundary_at = datetime.combine(
        plan.estimation.development_end_exclusive,
        time.min,
        tzinfo=UTC,
    )
    catalog = HistoricalDatasetCatalog(dataset_catalog)
    results: list[ForecastBaselineTargetResult] = []
    for target in plan.targets:
        dataset = catalog.load(target.dataset_id)
        if (
            dataset.manifest.symbol != target.symbol
            or dataset.manifest.bars_hash != target.bars_hash
            or dataset.manifest.interval != plan.sample_contract.interval
        ):
            raise ValueError(f"{target.symbol} 数据身份与预登记计划不一致")
        outcomes = build_non_overlapping_returns(
            dataset,
            horizon_days=plan.sample_contract.horizon_days,
            phase_epoch=plan.sample_contract.slot_phase_epoch,
        )
        development = tuple(item for item in outcomes if item.outcome_available_at < boundary_at)
        validation = tuple(item for item in outcomes if item.information_cutoff_at >= boundary_at)
        if not development or not validation:
            raise ValueError(f"{target.symbol} 开发或验证结果为空")
        boundaries = tuple(
            _nearest_rank(tuple(item.return_bps for item in development), quantile)
            for quantile in plan.estimation.bucket_quantiles
        )
        if boundaries != tuple(sorted(set(boundaries))):
            raise ValueError(f"{target.symbol} 开发样本无法形成唯一递增 bucket")
        development_buckets = tuple(
            _bucket_index(item.return_bps, boundaries) for item in development
        )
        fixed = _frequencies(development_buckets, len(_BUCKET_IDS))
        representatives = tuple(
            _median(
                tuple(
                    item.return_bps
                    for item, bucket in zip(development, development_buckets, strict=True)
                    if bucket == bucket_index
                )
            )
            for bucket_index in range(len(_BUCKET_IDS))
        )
        rolling_predictions: list[tuple[Decimal, ...]] = []
        realized_buckets: list[int] = []
        visible_counts: list[int] = []
        rolling_brier: list[Decimal] = []
        fixed_brier: list[Decimal] = []
        rolling_ranked: list[Decimal] = []
        fixed_ranked: list[Decimal] = []
        fixed_pairs = tuple(zip(_BUCKET_IDS, fixed, strict=True))
        for item in validation:
            rolling, visible_count = expanding_prior(
                outcomes,
                boundaries_bps=boundaries,
                information_cutoff_at=item.information_cutoff_at,
            )
            realized_index = _bucket_index(item.return_bps, boundaries)
            realized_id = _BUCKET_IDS[realized_index]
            rolling_pairs = tuple(zip(_BUCKET_IDS, rolling, strict=True))
            rolling_predictions.append(rolling)
            realized_buckets.append(realized_index)
            visible_counts.append(visible_count)
            rolling_brier.append(multiclass_brier_score(rolling_pairs, realized_id))
            fixed_brier.append(multiclass_brier_score(fixed_pairs, realized_id))
            rolling_ranked.append(ordinal_ranked_probability_score(rolling_pairs, realized_id))
            fixed_ranked.append(ordinal_ranked_probability_score(fixed_pairs, realized_id))
        mean_rolling = _mean_distribution(tuple(rolling_predictions))
        realized = _frequencies(tuple(realized_buckets), len(_BUCKET_IDS))
        terminal, terminal_count = expanding_prior(
            outcomes,
            boundaries_bps=boundaries,
            information_cutoff_at=dataset.manifest.last_close_time,
        )
        terminal_bucket_counts = tuple(
            sum(
                item.outcome_available_at <= dataset.manifest.last_close_time
                and _bucket_index(item.return_bps, boundaries) == bucket_index
                for item in outcomes
            )
            for bucket_index in range(len(_BUCKET_IDS))
        )
        results.append(
            ForecastBaselineTargetResult(
                symbol=target.symbol,
                dataset_id=target.dataset_id,
                bars_hash=target.bars_hash,
                development_sample_count=len(development),
                validation_sample_count=len(validation),
                first_validation_cutoff_at=validation[0].information_cutoff_at,
                last_validation_outcome_at=validation[-1].outcome_available_at,
                bucket_ids=_BUCKET_IDS,
                bucket_boundaries_bps=boundaries,
                representative_bps=representatives,
                fixed_probabilities=fixed,
                mean_rolling_probabilities=mean_rolling,
                terminal_probabilities=terminal,
                terminal_bucket_counts=terminal_bucket_counts,
                realized_probabilities=realized,
                minimum_visible_history_count=min(visible_counts),
                maximum_visible_history_count=max(visible_counts),
                terminal_history_count=terminal_count,
                rolling_mean_brier=_mean(tuple(rolling_brier)),
                fixed_mean_brier=_mean(tuple(fixed_brier)),
                rolling_mean_ranked_probability_score=_mean(tuple(rolling_ranked)),
                fixed_mean_ranked_probability_score=_mean(tuple(fixed_ranked)),
                rolling_maximum_absolute_calibration_error=max(
                    abs(predicted - observed)
                    for predicted, observed in zip(mean_rolling, realized, strict=True)
                ),
            )
        )
    values = {
        "plan_id": plan.plan_id,
        "plan_hash": content_hash(plan),
        "plan_commit": plan_commit,
        "evaluator_code_version": evaluator_code_version,
        "evaluated_at": evaluated_at,
        "status": "VALID",
        "results": tuple(results),
        "capital_change": plan.scope.capital_change,
        "historical_claim": plan.scope.historical_claim,
    }
    pending = ForecastBaselineArtifact.model_construct(artifact_id="pending", **values)
    return ForecastBaselineArtifact(
        artifact_id=stable_id(
            "forecast_baseline",
            content_hash(pending.model_dump(mode="json", exclude={"artifact_id"})),
        ),
        **values,
    )


def _nearest_rank(values: tuple[Decimal, ...], quantile: Decimal) -> Decimal:
    if not values:
        raise ValueError("分位点样本不能为空")
    ordered = tuple(sorted(values))
    rank = int((Decimal(len(ordered)) * quantile).to_integral_value(rounding=ROUND_CEILING))
    return ordered[max(rank - 1, 0)]


def _bucket_index(value: Decimal, boundaries: tuple[Decimal, ...]) -> int:
    return bisect_right(boundaries, value)


def _frequencies(values: tuple[int, ...], bucket_count: int) -> tuple[Decimal, ...]:
    if not values:
        raise ValueError("概率频率样本不能为空")
    counts = tuple(values.count(index) for index in range(bucket_count))
    return probabilities_from_counts(counts)


def _median(values: tuple[Decimal, ...]) -> Decimal:
    if not values:
        raise ValueError("bucket 代表收益样本不能为空")
    ordered = tuple(sorted(values))
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _mean(values: tuple[Decimal, ...]) -> Decimal:
    if not values:
        raise ValueError("均值样本不能为空")
    return sum(values, Decimal("0")) / Decimal(len(values))


def _mean_distribution(values: tuple[tuple[Decimal, ...], ...]) -> tuple[Decimal, ...]:
    if not values:
        raise ValueError("概率分布样本不能为空")
    means = [
        _mean(tuple(distribution[index] for distribution in values))
        for index in range(len(values[0]) - 1)
    ]
    means.append(Decimal("1") - sum(means, Decimal("0")))
    return tuple(means)
