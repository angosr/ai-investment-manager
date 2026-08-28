from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field

from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.research.carry import HistoricalCarryDataset
from investment_manager.research.dataset import HistoricalDataset

_BPS = Decimal("10000")
_HORIZON_BARS = 4
_PHASES = 4


class BasisMappingEvaluationStatus(StrEnum):
    REJECTED = "REJECTED"
    FORWARD_RESEARCH = "FORWARD_RESEARCH"


class BasisMappingMetrics(FrozenModel):
    sample_count: int = Field(gt=0)
    baseline_exit_basis_mae_bps: Decimal = Field(ge=0)
    candidate_exit_basis_mae_bps: Decimal = Field(ge=0)
    candidate_exit_basis_mae_improvement_bps: Decimal
    baseline_mapping_residual_mae_bps: Decimal = Field(ge=0)
    candidate_mapping_residual_mae_bps: Decimal = Field(ge=0)
    baseline_mapping_residual_sign_accuracy: Decimal = Field(ge=0, le=1)
    candidate_mapping_residual_sign_accuracy: Decimal = Field(ge=0, le=1)


class BasisMappingPhaseMetrics(FrozenModel):
    phase: int = Field(ge=0, lt=_PHASES)
    metrics: BasisMappingMetrics


class BasisMappingSplitEvidence(FrozenModel):
    overall: BasisMappingMetrics
    phases: tuple[BasisMappingPhaseMetrics, ...] = Field(min_length=_PHASES)
    candidate_wins_every_phase: bool


class BasisMappingEvaluationArtifact(FrozenModel):
    schema_version: str = "basis-mapping-evaluation-v1"
    artifact_id: str
    plan_id: str
    plan_hash: str
    plan_registration_commit: str
    plan_registered_at: datetime
    evaluator_code_version: str
    evaluated_at: datetime
    spot_dataset_id: str
    carry_dataset_id: str
    status: BasisMappingEvaluationStatus
    validation: BasisMappingSplitEvidence
    blind_evaluated: bool
    blind: BasisMappingSplitEvidence | None = None
    capital_feasibility: str = "UNAVAILABLE"
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _BasisCase:
    sequence_index: int
    entry_basis_bps: Decimal
    exit_basis_bps: Decimal
    baseline_mapping_residual_bps: Decimal
    candidate_mapping_residual_bps: Decimal
    realized_mapping_residual_bps: Decimal


def load_basis_mapping_plan(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Basis 研究计划根节点必须是对象")
    expected = {
        "schema_version": "product-mapping-candidate-plan-v1",
        "plan_id": "paxg-perpetual-basis-4h-v1",
        "evaluation_family_id": "paxg-product-mapping-4h",
    }
    if any(raw.get(key) != value for key, value in expected.items()):
        raise ValueError("Basis 研究计划身份不受当前 evaluator 支持")
    data = _mapping(raw, "data")
    target = _mapping(raw, "target")
    comparison = _mapping(raw, "comparison")
    evaluation = _mapping(raw, "evaluation")
    if (
        data.get("symbol") != "PAXGUSDT"
        or data.get("interval") != "1h"
        or target.get("horizon_minutes") != 240
        or target.get("decision_cadence_minutes") != 60
        or _mapping(comparison, "baseline").get("name") != "basis_persistence"
        or _mapping(comparison, "candidate").get("name") != "full_basis_convergence"
        or comparison.get("fitted_parameters") != "none"
        or evaluation.get("chronological_split")
        != ["development_60pct", "validation_20pct", "blind_20pct"]
        or evaluation.get("primary_metric") != "exit_basis_mae_bps"
    ):
        raise ValueError("Basis 研究计划与冻结的无参数比较语义不一致")
    window = data.get("source_window")
    if not isinstance(window, list) or len(window) != 2:
        raise ValueError("Basis 研究计划缺少唯一数据窗口")
    _plan_time(window[0])
    _plan_time(window[1])
    return raw


def evaluate_basis_mapping(
    *,
    plan: dict[str, Any],
    plan_registration_commit: str,
    plan_registered_at: datetime,
    evaluator_code_version: str,
    spot: HistoricalDataset,
    carry: HistoricalCarryDataset,
) -> BasisMappingEvaluationArtifact:
    """Evaluate the preregistered zero-exit-basis claim without tuning it."""

    _validate_inputs(plan=plan, spot=spot, carry=carry)
    bar_count = len(spot.bars)
    development_end = int(bar_count * 0.6)
    validation_end = int(bar_count * 0.8)
    validation_cases = _cases(
        spot=spot,
        carry=carry,
        start=development_end,
        end=validation_end,
    )
    validation = _split_evidence(validation_cases)
    blind = None
    if validation.candidate_wins_every_phase:
        blind = _split_evidence(
            _cases(
                spot=spot,
                carry=carry,
                start=validation_end,
                end=bar_count,
            )
        )
    accepted = blind is not None and blind.candidate_wins_every_phase
    reasons = ["EXECUTABLE_SPREAD_DEPTH_AND_FUNDING_HISTORY_UNAVAILABLE"]
    if not validation.candidate_wins_every_phase:
        reasons.append("VALIDATION_EXIT_BASIS_MAE_NOT_BETTER_IN_EVERY_PHASE")
    elif not accepted:
        reasons.append("BLIND_EXIT_BASIS_MAE_NOT_BETTER_IN_EVERY_PHASE")
    else:
        reasons.append("HISTORICAL_MAPPING_ONLY_REQUIRES_FORWARD_RESEARCH")
    values = {
        "plan_id": str(plan["plan_id"]),
        "plan_hash": content_hash(plan),
        "plan_registration_commit": plan_registration_commit,
        "plan_registered_at": require_utc(plan_registered_at),
        "evaluator_code_version": evaluator_code_version,
        "evaluated_at": max(spot.manifest.collected_at, carry.manifest.collected_at),
        "spot_dataset_id": spot.manifest.dataset_id,
        "carry_dataset_id": carry.manifest.dataset_id,
        "status": (
            BasisMappingEvaluationStatus.FORWARD_RESEARCH
            if accepted
            else BasisMappingEvaluationStatus.REJECTED
        ),
        "validation": validation,
        "blind_evaluated": blind is not None,
        "blind": blind,
        "reason_codes": tuple(sorted(reasons)),
    }
    return BasisMappingEvaluationArtifact(
        artifact_id=stable_id("basis_mapping_evaluation", content_hash(values)),
        **values,
    )


def _validate_inputs(
    *,
    plan: dict[str, Any],
    spot: HistoricalDataset,
    carry: HistoricalCarryDataset,
) -> None:
    spot_manifest = spot.manifest
    carry_manifest = carry.manifest
    window = _mapping(plan, "data")["source_window"]
    expected_start, expected_end = (_plan_time(item) for item in window)
    if (
        spot_manifest.dataset_id != carry_manifest.spot_dataset_id
        or spot_manifest.symbol != "PAXGUSDT"
        or carry_manifest.symbol != "PAXGUSDT"
        or spot_manifest.interval != "1h"
        or carry_manifest.interval != "1h"
        or spot_manifest.requested_start != carry_manifest.requested_start
        or spot_manifest.requested_end != carry_manifest.requested_end
        or spot_manifest.requested_start != expected_start
        or spot_manifest.requested_end != expected_end
        or carry_manifest.funding_dataset_id is not None
        or carry.settlements
    ):
        raise ValueError("Basis 评价数据与预登记 Spot/Carry 作用域不一致")
    spot_times = tuple(item.open_time for item in spot.bars)
    carry_times = tuple(item.open_time for item in carry.bars)
    if spot_times != carry_times or len(spot_times) < 100:
        raise ValueError("Basis 评价要求完整对齐且非空的小时 K 线")


def _cases(
    *,
    spot: HistoricalDataset,
    carry: HistoricalCarryDataset,
    start: int,
    end: int,
) -> tuple[_BasisCase, ...]:
    cases = []
    for index in range(start, end - _HORIZON_BARS):
        spot_entry = spot.bars[index].close
        spot_exit = spot.bars[index + _HORIZON_BARS].close
        contract_entry = carry.bars[index].contract_close
        contract_exit = carry.bars[index + _HORIZON_BARS].contract_close
        entry_ratio = contract_entry / spot_entry
        exit_ratio = contract_exit / spot_exit
        entry_basis = (entry_ratio - Decimal("1")) * _BPS
        exit_basis = (exit_ratio - Decimal("1")) * _BPS
        cases.append(
            _BasisCase(
                sequence_index=index,
                entry_basis_bps=entry_basis,
                exit_basis_bps=exit_basis,
                baseline_mapping_residual_bps=Decimal("0"),
                candidate_mapping_residual_bps=(
                    Decimal("1") / entry_ratio - Decimal("1")
                )
                * _BPS,
                realized_mapping_residual_bps=(
                    exit_ratio / entry_ratio - Decimal("1")
                )
                * _BPS,
            )
        )
    if len(cases) < _PHASES:
        raise ValueError("Basis 评价分段没有足够样本")
    return tuple(cases)


def _split_evidence(cases: tuple[_BasisCase, ...]) -> BasisMappingSplitEvidence:
    overall = _metrics(cases)
    phases = tuple(
        BasisMappingPhaseMetrics(
            phase=phase,
            metrics=_metrics(
                tuple(item for item in cases if item.sequence_index % _PHASES == phase)
            ),
        )
        for phase in range(_PHASES)
    )
    return BasisMappingSplitEvidence(
        overall=overall,
        phases=phases,
        candidate_wins_every_phase=(
            _candidate_wins(overall)
            and all(_candidate_wins(item.metrics) for item in phases)
        ),
    )


def _metrics(cases: tuple[_BasisCase, ...]) -> BasisMappingMetrics:
    baseline_exit_mae = _mean(
        tuple(abs(item.exit_basis_bps - item.entry_basis_bps) for item in cases)
    )
    candidate_exit_mae = _mean(tuple(abs(item.exit_basis_bps) for item in cases))
    return BasisMappingMetrics(
        sample_count=len(cases),
        baseline_exit_basis_mae_bps=baseline_exit_mae,
        candidate_exit_basis_mae_bps=candidate_exit_mae,
        candidate_exit_basis_mae_improvement_bps=(
            baseline_exit_mae - candidate_exit_mae
        ),
        baseline_mapping_residual_mae_bps=_mean(
            tuple(
                abs(
                    item.realized_mapping_residual_bps
                    - item.baseline_mapping_residual_bps
                )
                for item in cases
            )
        ),
        candidate_mapping_residual_mae_bps=_mean(
            tuple(
                abs(
                    item.realized_mapping_residual_bps
                    - item.candidate_mapping_residual_bps
                )
                for item in cases
            )
        ),
        baseline_mapping_residual_sign_accuracy=_sign_accuracy(
            tuple(
                (
                    item.baseline_mapping_residual_bps,
                    item.realized_mapping_residual_bps,
                )
                for item in cases
            )
        ),
        candidate_mapping_residual_sign_accuracy=_sign_accuracy(
            tuple(
                (
                    item.candidate_mapping_residual_bps,
                    item.realized_mapping_residual_bps,
                )
                for item in cases
            )
        ),
    )


def _candidate_wins(metrics: BasisMappingMetrics) -> bool:
    return (
        metrics.candidate_exit_basis_mae_bps
        < metrics.baseline_exit_basis_mae_bps
    )


def _mean(values: tuple[Decimal, ...]) -> Decimal:
    if not values:
        raise ValueError("Basis 指标不能使用空样本")
    return sum(values, Decimal("0")) / Decimal(len(values))


def _sign_accuracy(values: tuple[tuple[Decimal, Decimal], ...]) -> Decimal:
    return Decimal(
        sum(_sign(expected) == _sign(realized) for expected, realized in values)
    ) / Decimal(len(values))


def _sign(value: Decimal) -> int:
    return (value > 0) - (value < 0)


def _mapping(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"Basis 研究计划缺少对象字段：{key}")
    return value


def _plan_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return require_utc(value)
    if not isinstance(value, str):
        raise ValueError("Basis 研究计划时间必须是 ISO-8601")
    return require_utc(datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC))


__all__ = [
    "BasisMappingEvaluationArtifact",
    "BasisMappingEvaluationStatus",
    "evaluate_basis_mapping",
    "load_basis_mapping_plan",
]
