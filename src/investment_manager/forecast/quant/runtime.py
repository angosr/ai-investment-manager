"""Transparent, research-only Quant forecasts on the shared Forecast ledger."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from investment_manager.forecast.contract_repository import SqlForecastContractStore
from investment_manager.forecast.contracts import (
    ForecastContract,
    ForecastDecisionSlot,
    ForecastNoEstimate,
    ForecastNoEstimateReason,
    ForecastPriceAnchor,
    ForecastProducerBinding,
    ForecastProducerKind,
    ForecastSlotCause,
)
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.forecast.results import BaseForecast, ForecastBucketProbability
from investment_manager.kernel.errors import PointInTimeInputUnavailable
from investment_manager.kernel.identity import canonical_json, content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.market.models import (
    InstrumentId,
    InstrumentProduct,
    MarketBar,
    MarketSnapshot,
)
from investment_manager.market.repository import MarketDataStore

QUANT_FEATURE_VERSION = "hourly-5m-momentum-volatility-v1"
QUANT_INFERENCE_VERSION = "conditional-empirical-dirichlet-v1"
_FIVE_MINUTES = timedelta(minutes=5)
_FEATURE_BARS = 49


class QuantFeatureThresholds(FrozenModel):
    momentum_low_bps: Decimal
    momentum_high_bps: Decimal
    short_return_low_bps: Decimal
    short_return_high_bps: Decimal
    volatility_high_bps: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def bounds_are_ordered(self):
        if self.momentum_low_bps >= self.momentum_high_bps:
            raise ValueError("Quant momentum 阈值顺序非法")
        if self.short_return_low_bps >= self.short_return_high_bps:
            raise ValueError("Quant short-return 阈值顺序非法")
        return self


class QuantFeatureVector(FrozenModel):
    feature_version: Literal["hourly-5m-momentum-volatility-v1"] = QUANT_FEATURE_VERSION
    observed_at: datetime
    return_60m_bps: Decimal
    return_240m_bps: Decimal
    volatility_240m_bps: Decimal = Field(ge=0)

    _utc_observed_at = field_validator("observed_at")(require_utc)


class QuantCellDistribution(FrozenModel):
    cell_key: str = Field(min_length=1)
    sample_count: int = Field(ge=0)
    probabilities: tuple[ForecastBucketProbability, ...] = Field(min_length=3)

    @model_validator(mode="after")
    def probability_distribution_is_valid(self):
        ids = tuple(item.bucket_id for item in self.probabilities)
        if len(ids) != len(set(ids)):
            raise ValueError("Quant cell bucket 不能重复")
        if sum((item.probability for item in self.probabilities), Decimal("0")) != 1:
            raise ValueError("Quant cell 概率之和必须为 1")
        return self


class QuantCandidateEvaluation(FrozenModel):
    model_name: str = Field(min_length=1)
    cell_count: int = Field(gt=0)
    validation_brier: Decimal = Field(ge=0)
    validation_worst_phase_brier: Decimal = Field(ge=0)
    validation_phase_briers: tuple[Decimal, ...]
    cells: tuple[QuantCellDistribution, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validation_summary_matches_phases(self):
        if len(self.validation_phase_briers) != 4:
            raise ValueError("Quant validation 必须保存四个非重叠相位")
        if self.validation_brier != sum(
            self.validation_phase_briers, Decimal("0")
        ) / Decimal(len(self.validation_phase_briers)):
            raise ValueError("Quant validation Brier 必须等于非重叠相位均值")
        if self.validation_worst_phase_brier != max(self.validation_phase_briers):
            raise ValueError("Quant validation 最差相位 Brier 与相位明细不一致")
        return self


class QuantForecastArtifact(FrozenModel):
    """Content-addressed training result; runtime inference performs no fitting."""

    schema_version: Literal["quant-forecast-artifact-v3"] = "quant-forecast-artifact-v3"
    artifact_id: str = Field(min_length=1)
    training_method_version: Literal["purged-non-overlap-cell-panel-selection-v3"] = (
        "purged-non-overlap-cell-panel-selection-v3"
    )
    inference_version: Literal["conditional-empirical-dirichlet-v1"] = QUANT_INFERENCE_VERSION
    contract_id: str = Field(min_length=1)
    outcome_family_id: str = Field(min_length=1)
    reference_instrument_key: str = Field(min_length=1)
    dataset_id: str = Field(min_length=1)
    bars_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    interval: Literal["5m"] = "5m"
    training_cutoff_at: datetime
    dataset_last_close_at: datetime
    feature_thresholds: QuantFeatureThresholds
    candidate_evaluations: tuple[QuantCandidateEvaluation, ...] = Field(min_length=1)
    selected_model: str = Field(min_length=1)
    smoothing_strength: Decimal = Field(gt=0)
    global_distribution: QuantCellDistribution
    development_sample_count: int = Field(gt=0)
    validation_sample_count: int = Field(gt=0)
    blind_sample_count: int = Field(gt=0)
    validation_phase_sample_counts: tuple[int, ...]
    blind_phase_sample_counts: tuple[int, ...]
    validation_unconditional_brier: Decimal = Field(ge=0)
    validation_unconditional_phase_briers: tuple[Decimal, ...]
    selected_blind_brier: Decimal = Field(ge=0)
    selected_blind_phase_briers: tuple[Decimal, ...]
    blind_unconditional_brier: Decimal = Field(ge=0)
    blind_unconditional_phase_briers: tuple[Decimal, ...]

    _utc_training_cutoff_at = field_validator("training_cutoff_at")(require_utc)
    _utc_dataset_last_close_at = field_validator("dataset_last_close_at")(require_utc)

    @model_validator(mode="after")
    def identity_and_model_are_canonical(self):
        if self.dataset_last_close_at > self.training_cutoff_at:
            raise ValueError("Quant 数据尾部不能晚于训练截止")
        names = tuple(item.model_name for item in self.candidate_evaluations)
        if len(set(names)) != len(names) or self.selected_model not in names:
            raise ValueError("Quant candidate/selected model 身份非法")
        selected = min(
            enumerate(self.candidate_evaluations),
            key=lambda indexed: (
                indexed[1].validation_worst_phase_brier,
                indexed[0],
            ),
        )[1]
        if selected.model_name != self.selected_model:
            raise ValueError("Quant selected model 未按最差非重叠相位与复杂度排序")
        expected_ids = tuple(item.bucket_id for item in self.global_distribution.probabilities)
        for candidate in self.candidate_evaluations:
            cell_keys = tuple(item.cell_key for item in candidate.cells)
            if cell_keys != tuple(sorted(set(cell_keys))) or candidate.cell_count != len(
                candidate.cells
            ):
                raise ValueError("Quant candidate cells 必须按唯一 key 排序且计数一致")
            if any(
                tuple(item.bucket_id for item in cell.probabilities) != expected_ids
                for cell in candidate.cells
            ):
                raise ValueError("Quant candidate bucket 必须与全局分布同序")
        for name in ("validation_phase_sample_counts", "blind_phase_sample_counts"):
            values = getattr(self, name)
            if len(values) != 4 or any(value <= 0 for value in values):
                raise ValueError(f"Quant {name} 必须全部为正数")
        if sum(self.validation_phase_sample_counts) != self.validation_sample_count:
            raise ValueError("Quant validation 相位样本数与总数不一致")
        if sum(self.blind_phase_sample_counts) != self.blind_sample_count:
            raise ValueError("Quant blind 相位样本数与总数不一致")
        for summary_name, phase_name in (
            ("validation_unconditional_brier", "validation_unconditional_phase_briers"),
            ("selected_blind_brier", "selected_blind_phase_briers"),
            ("blind_unconditional_brier", "blind_unconditional_phase_briers"),
        ):
            phases = getattr(self, phase_name)
            if len(phases) != 4:
                raise ValueError(f"Quant {phase_name} 必须保存四个非重叠相位")
            if getattr(self, summary_name) != sum(phases, Decimal("0")) / Decimal(len(phases)):
                raise ValueError(f"Quant {summary_name} 必须等于非重叠相位均值")
        expected = stable_id(
            "quant_forecast_artifact",
            self.model_dump(mode="json", exclude={"artifact_id"}),
        )
        if self.artifact_id != expected:
            raise ValueError("Quant artifact_id 与冻结内容不一致")
        return self

    def probabilities_for(
        self,
        features: QuantFeatureVector,
        *,
        model_name: str | None = None,
    ) -> tuple[ForecastBucketProbability, ...]:
        selected_name = model_name or self.selected_model
        candidate = next(
            (item for item in self.candidate_evaluations if item.model_name == selected_name),
            None,
        )
        if candidate is None:
            raise ValueError(f"Quant artifact 不包含模型：{selected_name}")
        key = quant_cell_key(
            selected_name,
            features,
            self.feature_thresholds,
        )
        cell = next((item for item in candidate.cells if item.cell_key == key), None)
        return self.global_distribution.probabilities if cell is None else cell.probabilities


def load_quant_forecast_artifact(
    path: str | Path,
    *,
    expected_artifact_id: str | None = None,
) -> QuantForecastArtifact:
    artifact = QuantForecastArtifact.model_validate_json(Path(path).read_text(encoding="utf-8"))
    if expected_artifact_id is not None and artifact.artifact_id != expected_artifact_id:
        raise ValueError("Quant 制品身份与配置不一致")
    return artifact


def quant_forecast_behavior_id(
    *,
    policy_version: str,
    producer_id: str,
    targets: tuple[tuple[ForecastContract, QuantForecastArtifact | None], ...],
) -> str:
    """Identify the complete deterministic panel, including explicit missing targets."""

    if not targets:
        raise ValueError("Quant behavior 至少需要一个 Forecast target")
    ordered = tuple(sorted(targets, key=lambda item: item[0].outcome_family_id))
    families = tuple(contract.outcome_family_id for contract, _ in ordered)
    if len(families) != len(set(families)):
        raise ValueError("Quant behavior target family 不能重复")
    return stable_id(
        "quant_forecast_behavior",
        policy_version,
        producer_id,
        QUANT_FEATURE_VERSION,
        QUANT_INFERENCE_VERSION,
        tuple(
            (
                contract.contract_id,
                None if artifact is None else artifact.artifact_id,
            )
            for contract, artifact in ordered
        ),
    )


def quant_panel_projection(
    artifact: QuantForecastArtifact,
    features: QuantFeatureVector,
    *,
    decision_slot_id: str,
) -> dict[str, object]:
    """Project exact candidate disagreement without exposing training rows."""

    candidate_values = tuple(
        (
            candidate,
            quant_cell_key(
                candidate.model_name,
                features,
                artifact.feature_thresholds,
            ),
            artifact.probabilities_for(features, model_name=candidate.model_name),
        )
        for candidate in artifact.candidate_evaluations
    )
    selected_probabilities = artifact.probabilities_for(features)
    probability_ranges = tuple(
        max(values[2][index].probability for values in candidate_values)
        - min(values[2][index].probability for values in candidate_values)
        for index in range(len(selected_probabilities))
    )
    return {
        "purpose": "PROGRAM_QUANT_FORECAST",
        "artifact_id": artifact.artifact_id,
        "inference_version": artifact.inference_version,
        "decision_slot_id": decision_slot_id,
        "features": features,
        "quant_prior": {
            "model_name": artifact.selected_model,
            "outcome_probabilities": selected_probabilities,
        },
        "candidate_predictions": tuple(
            {
                "model_name": candidate.model_name,
                "validation_brier": candidate.validation_brier,
                "cell_key": cell_key,
                "outcome_probabilities": probabilities,
            }
            for candidate, cell_key, probabilities in candidate_values
        ),
        "maximum_bucket_probability_range": max(probability_ranges),
    }


def quant_features(snapshot: MarketSnapshot) -> QuantFeatureVector:
    return quant_features_from_bars(snapshot.bars)


def quant_features_from_bars(bars: tuple[MarketBar, ...]) -> QuantFeatureVector:
    bars = bars[-_FEATURE_BARS:]
    if len(bars) != _FEATURE_BARS:
        raise PointInTimeInputUnavailable("Quant 4h 特征需要连续 49 根 5m K 线")
    if any(right.event_time - left.event_time != _FIVE_MINUTES for left, right in pairwise(bars)):
        raise PointInTimeInputUnavailable("Quant 5m K 线存在时间缺口")
    closes = tuple(item.close for item in bars)
    returns = tuple(right / left - Decimal("1") for left, right in pairwise(closes))
    mean = sum(returns, Decimal("0")) / Decimal(len(returns))
    variance = sum(((item - mean) ** 2 for item in returns), Decimal("0")) / Decimal(len(returns))
    return QuantFeatureVector(
        observed_at=max(item.observed_at for item in bars),
        return_60m_bps=(closes[-1] / closes[-13] - Decimal("1")) * Decimal("10000"),
        return_240m_bps=(closes[-1] / closes[0] - Decimal("1")) * Decimal("10000"),
        volatility_240m_bps=variance.sqrt() * Decimal("10000"),
    )


def quant_cell_key(
    model_name: str,
    features: QuantFeatureVector,
    thresholds: QuantFeatureThresholds,
) -> str:
    momentum = _tercile(
        features.return_240m_bps,
        thresholds.momentum_low_bps,
        thresholds.momentum_high_bps,
    )
    if model_name == "momentum":
        return f"momentum={momentum}"
    volatility = "HIGH" if features.volatility_240m_bps >= thresholds.volatility_high_bps else "LOW"
    if model_name == "momentum_volatility":
        return f"momentum={momentum}|volatility={volatility}"
    if model_name == "momentum_reversal_volatility":
        short_return = _tercile(
            features.return_60m_bps,
            thresholds.short_return_low_bps,
            thresholds.short_return_high_bps,
        )
        return f"momentum={momentum}|short_return={short_return}|volatility={volatility}"
    raise ValueError(f"未知 Quant model: {model_name}")


def _tercile(value: Decimal, low: Decimal, high: Decimal) -> str:
    return "LOW" if value < low else "HIGH" if value >= high else "MID"


QuantProductionResult = BaseForecast | ForecastNoEstimate


@dataclass(frozen=True, slots=True)
class QuantForecastRuntimeTarget:
    contract: ForecastContract
    binding: ForecastProducerBinding
    instrument: InstrumentId
    artifact: QuantForecastArtifact | None

    def __post_init__(self) -> None:
        if self.binding.producer_kind != ForecastProducerKind.PROGRAM:
            raise ValueError("Quant Forecast 必须绑定 PROGRAM Producer")
        if self.binding.contract_id != self.contract.contract_id:
            raise ValueError("Quant Forecast Binding 与 Contract 不一致")
        if self.artifact is not None and any(
            (
                self.instrument.product != InstrumentProduct.SPOT,
                self.artifact.contract_id != self.contract.contract_id,
                self.artifact.outcome_family_id != self.contract.outcome_family_id,
                self.artifact.reference_instrument_key != self.instrument.key,
            )
        ):
            raise ValueError("Quant Forecast artifact 与运行目标不一致")


@dataclass(frozen=True, slots=True)
class PortfolioQuantForecastProducer:
    """Write research Forecasts for the exact slots used by the capital producer."""

    targets: tuple[QuantForecastRuntimeTarget, ...]
    market: MarketDataStore
    contracts: SqlForecastContractStore
    forecasts: SqlForecastStore
    interval: str
    bar_window: int
    maximum_quote_age_seconds: int
    activated_at: datetime
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def __post_init__(self) -> None:
        require_utc(self.activated_at)
        if self.interval != "5m" or self.bar_window < _FEATURE_BARS:
            raise ValueError("Quant baseline 需要至少 49 根 5m K 线")
        families = tuple(item.contract.outcome_family_id for item in self.targets)
        if not self.targets or families != tuple(sorted(set(families))):
            raise ValueError("Quant Forecast targets 必须按唯一 family 排序")

    def produce_all(
        self,
        *,
        as_of: datetime,
        cause: ForecastSlotCause | None = None,
    ) -> tuple[QuantProductionResult, ...]:
        slot_at = require_utc(as_of)
        if slot_at < self.activated_at:
            return ()
        return tuple(self._produce(target, as_of=slot_at, cause=cause) for target in self.targets)

    def _produce(
        self,
        target: QuantForecastRuntimeTarget,
        *,
        as_of: datetime,
        cause: ForecastSlotCause | None,
    ) -> QuantProductionResult:
        self.contracts.record_contract(target.contract)
        self.contracts.resolve_binding(target.binding, activated_at=self.activated_at)
        slot_cause = cause or ForecastSlotCause.cadence(target.contract)
        slot_id = ForecastDecisionSlot.identity_for(
            target.contract.contract_id,
            as_of,
            cause=slot_cause,
        )
        existing = self.forecasts.result_for_behavior(
            decision_slot_id=slot_id,
            producer_behavior_id=target.binding.producer_behavior_id,
        )
        if existing is not None:
            return existing
        absence = self.contracts.no_estimate(
            stable_id("forecast_no_estimate", slot_id, target.binding.producer_behavior_id)
        )
        if absence is not None:
            return absence
        cutoff_quote = self._quote(target.instrument, at=as_of)
        slot = self._slot(
            target,
            as_of=as_of,
            cause=slot_cause,
            quote=cutoff_quote,
        )
        completed_at = max(require_utc(self.clock()), as_of)
        if completed_at > slot.completion_deadline_at:
            return self._no_estimate(
                target,
                slot=slot,
                reason=ForecastNoEstimateReason.DEADLINE_MISSED,
                completed_at=completed_at,
                detail="QUANT_FORECAST_COMPLETION_DEADLINE_EXCEEDED",
            )
        if target.artifact is None:
            return self._no_estimate(
                target,
                slot=slot,
                reason=ForecastNoEstimateReason.REQUIRED_FEATURE_MISSING,
                completed_at=completed_at,
                detail="QUANT_ARTIFACT_UNAVAILABLE",
            )
        try:
            snapshot = self.market.snapshot(
                cycle_id=stable_id("quant_forecast_state", slot.slot_id),
                symbol=target.instrument.symbol,
                interval=self.interval,
                as_of=as_of,
                bar_window=self.bar_window,
                source="point-in-time-market-ledger",
            )
            features = quant_features(snapshot)
        except (PointInTimeInputUnavailable, ValueError):
            return self._no_estimate(
                target,
                slot=slot,
                reason=ForecastNoEstimateReason.REQUIRED_FEATURE_MISSING,
                completed_at=completed_at,
                detail="QUANT_POINT_IN_TIME_FEATURES_UNAVAILABLE",
            )
        if cutoff_quote is None or self._quote_age(target.instrument, cutoff_quote, as_of) > (
            self.maximum_quote_age_seconds
        ):
            return self._no_estimate(
                target,
                slot=slot,
                reason=ForecastNoEstimateReason.MARKET_INPUT_INVALID,
                completed_at=completed_at,
                detail="QUANT_CUTOFF_QUOTE_MISSING_OR_STALE",
            )
        entry_quote = self._quote(target.instrument, at=completed_at)
        if (
            entry_quote is None
            or self._quote_age(
                target.instrument,
                entry_quote,
                completed_at,
            )
            > self.maximum_quote_age_seconds
        ):
            return self._no_estimate(
                target,
                slot=slot,
                reason=ForecastNoEstimateReason.STALE_BEFORE_AVAILABLE,
                completed_at=completed_at,
                detail="QUANT_ENTRY_QUOTE_MISSING_OR_STALE",
            )
        probabilities = target.artifact.probabilities_for(features)
        panel = quant_panel_projection(
            target.artifact,
            features,
            decision_slot_id=slot.slot_id,
        )
        forecast = BaseForecast(
            forecast_id=stable_id(
                "base_forecast",
                slot.slot_id,
                target.binding.producer_behavior_id,
            ),
            contract_id=target.contract.contract_id,
            decision_slot_id=slot.slot_id,
            producer_id=target.binding.producer_id,
            producer_behavior_id=target.binding.producer_behavior_id,
            outcome_family_id=target.contract.outcome_family_id,
            target=target.contract.target,
            horizon_minutes=target.contract.horizon_minutes,
            cutoff_prices=slot.cutoff_prices,
            entry_prices=self._anchors(
                target.instrument,
                quote=entry_quote,
                available_at=completed_at,
            ),
            information_cutoff_at=slot.information_cutoff_at,
            input_observed_at=features.observed_at,
            available_at=completed_at,
            valid_until=min(
                slot.evaluation_at,
                completed_at + timedelta(minutes=target.contract.validity_minutes),
            ),
            outcome_probabilities=probabilities,
            expected_gross_bps=sum(
                (
                    probability.probability * bucket.representative_bps
                    for probability, bucket in zip(
                        probabilities,
                        target.contract.outcome_buckets,
                        strict=True,
                    )
                ),
                Decimal("0"),
            ),
            input_refs=tuple(
                sorted(
                    {
                        target.artifact.artifact_id,
                        content_hash(snapshot),
                        *(item.quote_ref for item in slot.cutoff_prices),
                    }
                )
            ),
            program_input_json=canonical_json(panel),
            program_input_hash=content_hash(panel),
        )
        self.forecasts.record(forecast)
        return forecast

    def _slot(
        self,
        target: QuantForecastRuntimeTarget,
        *,
        as_of: datetime,
        cause: ForecastSlotCause,
        quote,
    ) -> ForecastDecisionSlot:
        anchors = self._anchors(target.instrument, quote=quote, available_at=as_of)
        slot_id = ForecastDecisionSlot.identity_for(
            target.contract.contract_id,
            as_of,
            cause=cause,
        )
        slot = self.contracts.slot(slot_id)
        if slot is None:
            slot = ForecastDecisionSlot.create(
                target.contract,
                slot_as_of=as_of,
                information_cutoff_at=as_of,
                cutoff_prices=anchors,
                cause=cause,
            )
            self.contracts.record_slot(slot, binding=target.binding)
        elif slot.information_cutoff_at != as_of or slot.cutoff_prices != anchors:
            raise ValueError("Quant decision slot 已绑定不同点时输入")
        else:
            self.contracts.record_obligation(slot=slot, binding=target.binding)
        return slot

    def _no_estimate(
        self,
        target: QuantForecastRuntimeTarget,
        *,
        slot: ForecastDecisionSlot,
        reason: ForecastNoEstimateReason,
        completed_at: datetime,
        detail: str,
    ) -> ForecastNoEstimate:
        result = ForecastNoEstimate(
            result_id=stable_id(
                "forecast_no_estimate",
                slot.slot_id,
                target.binding.producer_behavior_id,
            ),
            slot_id=slot.slot_id,
            contract_id=target.contract.contract_id,
            producer_kind=target.binding.producer_kind,
            producer_id=target.binding.producer_id,
            producer_behavior_id=target.binding.producer_behavior_id,
            reason=reason,
            information_cutoff_at=slot.information_cutoff_at,
            attempted_at=slot.slot_as_of,
            completed_at=max(require_utc(completed_at), slot.slot_as_of),
            input_refs=tuple(item.quote_ref for item in slot.cutoff_prices),
            detail=detail,
        )
        self.contracts.record_no_estimate(result)
        return result

    def _quote(self, instrument: InstrumentId, *, at: datetime):
        if instrument.product == InstrumentProduct.SPOT:
            return self.market.latest_spot_quote(
                instrument=instrument,
                evaluation_at=at,
                visible_at=at,
            )
        return self.market.latest_perpetual_quote(
            instrument=instrument,
            evaluation_at=at,
            visible_at=at,
        )

    @staticmethod
    def _anchors(
        instrument: InstrumentId,
        *,
        quote,
        available_at: datetime,
    ) -> tuple[ForecastPriceAnchor, ...]:
        if quote is None:
            return ()
        return (
            ForecastPriceAnchor(
                instrument_id=instrument.key,
                price=quote.ask,
                observed_at=(
                    quote.observed_at
                    if instrument.product == InstrumentProduct.SPOT
                    else quote.exchange_time
                ),
                available_at=available_at,
                quote_ref=quote.quote_id,
            ),
        )

    @staticmethod
    def _quote_age(instrument: InstrumentId, quote, at: datetime) -> float:
        observed_at = (
            quote.observed_at
            if instrument.product == InstrumentProduct.SPOT
            else quote.exchange_time
        )
        return max(0.0, (at - observed_at).total_seconds())


__all__ = [
    "PortfolioQuantForecastProducer",
    "QuantCandidateEvaluation",
    "QuantCellDistribution",
    "QuantFeatureThresholds",
    "QuantFeatureVector",
    "QuantForecastArtifact",
    "QuantForecastRuntimeTarget",
    "load_quant_forecast_artifact",
    "quant_cell_key",
    "quant_features",
    "quant_features_from_bars",
    "quant_forecast_behavior_id",
]
