"""Retrospective rejection test for one preregistered volatility-state prior."""

from __future__ import annotations

import hashlib
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
from investment_manager.research.forecast_prior import (
    SettledReturn,
    build_non_overlapping_returns,
)

_REGIMES = ("LOW", "MEDIUM", "HIGH")


class TemporalScore(FrozenModel):
    half: Literal["FIRST", "SECOND"]
    baseline_mean_ranked_probability_score: Decimal = Field(ge=0)
    candidate_mean_ranked_probability_score: Decimal = Field(ge=0)


class VolatilityStateTargetResult(FrozenModel):
    symbol: str
    validation_sample_count: int = Field(gt=0)
    feature_thresholds: tuple[Decimal, Decimal]
    terminal_regime_sample_counts: tuple[int, int, int]
    baseline_mean_brier: Decimal = Field(ge=0)
    candidate_mean_brier: Decimal = Field(ge=0)
    baseline_mean_ranked_probability_score: Decimal = Field(ge=0)
    candidate_mean_ranked_probability_score: Decimal = Field(ge=0)
    baseline_maximum_absolute_calibration_error: Decimal = Field(ge=0, le=1)
    candidate_maximum_absolute_calibration_error: Decimal = Field(ge=0, le=1)
    temporal_scores: tuple[TemporalScore, TemporalScore]
    gate_brier_improved: bool
    gate_ranked_probability_improved: bool
    gate_temporal_stability: bool
    gate_calibration_not_worse: bool

    @model_validator(mode="after")
    def thresholds_and_counts_are_valid(self):
        if self.feature_thresholds[0] >= self.feature_thresholds[1]:
            raise ValueError("波动状态阈值必须严格递增")
        if any(item < 1 for item in self.terminal_regime_sample_counts):
            raise ValueError("每个波动状态必须至少有一个终态样本")
        if tuple(item.half for item in self.temporal_scores) != ("FIRST", "SECOND"):
            raise ValueError("时间稳定性必须按前后半段唯一报告")
        return self


class VolatilityStatePriorArtifact(FrozenModel):
    schema_version: Literal["volatility-state-prior-result-v1"] = "volatility-state-prior-result-v1"
    artifact_id: str
    plan_id: Literal["volatility-state-prior-72h-v1"]
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    evaluator_code_version: str = Field(pattern=r"^[0-9a-f]{40}$")
    baseline_artifact_id: str
    evaluated_at: datetime
    permission: Literal["RETROSPECTIVE_REJECTION_ONLY"]
    status: Literal["PASSED_RETROSPECTIVE", "REJECTED_RETROSPECTIVE"]
    targets: tuple[VolatilityStateTargetResult, ...] = Field(min_length=2, max_length=2)
    rejection_reasons: tuple[str, ...]
    capital_change: Literal["NONE"] = "NONE"
    conclusion: str

    _utc_evaluated = field_validator("evaluated_at")(require_utc)

    @model_validator(mode="after")
    def identity_and_status_are_valid(self):
        if tuple(item.symbol for item in self.targets) != ("BTCUSDT", "PAXGUSDT"):
            raise ValueError("波动状态结果目标必须唯一固定为 BTC 与 PAXG")
        passed = all(
            target.gate_brier_improved
            and target.gate_ranked_probability_improved
            and target.gate_temporal_stability
            and target.gate_calibration_not_worse
            for target in self.targets
        )
        if (self.status == "PASSED_RETROSPECTIVE") != passed:
            raise ValueError("波动状态结果状态与逐目标门槛不一致")
        if passed == bool(self.rejection_reasons):
            raise ValueError("波动状态拒绝原因与结果不一致")
        expected = stable_id(
            "volatility_state_prior",
            content_hash(self.model_dump(mode="json", exclude={"artifact_id"})),
        )
        if self.artifact_id != expected:
            raise ValueError("波动状态结果 ID 与内容不一致")
        return self


def evaluate_volatility_state_prior(
    *,
    plan_path: Path,
    dataset_catalog: Path,
    plan_commit: str,
    evaluator_code_version: str,
    evaluated_at: datetime,
) -> VolatilityStatePriorArtifact:
    """Evaluate the frozen candidate once; a pass still grants no runtime permission."""

    plan_bytes = plan_path.read_bytes()
    plan = yaml.safe_load(plan_bytes)
    _validate_fixed_plan(plan)
    baseline_path = plan_path.parents[2] / plan["baseline"]["artifact_path"]
    if _sha256(baseline_path.read_bytes()) != plan["baseline"]["artifact_sha256"]:
        raise ValueError("波动状态计划绑定的 baseline 字节已经变化")
    baseline = load_forecast_baseline(baseline_path)
    if baseline.artifact_id != plan["baseline"]["artifact_id"]:
        raise ValueError("波动状态计划绑定的 baseline 身份不一致")
    catalog = HistoricalDatasetCatalog(dataset_catalog)
    baseline_by_symbol = {item.symbol: item for item in baseline.results}
    target_results = tuple(
        _evaluate_target(
            catalog.load(target["dataset_id"]),
            baseline=baseline_by_symbol[target["symbol"]],
            target=target,
            development_end=date.fromisoformat(
                str(plan["sample_contract"]["development_end_exclusive"])
            ),
            lookback_days=int(plan["feature"]["lookback_completed_days"]),
            shrinkage_strength=Decimal(str(plan["estimation"]["shrinkage_strength"])),
        )
        for target in plan["targets"]
    )
    reasons = tuple(
        f"{target.symbol}::{reason}"
        for target in target_results
        for reason, passed in (
            ("BRIER_NOT_IMPROVED", target.gate_brier_improved),
            ("RANKED_PROBABILITY_NOT_IMPROVED", target.gate_ranked_probability_improved),
            ("TEMPORAL_STABILITY_FAILED", target.gate_temporal_stability),
            ("CALIBRATION_WORSE", target.gate_calibration_not_worse),
        )
        if not passed
    )
    status = "REJECTED_RETROSPECTIVE" if reasons else "PASSED_RETROSPECTIVE"
    conclusion = (
        "30 日已实现波动状态在 BTC 与 PAXG 上同时改善了 72 小时分布质量；"
        "结果只允许建立新的前瞻研究 Producer，不授予资本权限。"
        if not reasons
        else "30 日已实现波动状态没有在 BTC 与 PAXG 上稳定改善 72 小时分布；"
        "候选不得进入现役 Quant prior，也不得在同一历史上搜索相邻参数。"
    )
    values = {
        "plan_id": plan["plan_id"],
        "plan_hash": _sha256(plan_bytes),
        "plan_commit": plan_commit,
        "evaluator_code_version": evaluator_code_version,
        "baseline_artifact_id": baseline.artifact_id,
        "evaluated_at": require_utc(evaluated_at),
        "permission": plan["permission"],
        "status": status,
        "targets": target_results,
        "rejection_reasons": reasons,
        "conclusion": conclusion,
    }
    provisional = VolatilityStatePriorArtifact.model_construct(
        artifact_id="pending",
        schema_version="volatility-state-prior-result-v1",
        capital_change="NONE",
        **values,
    )
    artifact_id = stable_id(
        "volatility_state_prior",
        content_hash(provisional.model_dump(mode="json", exclude={"artifact_id"})),
    )
    return VolatilityStatePriorArtifact(artifact_id=artifact_id, **values)


def store_volatility_state_prior(
    artifact: VolatilityStatePriorArtifact,
    *,
    root: Path,
) -> Path:
    target = root / f"{artifact.artifact_id}.json"
    if target.exists():
        existing = VolatilityStatePriorArtifact.model_validate_json(
            target.read_text(encoding="utf-8")
        )
        if existing != artifact:
            raise ValueError("同一波动状态制品 ID 的内容不一致")
        return target
    return write_json_artifact(
        root=root,
        target=target,
        prefix=".volatility-state-prior-",
        payload=artifact,
    )


def _evaluate_target(
    dataset: HistoricalDataset,
    *,
    baseline: ForecastBaselineTargetResult,
    target: dict[str, object],
    development_end: date,
    lookback_days: int,
    shrinkage_strength: Decimal,
) -> VolatilityStateTargetResult:
    if (
        dataset.manifest.symbol != target["symbol"]
        or dataset.manifest.dataset_id != target["dataset_id"]
        or dataset.manifest.bars_hash != target["bars_hash"]
        or baseline.dataset_id != target["dataset_id"]
        or baseline.bars_hash != target["bars_hash"]
    ):
        raise ValueError(f"{target['symbol']} 数据或 baseline 身份不一致")
    outcomes = build_non_overlapping_returns(
        dataset,
        horizon_days=3,
        phase_epoch=date(1970, 1, 1),
    )
    features = {
        outcome.information_cutoff_at: _trailing_mean_absolute_log_return(
            dataset,
            cutoff=outcome.information_cutoff_at,
            lookback_days=lookback_days,
        )
        for outcome in outcomes
    }
    boundary = datetime.combine(development_end, time.min, tzinfo=UTC)
    development_features = tuple(
        feature
        for outcome in outcomes
        if outcome.outcome_available_at < boundary
        and (feature := features[outcome.information_cutoff_at]) is not None
    )
    thresholds = (
        _nearest_rank(development_features, Decimal(1) / Decimal(3)),
        _nearest_rank(development_features, Decimal(2) / Decimal(3)),
    )
    if thresholds[0] >= thresholds[1]:
        raise ValueError(f"{target['symbol']} 开发样本无法形成唯一波动状态")
    validation = tuple(
        outcome
        for outcome in outcomes
        if outcome.information_cutoff_at >= boundary
        and features[outcome.information_cutoff_at] is not None
    )
    if not validation:
        raise ValueError(f"{target['symbol']} 没有可评价验证结果")
    baseline_brier: list[Decimal] = []
    candidate_brier: list[Decimal] = []
    baseline_ranked: list[Decimal] = []
    candidate_ranked: list[Decimal] = []
    realized_indexes: list[int] = []
    baseline_predictions: list[tuple[Decimal, ...]] = []
    candidate_predictions: list[tuple[Decimal, ...]] = []
    for outcome in validation:
        visible = tuple(
            item
            for item in outcomes
            if item.outcome_available_at <= outcome.information_cutoff_at
            and features[item.information_cutoff_at] is not None
        )
        unconditional_counts = _bucket_counts(visible, baseline.bucket_boundaries_bps)
        unconditional = probabilities_from_counts(unconditional_counts)
        current_regime = _regime(features[outcome.information_cutoff_at], thresholds)
        same_regime = tuple(
            item
            for item in visible
            if _regime(features[item.information_cutoff_at], thresholds) == current_regime
        )
        conditional_counts = _bucket_counts(same_regime, baseline.bucket_boundaries_bps)
        conditional = _shrunk_probabilities(
            conditional_counts,
            unconditional=unconditional,
            strength=shrinkage_strength,
        )
        realized_index = bisect_right(baseline.bucket_boundaries_bps, outcome.return_bps)
        realized_id = BUCKET_IDS[realized_index]
        baseline_pairs = tuple(zip(BUCKET_IDS, unconditional, strict=True))
        candidate_pairs = tuple(zip(BUCKET_IDS, conditional, strict=True))
        baseline_brier.append(multiclass_brier_score(baseline_pairs, realized_id))
        candidate_brier.append(multiclass_brier_score(candidate_pairs, realized_id))
        baseline_ranked.append(ordinal_ranked_probability_score(baseline_pairs, realized_id))
        candidate_ranked.append(ordinal_ranked_probability_score(candidate_pairs, realized_id))
        realized_indexes.append(realized_index)
        baseline_predictions.append(unconditional)
        candidate_predictions.append(conditional)
    baseline_mean_brier = _mean(tuple(baseline_brier))
    candidate_mean_brier = _mean(tuple(candidate_brier))
    baseline_mean_ranked = _mean(tuple(baseline_ranked))
    candidate_mean_ranked = _mean(tuple(candidate_ranked))
    midpoint = len(validation) // 2
    if midpoint < 1:
        raise ValueError(f"{target['symbol']} 验证样本无法分成两个时段")
    temporal_scores = tuple(
        TemporalScore(
            half=half,
            baseline_mean_ranked_probability_score=_mean(tuple(baseline_ranked[start:end])),
            candidate_mean_ranked_probability_score=_mean(tuple(candidate_ranked[start:end])),
        )
        for half, start, end in (
            ("FIRST", 0, midpoint),
            ("SECOND", midpoint, len(validation)),
        )
    )
    terminal_visible = tuple(
        item
        for item in outcomes
        if item.outcome_available_at <= dataset.manifest.last_close_time
        and features[item.information_cutoff_at] is not None
    )
    regime_counts = tuple(
        sum(
            _regime(features[item.information_cutoff_at], thresholds) == regime
            for item in terminal_visible
        )
        for regime in _REGIMES
    )
    baseline_calibration = _maximum_calibration_error(
        tuple(baseline_predictions), tuple(realized_indexes)
    )
    candidate_calibration = _maximum_calibration_error(
        tuple(candidate_predictions), tuple(realized_indexes)
    )
    return VolatilityStateTargetResult(
        symbol=dataset.manifest.symbol,
        validation_sample_count=len(validation),
        feature_thresholds=thresholds,
        terminal_regime_sample_counts=regime_counts,
        baseline_mean_brier=baseline_mean_brier,
        candidate_mean_brier=candidate_mean_brier,
        baseline_mean_ranked_probability_score=baseline_mean_ranked,
        candidate_mean_ranked_probability_score=candidate_mean_ranked,
        baseline_maximum_absolute_calibration_error=baseline_calibration,
        candidate_maximum_absolute_calibration_error=candidate_calibration,
        temporal_scores=temporal_scores,
        gate_brier_improved=candidate_mean_brier < baseline_mean_brier,
        gate_ranked_probability_improved=candidate_mean_ranked < baseline_mean_ranked,
        gate_temporal_stability=all(
            item.candidate_mean_ranked_probability_score
            <= item.baseline_mean_ranked_probability_score
            for item in temporal_scores
        ),
        gate_calibration_not_worse=candidate_calibration <= baseline_calibration,
    )


def _trailing_mean_absolute_log_return(
    dataset: HistoricalDataset,
    *,
    cutoff: datetime,
    lookback_days: int,
) -> Decimal | None:
    close_times = tuple(item.close_time for item in dataset.bars)
    end = bisect_right(close_times, cutoff)
    start = end - lookback_days - 1
    if start < 0:
        return None
    closes = tuple(item.close for item in dataset.bars[start:end])
    if len(closes) != lookback_days + 1:
        return None
    values = tuple(abs((current / previous).ln()) for previous, current in pairwise(closes))
    return sum(values, Decimal("0")) / Decimal(len(values))


def _bucket_counts(
    outcomes: tuple[SettledReturn, ...],
    boundaries: tuple[Decimal, ...],
) -> tuple[int, ...]:
    counts = [0] * len(BUCKET_IDS)
    for outcome in outcomes:
        counts[bisect_right(boundaries, outcome.return_bps)] += 1
    if not sum(counts):
        raise ValueError("波动状态 prior 没有可见历史")
    return tuple(counts)


def _shrunk_probabilities(
    counts: tuple[int, ...],
    *,
    unconditional: tuple[Decimal, ...],
    strength: Decimal,
) -> tuple[Decimal, ...]:
    denominator = Decimal(sum(counts)) + strength
    probabilities = [
        (Decimal(count) + strength * prior) / denominator
        for count, prior in zip(counts[:-1], unconditional[:-1], strict=True)
    ]
    probabilities.append(Decimal("1") - sum(probabilities, Decimal("0")))
    return tuple(probabilities)


def _regime(value: Decimal | None, thresholds: tuple[Decimal, Decimal]) -> str:
    if value is None:
        raise ValueError("波动状态特征缺失")
    if value <= thresholds[0]:
        return "LOW"
    if value <= thresholds[1]:
        return "MEDIUM"
    return "HIGH"


def _nearest_rank(values: tuple[Decimal, ...], quantile: Decimal) -> Decimal:
    if not values:
        raise ValueError("波动状态开发特征为空")
    ordered = tuple(sorted(values))
    rank = int((Decimal(len(ordered)) * quantile).to_integral_value(rounding=ROUND_CEILING))
    return ordered[max(rank, 1) - 1]


def _mean(values: tuple[Decimal, ...]) -> Decimal:
    if not values:
        raise ValueError("均值样本为空")
    return sum(values, Decimal("0")) / Decimal(len(values))


def _maximum_calibration_error(
    predictions: tuple[tuple[Decimal, ...], ...],
    realized_indexes: tuple[int, ...],
) -> Decimal:
    mean_prediction = tuple(
        _mean(tuple(item[index] for item in predictions)) for index in range(len(BUCKET_IDS))
    )
    realized = probabilities_from_counts(
        tuple(realized_indexes.count(index) for index in range(len(BUCKET_IDS)))
    )
    return max(
        abs(predicted - actual) for predicted, actual in zip(mean_prediction, realized, strict=True)
    )


def _validate_fixed_plan(plan: dict[str, object]) -> None:
    expected = {
        "schema_version": "volatility-state-prior-plan-v1",
        "plan_id": "volatility-state-prior-72h-v1",
        "permission": "RETROSPECTIVE_REJECTION_ONLY",
        "runtime_change_before_forward_evidence": "NONE",
    }
    for key, value in expected.items():
        if plan.get(key) != value:
            raise ValueError(f"波动状态计划 {key} 不符合冻结合同")
    if plan["feature"]["lookback_completed_days"] != 30:
        raise ValueError("波动状态计划 lookback 已偏离预登记值")
    if plan["estimation"]["shrinkage_strength"] != 20:
        raise ValueError("波动状态计划 shrinkage 已偏离预登记值")
    symbols = tuple(item["symbol"] for item in plan["targets"])
    if symbols != ("BTCUSDT", "PAXGUSDT"):
        raise ValueError("波动状态计划目标顺序或范围非法")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
