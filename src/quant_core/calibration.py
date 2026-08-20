from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from quant_core.config import CalibrationPolicy
from quant_core.domain import (
    CandidateOutcome,
    CandidateOutcomeStatus,
    EdgeCalibration,
    Side,
    SignalCandidate,
    _require_utc,
)
from quant_core.ids import content_hash, stable_id

EDGE_CALIBRATION_MISSING = "EDGE_CALIBRATION_MISSING"


def uncalibrated_ref(
    producer_version: str,
    analysis_behavior_hash: str | None = None,
) -> str:
    """Identify one exact producer cohort before an edge artifact exists."""

    reference = f"uncalibrated:{producer_version}"
    if analysis_behavior_hash is None:
        return reference
    if not (
        len(analysis_behavior_hash) == 64
        and all(character in "0123456789abcdef" for character in analysis_behavior_hash)
    ):
        raise ValueError("analysis_behavior_hash 必须是 64 位十六进制摘要")
    return f"{reference}@{analysis_behavior_hash}"


def _is_uncalibrated_ref(reference: str, producer_version: str) -> bool:
    base = uncalibrated_ref(producer_version)
    if reference == base:
        return True
    prefix = f"{base}@"
    behavior_hash = reference.removeprefix(prefix)
    return (
        reference.startswith(prefix)
        and len(behavior_hash) == 64
        and all(character in "0123456789abcdef" for character in behavior_hash)
    )


@dataclass(frozen=True, slots=True)
class CalibrationBuildSpec:
    producer_id: str
    producer_version: str
    symbol: str
    side: Side
    horizon_minutes: int
    evaluation_version: str
    source_calibration_ref: str
    source_execution_policy_version: str
    source_frequency_policy_version: str
    training_start: datetime
    training_end: datetime
    published_at: datetime
    valid_from: datetime
    valid_until: datetime

    def __post_init__(self) -> None:
        for name in (
            "training_start",
            "training_end",
            "published_at",
            "valid_from",
            "valid_until",
        ):
            object.__setattr__(self, name, _require_utc(getattr(self, name)))
        if self.horizon_minutes <= 0:
            raise ValueError("校准周期必须为正数")
        if not (
            self.training_start
            < self.training_end
            <= self.published_at
            <= self.valid_from
            < self.valid_until
        ):
            raise ValueError("校准构建时间边界非法")


@dataclass(frozen=True, slots=True)
class EdgeCalibrationBuilder:
    """Build one immutable artifact from visible, exact-scope Shadow outcomes."""

    policy: CalibrationPolicy

    def build(
        self,
        outcomes: Iterable[CandidateOutcome],
        spec: CalibrationBuildSpec,
    ) -> EdgeCalibration:
        matched = tuple(
            sorted(
                (item for item in outcomes if self._eligible(item, spec)),
                key=lambda item: (item.signal_observed_at, item.outcome_id),
            )
        )
        independent: list[CandidateOutcome] = []
        last_evaluation: datetime | None = None
        for outcome in matched:
            if last_evaluation is not None and outcome.signal_observed_at < last_evaluation:
                continue
            independent.append(outcome)
            last_evaluation = outcome.evaluation_at
        if len(independent) < self.policy.minimum_non_overlapping_samples:
            raise ValueError("校准构建的非重叠成熟样本不足")

        gross_returns = tuple(self._gross_return(item) for item in independent)
        mean = sum(gross_returns, Decimal("0")) / Decimal(len(gross_returns))
        variance = sum(((value - mean) ** 2 for value in gross_returns), Decimal("0")) / Decimal(
            len(gross_returns) - 1
        )
        standard_error = variance.sqrt() / Decimal(len(gross_returns)).sqrt()
        conservative = mean - self.policy.lower_confidence_z * standard_error
        dataset_payload = {
            "method_version": self.policy.method_version,
            "lower_confidence_z": self.policy.lower_confidence_z,
            "matched": tuple(self._outcome_fact(item) for item in matched),
            "non_overlapping_outcome_ids": tuple(item.outcome_id for item in independent),
        }
        dataset_hash = content_hash(dataset_payload)
        calibration_id = stable_id(
            "edge_calibration",
            spec.producer_id,
            spec.producer_version,
            spec.symbol,
            spec.side.value,
            spec.horizon_minutes,
            spec.evaluation_version,
            spec.source_calibration_ref,
            spec.source_execution_policy_version,
            spec.source_frequency_policy_version,
            spec.training_start.isoformat(),
            spec.training_end.isoformat(),
            spec.published_at.isoformat(),
            spec.valid_from.isoformat(),
            spec.valid_until.isoformat(),
            self.policy.method_version,
            str(self.policy.lower_confidence_z),
            dataset_hash,
        )
        payload = {
            "calibration_id": calibration_id,
            "producer_id": spec.producer_id,
            "producer_version": spec.producer_version,
            "symbol": spec.symbol,
            "side": spec.side,
            "horizon_minutes": spec.horizon_minutes,
            "expected_gross_bps": mean,
            "conservative_gross_bps": conservative,
            "sample_size": len(matched),
            "non_overlapping_sample_size": len(independent),
            "training_start": spec.training_start,
            "training_end": spec.training_end,
            "published_at": spec.published_at,
            "valid_from": spec.valid_from,
            "valid_until": spec.valid_until,
            "evaluation_version": spec.evaluation_version,
            "source_calibration_ref": spec.source_calibration_ref,
            "source_execution_policy_version": spec.source_execution_policy_version,
            "source_frequency_policy_version": spec.source_frequency_policy_version,
            "method_version": self.policy.method_version,
            "dataset_hash": dataset_hash,
        }
        return EdgeCalibration(
            **payload,
            artifact_hash=content_hash(payload),
        )

    @staticmethod
    def _eligible(outcome: CandidateOutcome, spec: CalibrationBuildSpec) -> bool:
        horizon = int((outcome.evaluation_at - outcome.signal_observed_at).total_seconds() // 60)
        return (
            outcome.status == CandidateOutcomeStatus.SETTLED
            and outcome.producer_id == spec.producer_id
            and outcome.producer_version == spec.producer_version
            and outcome.symbol == spec.symbol
            and outcome.side == spec.side
            and horizon == spec.horizon_minutes
            and outcome.evaluation_version == spec.evaluation_version
            and outcome.calibration_ref == spec.source_calibration_ref
            and outcome.execution_policy_version == spec.source_execution_policy_version
            and outcome.frequency_policy_version == spec.source_frequency_policy_version
            and spec.training_start <= outcome.signal_observed_at
            and outcome.evaluation_at <= spec.training_end
            and outcome.settled_at <= spec.published_at
        )

    @staticmethod
    def _gross_return(outcome: CandidateOutcome) -> Decimal:
        if outcome.gross_return_bps is None:
            raise ValueError("SETTLED 校准样本缺少毛收益")
        return outcome.gross_return_bps

    @staticmethod
    def _outcome_fact(outcome: CandidateOutcome) -> dict[str, object]:
        return {
            "outcome_id": outcome.outcome_id,
            "candidate_id": outcome.candidate_id,
            "signal_observed_at": outcome.signal_observed_at,
            "evaluation_at": outcome.evaluation_at,
            "settled_at": outcome.settled_at,
            "gross_return_bps": outcome.gross_return_bps,
        }


@dataclass(frozen=True, slots=True)
class EdgeCalibrationBook:
    """Resolve one published calibration without training or mutating it online."""

    policy: CalibrationPolicy

    def apply(self, candidate: SignalCandidate) -> SignalCandidate:
        if candidate.expected_gross_bps != Decimal(
            "0"
        ) or not _is_uncalibrated_ref(
            candidate.calibration_ref,
            candidate.producer_version,
        ):
            raise ValueError("Candidate Producer 不得自行填充校准收益")
        matches = [
            artifact
            for artifact in self.policy.artifacts
            if artifact.scope
            == (
                candidate.producer_id,
                candidate.producer_version,
                candidate.symbol,
                candidate.side,
                candidate.horizon_minutes,
            )
            and artifact.source_calibration_ref == candidate.calibration_ref
            and artifact.valid_from <= candidate.signal_observed_at < artifact.valid_until
        ]
        if not matches:
            unknowns = tuple(dict.fromkeys((*candidate.unknowns, EDGE_CALIBRATION_MISSING)))
            return candidate.model_copy(update={"unknowns": unknowns})
        if len(matches) != 1:
            raise ValueError("Candidate 同时匹配多个校准制品")
        artifact = matches[0]
        unknowns = tuple(item for item in candidate.unknowns if item != EDGE_CALIBRATION_MISSING)
        return candidate.model_copy(
            update={
                "expected_gross_bps": artifact.conservative_gross_bps,
                "calibration_ref": artifact.calibration_id,
                "unknowns": unknowns,
            }
        )
