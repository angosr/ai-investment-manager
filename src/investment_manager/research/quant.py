"""Chronological training of the transparent Quant Forecast baseline."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from investment_manager.forecast.context.evaluation import multiclass_brier_score
from investment_manager.forecast.contracts import ForecastContract
from investment_manager.forecast.quant.runtime import (
    QuantCandidateEvaluation,
    QuantCellDistribution,
    QuantFeatureThresholds,
    QuantFeatureVector,
    QuantForecastArtifact,
    quant_cell_key,
    quant_features_from_bars,
)
from investment_manager.forecast.results import ForecastBucketProbability
from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.market.models import InstrumentId
from investment_manager.research.dataset import HistoricalDataset

_CANDIDATE_MODELS = (
    "momentum",
    "momentum_volatility",
    "momentum_reversal_volatility",
)
_HORIZON_BARS = 48
_FEATURE_BARS = 49


@dataclass(frozen=True, slots=True)
class _TrainingSample:
    features: QuantFeatureVector
    realized_bucket_id: str


def train_quant_forecast_artifact(
    *,
    dataset: HistoricalDataset,
    contract: ForecastContract,
    reference_instrument: InstrumentId,
    training_cutoff_at: datetime,
    smoothing_strength: Decimal = Decimal("20"),
) -> QuantForecastArtifact:
    """Select on validation, report blind performance, then fit the future model."""

    cutoff = require_utc(training_cutoff_at)
    if smoothing_strength <= 0:
        raise ValueError("Quant smoothing strength 必须为正数")
    if dataset.manifest.interval != "5m":
        raise ValueError("Quant baseline 当前只接受冻结的 5m 数据")
    if dataset.manifest.symbol != reference_instrument.symbol:
        raise ValueError("Quant 数据集与参考产品 symbol 不一致")
    if dataset.manifest.source != "binance-rest-historical":
        raise ValueError("Quant Spot Outcome 只能使用 Binance Spot 历史")
    if contract.target.legs[0].instrument != reference_instrument:
        raise ValueError("Quant 合同与参考产品不一致")
    if contract.horizon_minutes != 240:
        raise ValueError("Quant artifact v1 只支持 4h ForecastContract")

    samples = _training_samples(dataset, contract=contract, cutoff=cutoff)
    if len(samples) < 1_000:
        raise ValueError("Quant baseline 至少需要 1,000 个逐小时可结算样本")
    development_end = len(samples) * 3 // 5
    validation_end = len(samples) * 4 // 5
    development = samples[:development_end]
    validation = samples[development_end:validation_end]
    blind = samples[validation_end:]
    if min(len(development), len(validation), len(blind)) < 100:
        raise ValueError("Quant chronological split 样本不足")

    thresholds = _feature_thresholds(development)
    candidates = tuple(
        _candidate_evaluation(
            model_name,
            training=development,
            evaluation=validation,
            thresholds=thresholds,
            contract=contract,
            smoothing_strength=smoothing_strength,
        )
        for model_name in _CANDIDATE_MODELS
    )
    validation_unconditional_brier = _score_distribution(
        validation,
        _global_distribution(development, contract=contract).probabilities,
    )
    selected = min(
        candidates,
        key=lambda item: (item.validation_brier, _CANDIDATE_MODELS.index(item.model_name)),
    )
    development_and_validation = (*development, *validation)
    selected_training_distributions = _fit_distributions(
        selected.model_name,
        development_and_validation,
        thresholds=thresholds,
        contract=contract,
        smoothing_strength=smoothing_strength,
    )
    selected_blind_brier = _score(
        blind,
        model_name=selected.model_name,
        thresholds=thresholds,
        distributions=selected_training_distributions,
    )
    blind_unconditional_brier = _score_distribution(
        blind,
        _global_distribution(
            development_and_validation,
            contract=contract,
        ).probabilities,
    )
    final_distributions = _fit_distributions(
        selected.model_name,
        samples,
        thresholds=thresholds,
        contract=contract,
        smoothing_strength=smoothing_strength,
    )
    values = {
        "contract_id": contract.contract_id,
        "outcome_family_id": contract.outcome_family_id,
        "reference_instrument_key": reference_instrument.key,
        "dataset_id": dataset.manifest.dataset_id,
        "bars_hash": dataset.manifest.bars_hash,
        "training_cutoff_at": cutoff,
        "dataset_last_close_at": max(
            item.close_time for item in dataset.bars if item.close_time <= cutoff
        ),
        "feature_thresholds": thresholds,
        "candidate_evaluations": candidates,
        "selected_model": selected.model_name,
        "smoothing_strength": smoothing_strength,
        "global_distribution": _global_distribution(samples, contract=contract),
        "cells": tuple(
            final_distributions[key] for key in sorted(final_distributions) if key != "GLOBAL"
        ),
        "development_sample_count": len(development),
        "validation_sample_count": len(validation),
        "blind_sample_count": len(blind),
        "validation_unconditional_brier": validation_unconditional_brier,
        "selected_blind_brier": selected_blind_brier,
        "blind_unconditional_brier": blind_unconditional_brier,
    }
    provisional = QuantForecastArtifact.model_construct(artifact_id="pending", **values)
    artifact_id = stable_id(
        "quant_forecast_artifact",
        provisional.model_dump(mode="json", exclude={"artifact_id"}),
    )
    return QuantForecastArtifact(artifact_id=artifact_id, **values)


def _training_samples(
    dataset: HistoricalDataset,
    *,
    contract: ForecastContract,
    cutoff: datetime,
) -> tuple[_TrainingSample, ...]:
    bars = dataset.bars
    results = []
    for index in range(_FEATURE_BARS - 1, len(bars) - _HORIZON_BARS):
        current = bars[index]
        future = bars[index + _HORIZON_BARS]
        if (
            current.close_time.minute != 59
            or current.close_time.second != 59
            or current.close_time > cutoff
        ):
            continue
        if future.close_time > cutoff:
            break
        window = tuple(item.to_market_bar() for item in bars[index - 48 : index + 1])
        features = quant_features_from_bars(window)
        realized_bps = (future.close / current.close - Decimal("1")) * Decimal("10000")
        bucket_id = next(
            (
                bucket.bucket_id
                for bucket in contract.outcome_buckets
                if (bucket.lower_bps is None or realized_bps >= bucket.lower_bps)
                and (bucket.upper_bps is None or realized_bps < bucket.upper_bps)
            ),
            None,
        )
        if bucket_id is None:
            raise ValueError("Quant 训练收益未落入 ForecastContract bucket")
        results.append(_TrainingSample(features=features, realized_bucket_id=bucket_id))
    return tuple(results)


def _feature_thresholds(samples: tuple[_TrainingSample, ...]) -> QuantFeatureThresholds:
    momentum = tuple(item.features.return_240m_bps for item in samples)
    short_return = tuple(item.features.return_60m_bps for item in samples)
    volatility = tuple(item.features.volatility_240m_bps for item in samples)
    return QuantFeatureThresholds(
        momentum_low_bps=_quantile(momentum, Decimal("0.333333333333333333")),
        momentum_high_bps=_quantile(momentum, Decimal("0.666666666666666667")),
        short_return_low_bps=_quantile(short_return, Decimal("0.333333333333333333")),
        short_return_high_bps=_quantile(short_return, Decimal("0.666666666666666667")),
        volatility_high_bps=_quantile(volatility, Decimal("0.5")),
    )


def _candidate_evaluation(
    model_name: str,
    *,
    training: tuple[_TrainingSample, ...],
    evaluation: tuple[_TrainingSample, ...],
    thresholds: QuantFeatureThresholds,
    contract: ForecastContract,
    smoothing_strength: Decimal,
) -> QuantCandidateEvaluation:
    distributions = _fit_distributions(
        model_name,
        training,
        thresholds=thresholds,
        contract=contract,
        smoothing_strength=smoothing_strength,
    )
    return QuantCandidateEvaluation(
        model_name=model_name,
        cell_count=len(distributions) - 1,
        validation_brier=_score(
            evaluation,
            model_name=model_name,
            thresholds=thresholds,
            distributions=distributions,
        ),
    )


def _fit_distributions(
    model_name: str,
    samples: tuple[_TrainingSample, ...],
    *,
    thresholds: QuantFeatureThresholds,
    contract: ForecastContract,
    smoothing_strength: Decimal,
) -> dict[str, QuantCellDistribution]:
    global_distribution = _global_distribution(samples, contract=contract)
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for sample in samples:
        counts[quant_cell_key(model_name, sample.features, thresholds)][
            sample.realized_bucket_id
        ] += 1
    result = {"GLOBAL": global_distribution}
    for key, cell_counts in counts.items():
        sample_count = sum(cell_counts.values())
        denominator = Decimal(sample_count) + smoothing_strength
        raw = tuple(
            (
                bucket.bucket_id,
                (
                    Decimal(cell_counts[bucket.bucket_id])
                    + smoothing_strength * prior.probability
                )
                / denominator,
            )
            for bucket, prior in zip(
                contract.outcome_buckets,
                global_distribution.probabilities,
                strict=True,
            )
        )
        result[key] = QuantCellDistribution(
            cell_key=key,
            sample_count=sample_count,
            probabilities=_normalized_probabilities(raw),
        )
    return result


def _global_distribution(
    samples: tuple[_TrainingSample, ...],
    *,
    contract: ForecastContract,
) -> QuantCellDistribution:
    counts = Counter(item.realized_bucket_id for item in samples)
    denominator = Decimal(len(samples))
    return QuantCellDistribution(
        cell_key="GLOBAL",
        sample_count=len(samples),
        probabilities=_normalized_probabilities(
            tuple(
                (bucket.bucket_id, Decimal(counts[bucket.bucket_id]) / denominator)
                for bucket in contract.outcome_buckets
            )
        ),
    )


def _score(
    samples: tuple[_TrainingSample, ...],
    *,
    model_name: str,
    thresholds: QuantFeatureThresholds,
    distributions: dict[str, QuantCellDistribution],
) -> Decimal:
    return sum(
        (
            multiclass_brier_score(
                tuple(
                    (item.bucket_id, item.probability)
                    for item in distributions.get(
                        quant_cell_key(model_name, sample.features, thresholds),
                        distributions["GLOBAL"],
                    ).probabilities
                ),
                sample.realized_bucket_id,
            )
            for sample in samples
        ),
        Decimal("0"),
    ) / Decimal(len(samples))


def _score_distribution(
    samples: tuple[_TrainingSample, ...],
    probabilities: tuple[ForecastBucketProbability, ...],
) -> Decimal:
    values = tuple((item.bucket_id, item.probability) for item in probabilities)
    return sum(
        (multiclass_brier_score(values, sample.realized_bucket_id) for sample in samples),
        Decimal("0"),
    ) / Decimal(len(samples))


def _normalized_probabilities(
    values: tuple[tuple[str, Decimal], ...],
) -> tuple[ForecastBucketProbability, ...]:
    if not values:
        raise ValueError("Quant 概率不能为空")
    normalized = []
    running = Decimal("0")
    for bucket_id, probability in values[:-1]:
        normalized.append(
            ForecastBucketProbability(bucket_id=bucket_id, probability=probability)
        )
        running += probability
    normalized.append(
        ForecastBucketProbability(
            bucket_id=values[-1][0],
            probability=Decimal("1") - running,
        )
    )
    return tuple(normalized)


def _quantile(values: tuple[Decimal, ...], probability: Decimal) -> Decimal:
    if not values or not Decimal("0") <= probability <= Decimal("1"):
        raise ValueError("Quant quantile 输入非法")
    ordered = sorted(values)
    index = int(Decimal(len(ordered) - 1) * probability)
    return ordered[index]


__all__ = ["train_quant_forecast_artifact"]
