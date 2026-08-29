"""Immutable artifact contract shared by offline baseline research and runtime."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.platform.artifacts import write_json_artifact

BUCKET_IDS = ("TAIL_LOSS", "LOSS", "MIDDLE", "GAIN", "TAIL_GAIN")


def probabilities_from_counts(counts: tuple[int, ...]) -> tuple[Decimal, ...]:
    total = sum(counts)
    if total < 1 or any(item < 0 for item in counts):
        raise ValueError("概率计数必须非负且总数为正")
    probabilities = [Decimal(count) / Decimal(total) for count in counts[:-1]]
    probabilities.append(Decimal("1") - sum(probabilities, Decimal("0")))
    return tuple(probabilities)


class ForecastBaselineTargetResult(FrozenModel):
    symbol: str
    dataset_id: str
    bars_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    development_sample_count: int = Field(gt=0)
    validation_sample_count: int = Field(gt=0)
    first_validation_cutoff_at: datetime
    last_validation_outcome_at: datetime
    bucket_ids: tuple[str, ...] = Field(min_length=5, max_length=5)
    bucket_boundaries_bps: tuple[Decimal, ...] = Field(min_length=4, max_length=4)
    representative_bps: tuple[Decimal, ...] = Field(min_length=5, max_length=5)
    fixed_probabilities: tuple[Decimal, ...] = Field(min_length=5, max_length=5)
    mean_rolling_probabilities: tuple[Decimal, ...] = Field(min_length=5, max_length=5)
    terminal_probabilities: tuple[Decimal, ...] = Field(min_length=5, max_length=5)
    terminal_bucket_counts: tuple[int, ...] = Field(min_length=5, max_length=5)
    realized_probabilities: tuple[Decimal, ...] = Field(min_length=5, max_length=5)
    minimum_visible_history_count: int = Field(gt=0)
    maximum_visible_history_count: int = Field(gt=0)
    terminal_history_count: int = Field(gt=0)
    rolling_mean_brier: Decimal = Field(ge=0)
    fixed_mean_brier: Decimal = Field(ge=0)
    rolling_mean_ranked_probability_score: Decimal = Field(ge=0)
    fixed_mean_ranked_probability_score: Decimal = Field(ge=0)
    rolling_maximum_absolute_calibration_error: Decimal = Field(ge=0, le=1)

    _utc_first = field_validator("first_validation_cutoff_at")(require_utc)
    _utc_last = field_validator("last_validation_outcome_at")(require_utc)

    @model_validator(mode="after")
    def distribution_and_bounds_are_valid(self):
        if self.bucket_ids != BUCKET_IDS:
            raise ValueError("预测先验 bucket 顺序不一致")
        if self.bucket_boundaries_bps != tuple(sorted(set(self.bucket_boundaries_bps))):
            raise ValueError("预测先验 bucket 边界必须唯一递增")
        for values in (
            self.fixed_probabilities,
            self.mean_rolling_probabilities,
            self.terminal_probabilities,
            self.realized_probabilities,
        ):
            if any(item < 0 or item > 1 for item in values) or sum(values) != 1:
                raise ValueError("预测先验概率必须位于 [0, 1] 且总和为 1")
        if self.minimum_visible_history_count > self.maximum_visible_history_count:
            raise ValueError("预测先验可见历史数量边界非法")
        if self.terminal_history_count < self.maximum_visible_history_count:
            raise ValueError("预测先验终态历史不能少于最后验证时点")
        if (
            sum(self.terminal_bucket_counts) != self.terminal_history_count
            or probabilities_from_counts(self.terminal_bucket_counts)
            != self.terminal_probabilities
        ):
            raise ValueError("预测先验终态计数与概率不一致")
        if self.first_validation_cutoff_at >= self.last_validation_outcome_at:
            raise ValueError("预测先验验证时间边界非法")
        return self


class ForecastBaselineArtifact(FrozenModel):
    schema_version: Literal["forecast-baseline-artifact-v2"] = "forecast-baseline-artifact-v2"
    artifact_id: str
    plan_id: str
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    evaluator_code_version: str = Field(pattern=r"^[0-9a-f]{40}$")
    evaluated_at: datetime
    status: Literal["VALID"]
    results: tuple[ForecastBaselineTargetResult, ...] = Field(min_length=1)
    capital_change: Literal["NONE"] = "NONE"
    historical_claim: str = Field(min_length=1)

    _utc_evaluated = field_validator("evaluated_at")(require_utc)

    @model_validator(mode="after")
    def identity_and_targets_are_canonical(self):
        symbols = tuple(item.symbol for item in self.results)
        if symbols != tuple(sorted(set(symbols))):
            raise ValueError("预测先验结果必须按 symbol 唯一排序")
        expected = stable_id(
            "forecast_baseline",
            content_hash(self.model_dump(mode="json", exclude={"artifact_id"})),
        )
        if self.artifact_id != expected:
            raise ValueError("预测先验制品 ID 与内容不一致")
        return self


def load_forecast_baseline(path: Path) -> ForecastBaselineArtifact:
    return ForecastBaselineArtifact.model_validate_json(path.read_text(encoding="utf-8"))


def store_forecast_baseline(artifact: ForecastBaselineArtifact, *, root: Path) -> Path:
    target = root / f"{artifact.artifact_id}.json"
    if target.exists():
        if load_forecast_baseline(target) != artifact:
            raise ValueError("同一预测先验制品 ID 的内容不一致")
        return target
    return write_json_artifact(
        root=root,
        target=target,
        prefix=".forecast-baseline-",
        payload=artifact,
    )
