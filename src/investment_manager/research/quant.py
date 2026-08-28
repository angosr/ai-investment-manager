"""Chronological training of the transparent Quant Forecast baseline."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from investment_manager.forecast.context.evaluation import (
    multiclass_brier_score,
    ordinal_ranked_probability_score,
)
from investment_manager.forecast.contracts import ForecastContract
from investment_manager.forecast.quant.runtime import (
    QuantCandidateEvaluation,
    QuantCellDistribution,
    QuantFeatureThresholds,
    QuantFeatureVector,
    QuantForecastArtifact,
    QuantHistoricalCapitalFeasibility,
    quant_cell_key,
    quant_features_from_bars,
    quant_phase_score_standard_error,
)
from investment_manager.forecast.results import ForecastBucketProbability
from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.market.models import InstrumentId, InstrumentProduct
from investment_manager.research.dataset import HistoricalDataset

_CANDIDATE_MODELS = (
    "momentum",
    "momentum_volatility",
    "momentum_reversal_volatility",
)
_HORIZON_BARS = 48
_FEATURE_BARS = 49
_OUTCOME_HORIZON = timedelta(hours=4)
_NON_OVERLAPPING_PHASES = 4
_OUTCOME_BUCKET_IDS = (
    "EXTREME_LOSS",
    "LARGE_LOSS",
    "LOSS",
    "SMALL_LOSS",
    "NEUTRAL",
    "SMALL_GAIN",
    "GAIN",
    "LARGE_GAIN",
    "EXTREME_GAIN",
)
_OUTCOME_BOUNDARY_QUANTILES = tuple(
    map(Decimal, ("0.05", "0.15", "0.30", "0.45", "0.55", "0.70", "0.85", "0.95"))
)
_OUTCOME_BENCHMARK_PROBABILITIES = tuple(
    map(Decimal, ("0.05", "0.10", "0.15", "0.15", "0.10", "0.15", "0.15", "0.10", "0.05"))
)


@dataclass(frozen=True, slots=True)
class _RawTrainingSample:
    features: QuantFeatureVector
    realized_bps: Decimal


@dataclass(frozen=True, slots=True)
class _TrainingSample:
    features: QuantFeatureVector
    realized_bps: Decimal
    realized_bucket_id: str


def train_quant_forecast_artifact(
    *,
    dataset: HistoricalDataset,
    contract: ForecastContract,
    reference_instrument: InstrumentId,
    capital_instruments: tuple[InstrumentId, ...],
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
        raise ValueError("Quant artifact v7 只支持 4h ForecastContract")
    capital_keys = tuple(item.key for item in capital_instruments)
    if tuple(sorted(set(capital_keys))) != capital_keys:
        raise ValueError("Quant 历史资本诊断产品必须唯一且排序")
    if not capital_instruments:
        raise ValueError("Quant 历史资本诊断必须绑定正式 Product 表达")

    raw_samples = _training_samples(dataset, cutoff=cutoff)
    if len(raw_samples) < 1_000:
        raise ValueError("Quant baseline 至少需要 1,000 个逐小时可结算样本")
    development_end = len(raw_samples) * 3 // 5
    validation_end = len(raw_samples) * 4 // 5
    raw_development = _purge_before(
        raw_samples[:development_end],
        next_period_start=raw_samples[development_end].features.observed_at,
    )
    raw_validation = _purge_before(
        raw_samples[development_end:validation_end],
        next_period_start=raw_samples[validation_end].features.observed_at,
    )
    raw_blind = raw_samples[validation_end:]
    _validate_development_bucket_contract(contract, raw_development)
    development = _label_samples(raw_development, contract)
    validation = _label_samples(raw_validation, contract)
    blind = _label_samples(raw_blind, contract)
    if min(len(development), len(validation), len(blind)) < 100:
        raise ValueError("Quant chronological split 样本不足")

    thresholds = _feature_thresholds(development)
    candidate_evaluations = tuple(
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
    validation_phases = _non_overlapping_phases(validation)
    blind_phases = _non_overlapping_phases(blind)
    validation_unconditional_phase_ranked_probability_scores = (
        _phase_ranked_scores_for_distribution(
            validation_phases,
            _global_distribution(development, contract=contract).probabilities,
        )
    )
    validation_unconditional_ranked_probability_score = _mean(
        validation_unconditional_phase_ranked_probability_scores
    )
    validation_unconditional_phase_briers = _phase_brier_scores_for_distribution(
        validation_phases,
        _global_distribution(development, contract=contract).probabilities,
    )
    validation_unconditional_brier = _mean(validation_unconditional_phase_briers)
    eligible = tuple(
        candidate
        for candidate in candidate_evaluations
        if all(
            model < baseline
            for model, baseline in zip(
                candidate.validation_phase_ranked_probability_scores,
                validation_unconditional_phase_ranked_probability_scores,
                strict=True,
            )
        )
    )
    if not eligible:
        raise ValueError("Quant 候选未在全部非重叠验证相位改善有序分布")
    strict_best = min(
        eligible,
        key=lambda item: (
            item.validation_worst_phase_ranked_probability_score,
            _CANDIDATE_MODELS.index(item.model_name),
        ),
    )
    selection_standard_error = quant_phase_score_standard_error(
        strict_best.validation_phase_ranked_probability_scores
    )
    selection_limit = (
        strict_best.validation_worst_phase_ranked_probability_score
        + selection_standard_error
    )
    selected = next(
        candidate
        for candidate in eligible
        if candidate.validation_worst_phase_ranked_probability_score <= selection_limit
    )
    development_and_validation = (*development, *validation)
    selected_training_distributions = _fit_distributions(
        selected.model_name,
        development_and_validation,
        thresholds=thresholds,
        contract=contract,
        smoothing_strength=smoothing_strength,
    )
    selected_blind_phase_ranked_probability_scores = _phase_ranked_scores(
        blind_phases,
        model_name=selected.model_name,
        thresholds=thresholds,
        distributions=selected_training_distributions,
    )
    selected_blind_ranked_probability_score = _mean(
        selected_blind_phase_ranked_probability_scores
    )
    selected_blind_phase_briers = _phase_brier_scores(
        blind_phases,
        model_name=selected.model_name,
        thresholds=thresholds,
        distributions=selected_training_distributions,
    )
    selected_blind_brier = _mean(selected_blind_phase_briers)
    (
        selected_blind_phase_mean_absolute_return_errors_bps,
        selected_blind_phase_return_correlations,
    ) = _phase_return_diagnostics(
        blind_phases,
        model_name=selected.model_name,
        thresholds=thresholds,
        distributions=selected_training_distributions,
        contract=contract,
    )
    blind_unconditional_phase_ranked_probability_scores = (
        _phase_ranked_scores_for_distribution(
            blind_phases,
            _global_distribution(
                development_and_validation,
                contract=contract,
            ).probabilities,
        )
    )
    blind_unconditional_ranked_probability_score = _mean(
        blind_unconditional_phase_ranked_probability_scores
    )
    blind_unconditional_phase_briers = _phase_brier_scores_for_distribution(
        blind_phases,
        _global_distribution(
            development_and_validation,
            contract=contract,
        ).probabilities,
    )
    blind_unconditional_brier = _mean(blind_unconditional_phase_briers)
    blind_distribution = _global_distribution(
        development_and_validation,
        contract=contract,
    ).probabilities
    (
        blind_unconditional_phase_mean_absolute_return_errors_bps,
        blind_unconditional_phase_return_correlations,
    ) = _phase_return_diagnostics_for_distribution(
        blind_phases,
        blind_distribution,
        contract=contract,
    )
    all_samples = (*development, *validation, *blind)
    candidates = []
    for candidate in candidate_evaluations:
        distributions = _fit_distributions(
            candidate.model_name,
            all_samples,
            thresholds=thresholds,
            contract=contract,
            smoothing_strength=smoothing_strength,
        )
        cells = tuple(distributions[key] for key in sorted(distributions) if key != "GLOBAL")
        candidates.append(
            candidate.model_copy(
                update={"cell_count": len(cells), "cells": cells},
            )
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
        "candidate_evaluations": tuple(candidates),
        "selected_model": selected.model_name,
        "selection_standard_error": selection_standard_error,
        "smoothing_strength": smoothing_strength,
        "global_distribution": _global_distribution(all_samples, contract=contract),
        "development_sample_count": len(development),
        "validation_sample_count": len(validation),
        "blind_sample_count": len(blind),
        "validation_phase_sample_counts": tuple(len(item) for item in validation_phases),
        "blind_phase_sample_counts": tuple(len(item) for item in blind_phases),
        "validation_unconditional_ranked_probability_score": (
            validation_unconditional_ranked_probability_score
        ),
        "validation_unconditional_phase_ranked_probability_scores": (
            validation_unconditional_phase_ranked_probability_scores
        ),
        "selected_blind_ranked_probability_score": (
            selected_blind_ranked_probability_score
        ),
        "selected_blind_phase_ranked_probability_scores": (
            selected_blind_phase_ranked_probability_scores
        ),
        "blind_unconditional_ranked_probability_score": (
            blind_unconditional_ranked_probability_score
        ),
        "blind_unconditional_phase_ranked_probability_scores": (
            blind_unconditional_phase_ranked_probability_scores
        ),
        "validation_unconditional_brier": validation_unconditional_brier,
        "validation_unconditional_phase_briers": validation_unconditional_phase_briers,
        "selected_blind_brier": selected_blind_brier,
        "selected_blind_phase_briers": selected_blind_phase_briers,
        "blind_unconditional_brier": blind_unconditional_brier,
        "blind_unconditional_phase_briers": blind_unconditional_phase_briers,
        "selected_blind_mean_absolute_return_error_bps": _mean(
            selected_blind_phase_mean_absolute_return_errors_bps
        ),
        "selected_blind_phase_mean_absolute_return_errors_bps": (
            selected_blind_phase_mean_absolute_return_errors_bps
        ),
        "blind_unconditional_mean_absolute_return_error_bps": _mean(
            blind_unconditional_phase_mean_absolute_return_errors_bps
        ),
        "blind_unconditional_phase_mean_absolute_return_errors_bps": (
            blind_unconditional_phase_mean_absolute_return_errors_bps
        ),
        "selected_blind_return_correlation": _mean(
            selected_blind_phase_return_correlations
        ),
        "selected_blind_phase_return_correlations": (
            selected_blind_phase_return_correlations
        ),
        "blind_unconditional_return_correlation": _mean(
            blind_unconditional_phase_return_correlations
        ),
        "blind_unconditional_phase_return_correlations": (
            blind_unconditional_phase_return_correlations
        ),
        "historical_capital_feasibility": _unavailable_historical_capital_feasibility(
            dataset=dataset,
            capital_instruments=capital_instruments,
        ),
    }
    provisional = QuantForecastArtifact.model_construct(artifact_id="pending", **values)
    artifact_id = stable_id(
        "quant_forecast_artifact",
        provisional.model_dump(mode="json", exclude={"artifact_id"}),
    )
    return QuantForecastArtifact(artifact_id=artifact_id, **values)


def _unavailable_historical_capital_feasibility(
    *,
    dataset: HistoricalDataset,
    capital_instruments: tuple[InstrumentId, ...],
) -> QuantHistoricalCapitalFeasibility:
    """Refuse to turn trade-price bars or current rules into historical execution facts."""

    missing = {
        "EXECUTABLE_BID_ASK_DEPTH_HISTORY",
        "TIME_VERSIONED_EXECUTION_RULES",
    }
    products = {item.product for item in capital_instruments}
    if products & {
        InstrumentProduct.USD_M_PERPETUAL,
        InstrumentProduct.TRADFI_PERPETUAL,
    }:
        missing.add("VERIFIED_FUNDING_SETTLEMENT_HISTORY")
    if InstrumentProduct.TRADFI_PERPETUAL in products:
        missing.add("POINT_IN_TIME_TRADING_SESSION_HISTORY")
    return QuantHistoricalCapitalFeasibility(
        capital_instrument_keys=tuple(item.key for item in capital_instruments),
        checked_dataset_ids=(dataset.manifest.dataset_id,),
        missing_fact_types=tuple(sorted(missing)),
    )


def _training_samples(
    dataset: HistoricalDataset,
    *,
    cutoff: datetime,
) -> tuple[_RawTrainingSample, ...]:
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
        results.append(_RawTrainingSample(features=features, realized_bps=realized_bps))
    return tuple(results)


def _validate_development_bucket_contract(
    contract: ForecastContract,
    development: tuple[_RawTrainingSample, ...],
) -> None:
    """Prevent validation or blind outcomes from leaking through target encoding."""

    if tuple(item.bucket_id for item in contract.outcome_buckets) != _OUTCOME_BUCKET_IDS:
        raise ValueError("Quant v6 合同必须使用唯一九档有序收益编码")
    realized = tuple(item.realized_bps for item in development)
    expected_boundaries = tuple(
        _whole_bps_quantile(realized, probability)
        for probability in _OUTCOME_BOUNDARY_QUANTILES
    )
    actual_boundaries = tuple(
        bucket.upper_bps for bucket in contract.outcome_buckets[:-1]
    )
    if actual_boundaries != expected_boundaries:
        raise ValueError("Quant v6 bucket 边界必须只由 development 分布冻结")
    expected_representatives = _development_bucket_means(
        realized,
        expected_boundaries,
    )
    if tuple(item.representative_bps for item in contract.outcome_buckets) != (
        expected_representatives
    ):
        raise ValueError("Quant v6 bucket 代表值必须只由 development 分布冻结")
    if tuple(item.probability for item in contract.forecast_benchmark) != (
        _OUTCOME_BENCHMARK_PROBABILITIES
    ):
        raise ValueError("Quant v6 基准概率与冻结开发分位不一致")


def _label_samples(
    samples: tuple[_RawTrainingSample, ...],
    contract: ForecastContract,
) -> tuple[_TrainingSample, ...]:
    labeled = []
    for sample in samples:
        bucket_id = next(
            (
                bucket.bucket_id
                for bucket in contract.outcome_buckets
                if (bucket.lower_bps is None or sample.realized_bps >= bucket.lower_bps)
                and (bucket.upper_bps is None or sample.realized_bps < bucket.upper_bps)
            ),
            None,
        )
        if bucket_id is None:
            raise ValueError("Quant 训练收益未落入 ForecastContract bucket")
        labeled.append(
            _TrainingSample(
                features=sample.features,
                realized_bps=sample.realized_bps,
                realized_bucket_id=bucket_id,
            )
        )
    return tuple(labeled)


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
    phases = _non_overlapping_phases(evaluation)
    phase_ranked_probability_scores = _phase_ranked_scores(
        phases,
        model_name=model_name,
        thresholds=thresholds,
        distributions=distributions,
    )
    phase_briers = _phase_brier_scores(
        phases,
        model_name=model_name,
        thresholds=thresholds,
        distributions=distributions,
    )
    phase_return_errors, phase_return_correlations = _phase_return_diagnostics(
        phases,
        model_name=model_name,
        thresholds=thresholds,
        distributions=distributions,
        contract=contract,
    )
    return QuantCandidateEvaluation(
        model_name=model_name,
        cell_count=len(distributions) - 1,
        validation_ranked_probability_score=_mean(phase_ranked_probability_scores),
        validation_worst_phase_ranked_probability_score=max(
            phase_ranked_probability_scores
        ),
        validation_phase_ranked_probability_scores=phase_ranked_probability_scores,
        validation_brier=_mean(phase_briers),
        validation_worst_phase_brier=max(phase_briers),
        validation_phase_briers=phase_briers,
        validation_mean_absolute_return_error_bps=_mean(phase_return_errors),
        validation_phase_mean_absolute_return_errors_bps=phase_return_errors,
        validation_return_correlation=_mean(phase_return_correlations),
        validation_phase_return_correlations=phase_return_correlations,
        cells=tuple(distributions[key] for key in sorted(distributions) if key != "GLOBAL"),
    )


def _purge_before(
    samples: tuple[_RawTrainingSample, ...],
    *,
    next_period_start: datetime,
) -> tuple[_RawTrainingSample, ...]:
    """Exclude labels whose economic horizon reaches into the next split."""

    return tuple(
        item
        for item in samples
        if item.features.observed_at + _OUTCOME_HORIZON <= next_period_start
    )


def _non_overlapping_phases(
    samples: tuple[_TrainingSample, ...],
) -> tuple[tuple[_TrainingSample, ...], ...]:
    phases = tuple(
        tuple(
            item
            for item in samples
            if int(item.features.observed_at.timestamp() // 3600)
            % _NON_OVERLAPPING_PHASES
            == phase
        )
        for phase in range(_NON_OVERLAPPING_PHASES)
    )
    if any(not phase for phase in phases):
        raise ValueError("Quant 非重叠相位样本不足")
    return phases


def _phase_ranked_scores(
    phases: tuple[tuple[_TrainingSample, ...], ...],
    *,
    model_name: str,
    thresholds: QuantFeatureThresholds,
    distributions: dict[str, QuantCellDistribution],
) -> tuple[Decimal, ...]:
    values = tuple(
        _score(
            phase,
            model_name=model_name,
            thresholds=thresholds,
            distributions=distributions,
            score=ordinal_ranked_probability_score,
        )
        for phase in phases
    )
    return values


def _phase_brier_scores(
    phases: tuple[tuple[_TrainingSample, ...], ...],
    *,
    model_name: str,
    thresholds: QuantFeatureThresholds,
    distributions: dict[str, QuantCellDistribution],
) -> tuple[Decimal, ...]:
    return tuple(
        _score(
            phase,
            model_name=model_name,
            thresholds=thresholds,
            distributions=distributions,
            score=multiclass_brier_score,
        )
        for phase in phases
    )


def _phase_ranked_scores_for_distribution(
    phases: tuple[tuple[_TrainingSample, ...], ...],
    probabilities: tuple[ForecastBucketProbability, ...],
) -> tuple[Decimal, ...]:
    return tuple(
        _score_distribution(
            phase,
            probabilities,
            score=ordinal_ranked_probability_score,
        )
        for phase in phases
    )


def _phase_brier_scores_for_distribution(
    phases: tuple[tuple[_TrainingSample, ...], ...],
    probabilities: tuple[ForecastBucketProbability, ...],
) -> tuple[Decimal, ...]:
    return tuple(
        _score_distribution(phase, probabilities, score=multiclass_brier_score)
        for phase in phases
    )


def _phase_return_diagnostics(
    phases: tuple[tuple[_TrainingSample, ...], ...],
    *,
    model_name: str,
    thresholds: QuantFeatureThresholds,
    distributions: dict[str, QuantCellDistribution],
    contract: ForecastContract,
) -> tuple[tuple[Decimal, ...], tuple[Decimal, ...]]:
    expected_by_bucket = {
        item.bucket_id: item.representative_bps for item in contract.outcome_buckets
    }
    pairs = tuple(
        tuple(
            (
                _expected_return_bps(
                    distributions.get(
                        quant_cell_key(model_name, sample.features, thresholds),
                        distributions["GLOBAL"],
                    ).probabilities,
                    expected_by_bucket,
                ),
                sample.realized_bps,
            )
            for sample in phase
        )
        for phase in phases
    )
    return (
        tuple(_mean_absolute_error(values) for values in pairs),
        tuple(_pearson_correlation(values) for values in pairs),
    )


def _phase_return_diagnostics_for_distribution(
    phases: tuple[tuple[_TrainingSample, ...], ...],
    probabilities: tuple[ForecastBucketProbability, ...],
    *,
    contract: ForecastContract,
) -> tuple[tuple[Decimal, ...], tuple[Decimal, ...]]:
    expected = _expected_return_bps(
        probabilities,
        {item.bucket_id: item.representative_bps for item in contract.outcome_buckets},
    )
    pairs = tuple(
        tuple((expected, sample.realized_bps) for sample in phase) for phase in phases
    )
    return (
        tuple(_mean_absolute_error(values) for values in pairs),
        tuple(_pearson_correlation(values) for values in pairs),
    )


def _expected_return_bps(
    probabilities: tuple[ForecastBucketProbability, ...],
    representatives: dict[str, Decimal],
) -> Decimal:
    return sum(
        (
            item.probability * representatives[item.bucket_id]
            for item in probabilities
        ),
        Decimal("0"),
    )


def _mean_absolute_error(values: tuple[tuple[Decimal, Decimal], ...]) -> Decimal:
    return sum((abs(expected - realized) for expected, realized in values), Decimal("0")) / Decimal(
        len(values)
    )


def _pearson_correlation(values: tuple[tuple[Decimal, Decimal], ...]) -> Decimal:
    expected_mean = sum((item[0] for item in values), Decimal("0")) / Decimal(len(values))
    realized_mean = sum((item[1] for item in values), Decimal("0")) / Decimal(len(values))
    expected_variance = sum(
        ((expected - expected_mean) ** 2 for expected, _ in values),
        Decimal("0"),
    )
    realized_variance = sum(
        ((realized - realized_mean) ** 2 for _, realized in values),
        Decimal("0"),
    )
    if expected_variance == 0 or realized_variance == 0:
        return Decimal("0")
    covariance = sum(
        (
            (expected - expected_mean) * (realized - realized_mean)
            for expected, realized in values
        ),
        Decimal("0"),
    )
    correlation = covariance / (expected_variance * realized_variance).sqrt()
    return max(Decimal("-1"), min(Decimal("1"), correlation))


def _mean(values: tuple[Decimal, ...]) -> Decimal:
    return sum(values, Decimal("0")) / Decimal(len(values))


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
                (Decimal(cell_counts[bucket.bucket_id]) + smoothing_strength * prior.probability)
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
    score: Callable[[tuple[tuple[str, Decimal], ...], str], Decimal],
) -> Decimal:
    return sum(
        (
            score(
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
    *,
    score: Callable[[tuple[tuple[str, Decimal], ...], str], Decimal],
) -> Decimal:
    values = tuple((item.bucket_id, item.probability) for item in probabilities)
    return sum(
        (score(values, sample.realized_bucket_id) for sample in samples),
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
        normalized.append(ForecastBucketProbability(bucket_id=bucket_id, probability=probability))
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


def _whole_bps_quantile(values: tuple[Decimal, ...], probability: Decimal) -> Decimal:
    return _quantile(values, probability).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def _development_bucket_means(
    values: tuple[Decimal, ...],
    boundaries: tuple[Decimal, ...],
) -> tuple[Decimal, ...]:
    buckets = tuple(
        tuple(
            value
            for value in values
            if (index == 0 or value >= boundaries[index - 1])
            and (index == len(boundaries) or value < boundaries[index])
        )
        for index in range(len(boundaries) + 1)
    )
    if any(not bucket for bucket in buckets):
        raise ValueError("Quant development bucket 不能为空")
    return tuple(
        (sum(bucket, Decimal("0")) / Decimal(len(bucket))).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
        for bucket in buckets
    )


__all__ = ["train_quant_forecast_artifact"]
