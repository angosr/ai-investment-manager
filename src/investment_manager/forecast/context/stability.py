"""Prospective exact-input stability evidence for one AI Forecast behavior."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import Field, TypeAdapter, field_validator, model_validator
from sqlalchemy import insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.forecast.codex.bundle import load_existing_bundle, write_run_bundle
from investment_manager.forecast.codex.repository import SqlAccountLeaseStore, SqlCodexAuditStore
from investment_manager.forecast.codex.router import (
    AnalystResult,
    CodexAccountRouter,
    assemble_codex_router,
)
from investment_manager.forecast.context.estimate import (
    CONTEXT_FORECAST_INPUT_VERSION,
    CONTEXT_FORECAST_OUTPUT_VERSION,
    ContextForecastStructuredOutput,
    QuantContextPosteriorStructuredOutput,
    context_forecast_prompt,
    context_forecast_runtime,
)
from investment_manager.forecast.context.posterior_prompt import (
    POSTERIOR_INPUT_VERSION,
    quant_context_posterior_prompt,
)
from investment_manager.forecast.contracts import ForecastDecisionSlot
from investment_manager.forecast.results import BaseForecast
from investment_manager.forecast.tables import (
    context_forecast_stability_assignments,
    context_forecast_stability_results,
    forecasts,
)
from investment_manager.governance.policy import ContextForecastStabilityPolicy
from investment_manager.kernel.identity import canonical_json, content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.settings import AppConfig

_FORECAST_OUTPUT_ADAPTER = TypeAdapter(
    ContextForecastStructuredOutput | QuantContextPosteriorStructuredOutput
)

STABILITY_EVALUATION_VERSION = "context-forecast-exact-input-stability-v1"


class ContextForecastStabilityStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ContextForecastStabilityTarget(FrozenModel):
    formal_forecast_id: str = Field(min_length=1)
    decision_slot_id: str = Field(min_length=1)
    contract_id: str = Field(min_length=1)
    representative_bps: tuple[tuple[str, Decimal], ...] = Field(min_length=3)

    @model_validator(mode="after")
    def buckets_are_unique(self):
        bucket_ids = tuple(item[0] for item in self.representative_bps)
        if len(set(bucket_ids)) != len(bucket_ids):
            raise ValueError("Context Forecast stability bucket 不得重复")
        return self


class ContextForecastStabilityAssignment(FrozenModel):
    assignment_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    formal_producer_behavior_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    targets: tuple[ContextForecastStabilityTarget, ...] = Field(min_length=1)
    information_cutoff_at: datetime
    assigned_at: datetime
    completion_deadline_at: datetime
    evaluation_at: datetime
    replicas_per_input: int = Field(ge=1, le=3)
    formal_analysis_input_json: str = Field(min_length=2)
    formal_analysis_input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    formal_prompt: str = Field(min_length=1)
    formal_prompt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    formal_output_schema_json: str = Field(min_length=2)
    formal_output_schema_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    _utc_times = field_validator(
        "information_cutoff_at",
        "assigned_at",
        "completion_deadline_at",
        "evaluation_at",
    )(require_utc)

    @model_validator(mode="after")
    def exact_input_and_identity_are_frozen(self):
        if not (
            self.information_cutoff_at
            <= self.assigned_at
            < self.completion_deadline_at
            < self.evaluation_at
        ):
            raise ValueError("Context Forecast stability 时间边界非法")
        analysis_input = _canonical_object(
            self.formal_analysis_input_json,
            "formal analysis input",
        )
        output_schema = _canonical_object(
            self.formal_output_schema_json,
            "formal output schema",
        )
        if content_hash(analysis_input) != self.formal_analysis_input_hash:
            raise ValueError("Context Forecast stability input hash 不一致")
        if content_hash({"prompt": self.formal_prompt}) != self.formal_prompt_hash:
            raise ValueError("Context Forecast stability prompt hash 不一致")
        if content_hash(output_schema) != self.formal_output_schema_hash:
            raise ValueError("Context Forecast stability schema hash 不一致")
        if self.formal_prompt != _stability_prompt(analysis_input):
            raise ValueError("Context Forecast stability prompt 不是正式输入的唯一投影")
        slot_ids = tuple(item.decision_slot_id for item in self.targets)
        if len(set(slot_ids)) != len(slot_ids):
            raise ValueError("Context Forecast stability target 不得重复")
        expected_id = stable_id(
            "context_forecast_stability_assignment",
            self.policy_version,
            self.formal_producer_behavior_id,
            self.targets[0].formal_forecast_id,
        )
        if self.assignment_id != expected_id:
            raise ValueError("Context Forecast stability assignment_id 不一致")
        if content_hash(
            self.model_dump(mode="json", exclude={"source_hash"})
        ) != self.source_hash:
            raise ValueError("Context Forecast stability source_hash 不一致")
        return self


class ContextForecastStabilityResult(FrozenModel):
    result_id: str = Field(min_length=1)
    assignment_id: str = Field(min_length=1)
    replica_index: int = Field(ge=1, le=3)
    status: ContextForecastStabilityStatus
    completed_at: datetime
    reason_code: str = Field(min_length=1)
    account_id: str | None = None
    attempts: int = Field(default=0, ge=0)
    usage: tuple[tuple[str, int], ...] = ()
    codex_run_id: str | None = None
    output_json: str | None = None
    output_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    _utc_completed_at = field_validator("completed_at")(require_utc)

    @model_validator(mode="after")
    def completion_is_atomic(self):
        succeeded = self.status == ContextForecastStabilityStatus.SUCCEEDED
        if succeeded != (self.output_json is not None and self.output_hash is not None):
            raise ValueError("Context Forecast stability 成功状态与输出不一致")
        if tuple(sorted(set(self.usage))) != self.usage:
            raise ValueError("Context Forecast stability usage 必须唯一且排序")
        if self.output_json is not None:
            output = _canonical_object(self.output_json, "replica output")
            _FORECAST_OUTPUT_ADAPTER.validate_python(output)
            if content_hash(output) != self.output_hash:
                raise ValueError("Context Forecast stability output hash 不一致")
        if self.result_id != stable_id(
            "context_forecast_stability_result",
            self.assignment_id,
            self.replica_index,
        ):
            raise ValueError("Context Forecast stability result_id 不一致")
        return self


class ContextForecastStabilityReport(FrozenModel):
    evaluation_version: str
    policy_version: str
    formal_producer_behavior_id: str
    as_of: datetime
    assignment_count: int = Field(ge=0)
    pending_replica_count: int = Field(ge=0)
    successful_replica_count: int = Field(ge=0)
    failed_replica_count: int = Field(ge=0)
    complete_sample_count: int = Field(ge=0)
    mean_max_total_variation: Decimal | None = None
    maximum_total_variation: Decimal | None = None
    mean_max_expected_gross_difference_bps: Decimal | None = None
    p95_max_expected_gross_difference_bps: Decimal | None = None
    maximum_expected_gross_difference_bps: Decimal | None = None
    canonical_direction_flip_count: int = Field(ge=0)

    _utc_as_of = field_validator("as_of")(require_utc)


def build_context_forecast_stability_assignment(
    *,
    policy: ContextForecastStabilityPolicy,
    slot: ForecastDecisionSlot,
    formal_producer_behavior_id: str,
    formal_analysis_input: dict[str, object],
    formal_output_schema: dict[str, object],
    assigned_at: datetime,
) -> ContextForecastStabilityAssignment:
    raw = _canonical_object(canonical_json(formal_analysis_input), "formal analysis input")
    raw_targets = raw.get("forecast_targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ValueError("Context Forecast stability 正式输入缺少 targets")
    targets: list[ContextForecastStabilityTarget] = []
    for raw_target in raw_targets:
        if not isinstance(raw_target, dict):
            raise ValueError("Context Forecast stability target 结构非法")
        decision_slot = raw_target.get("decision_slot")
        contract = raw_target.get("forecast_contract")
        if not isinstance(decision_slot, dict) or not isinstance(contract, dict):
            raise ValueError("Context Forecast stability target 边界不完整")
        slot_id = decision_slot.get("decision_slot_id")
        contract_id = contract.get("contract_id")
        buckets = contract.get("outcome_buckets")
        if (
            not isinstance(slot_id, str)
            or not isinstance(contract_id, str)
            or not isinstance(buckets, list)
        ):
            raise ValueError("Context Forecast stability target 身份非法")
        if (
            decision_slot.get("information_cutoff_at")
            != slot.information_cutoff_at.isoformat().replace("+00:00", "Z")
            or decision_slot.get("completion_deadline_at")
            != slot.completion_deadline_at.isoformat().replace("+00:00", "Z")
            or decision_slot.get("evaluation_at")
            != slot.evaluation_at.isoformat().replace("+00:00", "Z")
        ):
            raise ValueError("Context Forecast stability targets 必须共享正式时间边界")
        representative = tuple(
            (str(bucket["bucket_id"]), Decimal(str(bucket["representative_bps"])))
            for bucket in buckets
            if isinstance(bucket, dict)
            and isinstance(bucket.get("bucket_id"), str)
            and bucket.get("representative_bps") is not None
        )
        if len(representative) != len(buckets):
            raise ValueError("Context Forecast stability bucket 代表收益不完整")
        targets.append(
            ContextForecastStabilityTarget(
                formal_forecast_id=stable_id(
                    "base_forecast",
                    slot_id,
                    formal_producer_behavior_id,
                ),
                decision_slot_id=slot_id,
                contract_id=contract_id,
                representative_bps=representative,
            )
        )
    if slot.slot_id not in {item.decision_slot_id for item in targets}:
        raise ValueError("Context Forecast stability 正式组合缺少锚点 slot")
    prompt = _stability_prompt(raw)
    output_schema = _canonical_object(canonical_json(formal_output_schema), "formal schema")
    values = {
        "assignment_id": _stability_assignment_id(
            policy=policy,
            producer_behavior_id=formal_producer_behavior_id,
            analysis_input=raw,
        ),
        "policy_version": policy.version,
        "formal_producer_behavior_id": formal_producer_behavior_id,
        "targets": tuple(targets),
        "information_cutoff_at": slot.information_cutoff_at,
        "assigned_at": require_utc(assigned_at),
        "completion_deadline_at": slot.completion_deadline_at,
        "evaluation_at": slot.evaluation_at,
        "replicas_per_input": policy.replicas_per_input,
        "formal_analysis_input_json": canonical_json(raw),
        "formal_analysis_input_hash": content_hash(raw),
        "formal_prompt": prompt,
        "formal_prompt_hash": content_hash({"prompt": prompt}),
        "formal_output_schema_json": canonical_json(output_schema),
        "formal_output_schema_hash": content_hash(output_schema),
    }
    values["source_hash"] = content_hash(values)
    return ContextForecastStabilityAssignment.model_validate(values)


@dataclass(slots=True)
class ContextForecastStabilityPreallocator:
    policy: ContextForecastStabilityPolicy
    formal_producer_behavior_id: str
    repository: SqlContextForecastStabilityRepository
    clock: Callable[[], datetime]

    def before_estimate(
        self,
        *,
        slot: ForecastDecisionSlot,
        formal_producer_behavior_id: str,
        formal_analysis_input: dict[str, object] | None,
        formal_output_schema: dict[str, object] | None,
    ) -> None:
        if slot.information_cutoff_at < self.policy.activated_at:
            return
        if formal_producer_behavior_id != self.formal_producer_behavior_id:
            raise ValueError("Context Forecast stability preflight 行为身份不一致")
        if formal_analysis_input is None or formal_output_schema is None:
            return
        preregister_context_forecast_stability(
            policy=self.policy,
            repository=self.repository,
            slot=slot,
            producer_behavior_id=formal_producer_behavior_id,
            analysis_input=formal_analysis_input,
            output_schema=formal_output_schema,
            assigned_at=require_utc(self.clock()),
        )


def preregister_context_forecast_stability(
    *,
    policy: ContextForecastStabilityPolicy,
    repository: SqlContextForecastStabilityRepository,
    slot: ForecastDecisionSlot,
    producer_behavior_id: str,
    analysis_input: dict[str, object],
    output_schema: dict[str, object],
    assigned_at: datetime,
) -> ContextForecastStabilityAssignment | None:
    """Freeze one exact-input replica task before the producer's first call."""

    if slot.information_cutoff_at < policy.activated_at:
        return None
    frozen_input = _canonical_object(
        canonical_json(analysis_input),
        "stability analysis input",
    )
    assignment_id = _stability_assignment_id(
        policy=policy,
        producer_behavior_id=producer_behavior_id,
        analysis_input=frozen_input,
    )
    existing = repository.assignment(assignment_id)
    frozen_assigned_at = (
        existing.assigned_at if existing is not None else require_utc(assigned_at)
    )
    if existing is None and frozen_assigned_at >= slot.completion_deadline_at:
        return None
    expected = build_context_forecast_stability_assignment(
        policy=policy,
        slot=slot,
        formal_producer_behavior_id=producer_behavior_id,
        formal_analysis_input=analysis_input,
        formal_output_schema=output_schema,
        assigned_at=frozen_assigned_at,
    )
    if existing is not None:
        if existing != expected:
            raise ValueError("Context Forecast stability 重试绑定了不同输入")
        return existing
    repository.record_assignment(expected)
    return expected


class ContextForecastReplicaAnalyst(Protocol):
    def estimate(
        self,
        assignment: ContextForecastStabilityAssignment,
        replica_index: int,
    ) -> AnalystResult: ...


@dataclass(slots=True)
class CodexContextForecastReplicaAnalyst:
    bundle_root: Path
    router: CodexAccountRouter

    def estimate(
        self,
        assignment: ContextForecastStabilityAssignment,
        replica_index: int,
    ) -> AnalystResult:
        result_id = stable_id(
            "context_forecast_stability_result",
            assignment.assignment_id,
            replica_index,
        )
        target = self.bundle_root / stable_id(
            "context_forecast_stability_bundle",
            result_id,
            assignment.formal_producer_behavior_id,
        )
        input_version, input_filename = _stability_input_contract(assignment)
        try:
            bundle = load_existing_bundle(
                cycle_id=result_id,
                target=target,
                expected_manifest={
                    "analysis_mode": "CONTEXT_FORECAST_STABILITY_REPLICA",
                    "assignment_id": assignment.assignment_id,
                    "replica_index": replica_index,
                    "analysis_behavior_hash": assignment.formal_producer_behavior_id,
                },
            )
            if bundle is None:
                bundle = write_run_bundle(
                    cycle_id=result_id,
                    target=target,
                    prompt=assignment.formal_prompt,
                    files={
                        input_filename: assignment.formal_analysis_input_json + "\n",
                        "analyst_prompt.md": assignment.formal_prompt + "\n",
                        "output.schema.json": assignment.formal_output_schema_json + "\n",
                    },
                    manifest={
                        "analysis_mode": "CONTEXT_FORECAST_STABILITY_REPLICA",
                        "input_version": input_version,
                        "output_version": CONTEXT_FORECAST_OUTPUT_VERSION,
                        "assignment_id": assignment.assignment_id,
                        "replica_index": replica_index,
                        "policy_version": assignment.policy_version,
                        "analysis_behavior_hash": assignment.formal_producer_behavior_id,
                        "formal_analysis_input_hash": (
                            assignment.formal_analysis_input_hash
                        ),
                        "output_schema_hash": assignment.formal_output_schema_hash,
                    },
                )
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return AnalystResult(False, None, "CODEX_BUNDLE_INVALID")
        return self.router.run(bundle)


class SqlContextForecastStabilityRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record_assignment(self, assignment: ContextForecastStabilityAssignment) -> bool:
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(context_forecast_stability_assignments).values(
                        assignment_id=assignment.assignment_id,
                        policy_version=assignment.policy_version,
                        formal_producer_behavior_id=(
                            assignment.formal_producer_behavior_id
                        ),
                        information_cutoff_at=assignment.information_cutoff_at,
                        assigned_at=assignment.assigned_at,
                        completion_deadline_at=assignment.completion_deadline_at,
                        source_hash=assignment.source_hash,
                        payload=assignment.model_dump(mode="json"),
                    )
                )
            return True
        except IntegrityError:
            existing = self.assignment(assignment.assignment_id)
            if existing != assignment:
                raise ValueError(
                    "Context Forecast stability assignment 已存在且内容不同"
                ) from None
            return False

    def assignment(self, assignment_id: str) -> ContextForecastStabilityAssignment | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(context_forecast_stability_assignments.c.payload).where(
                    context_forecast_stability_assignments.c.assignment_id == assignment_id
                )
            ).scalar_one_or_none()
        return (
            None
            if payload is None
            else ContextForecastStabilityAssignment.model_validate(payload)
        )

    def assignments(
        self,
        *,
        policy_version: str,
        formal_producer_behavior_id: str,
    ) -> tuple[ContextForecastStabilityAssignment, ...]:
        with self._engine.connect() as connection:
            payloads = connection.execute(
                select(context_forecast_stability_assignments.c.payload)
                .where(
                    context_forecast_stability_assignments.c.policy_version
                    == policy_version,
                    context_forecast_stability_assignments.c.formal_producer_behavior_id
                    == formal_producer_behavior_id,
                )
                .order_by(
                    context_forecast_stability_assignments.c.information_cutoff_at,
                    context_forecast_stability_assignments.c.assignment_id,
                )
            ).scalars()
            return tuple(
                ContextForecastStabilityAssignment.model_validate(item)
                for item in payloads
            )

    def results(
        self,
        assignment_ids: tuple[str, ...],
    ) -> tuple[ContextForecastStabilityResult, ...]:
        if not assignment_ids:
            return ()
        with self._engine.connect() as connection:
            payloads = connection.execute(
                select(context_forecast_stability_results.c.payload)
                .where(
                    context_forecast_stability_results.c.assignment_id.in_(
                        assignment_ids
                    )
                )
                .order_by(
                    context_forecast_stability_results.c.assignment_id,
                    context_forecast_stability_results.c.replica_index,
                )
            ).scalars()
            return tuple(
                ContextForecastStabilityResult.model_validate(item)
                for item in payloads
            )

    def record_result(self, result: ContextForecastStabilityResult) -> bool:
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(context_forecast_stability_results).values(
                        result_id=result.result_id,
                        assignment_id=result.assignment_id,
                        replica_index=result.replica_index,
                        status=result.status.value,
                        completed_at=result.completed_at,
                        payload=result.model_dump(mode="json"),
                    )
                )
            return True
        except IntegrityError:
            existing = next(
                (
                    item
                    for item in self.results((result.assignment_id,))
                    if item.replica_index == result.replica_index
                ),
                None,
            )
            if existing != result:
                raise ValueError(
                    "Context Forecast stability result 已存在且内容不同"
                ) from None
            return False

    def formal_forecasts(
        self,
        forecast_ids: tuple[str, ...],
    ) -> dict[str, BaseForecast]:
        if not forecast_ids:
            return {}
        with self._engine.connect() as connection:
            payloads = connection.execute(
                select(forecasts.c.payload).where(forecasts.c.forecast_id.in_(forecast_ids))
            ).scalars()
            values = tuple(BaseForecast.model_validate(item) for item in payloads)
        return {item.forecast_id: item for item in values}

@dataclass(slots=True)
class ContextForecastStabilityRunner:
    policy: ContextForecastStabilityPolicy
    formal_producer_behavior_id: str
    repository: SqlContextForecastStabilityRepository
    analyst: ContextForecastReplicaAnalyst

    def reconcile(self, *, as_of: datetime) -> ContextForecastStabilityReport:
        now = require_utc(as_of)
        assignments = self.repository.assignments(
            policy_version=self.policy.version,
            formal_producer_behavior_id=self.formal_producer_behavior_id,
        )
        result_by_key = {
            (item.assignment_id, item.replica_index): item
            for item in self.repository.results(
                tuple(item.assignment_id for item in assignments)
            )
        }
        for assignment in assignments:
            for replica_index in range(1, assignment.replicas_per_input + 1):
                key = (assignment.assignment_id, replica_index)
                if key in result_by_key:
                    continue
                result = (
                    _failed_result(
                        assignment,
                        replica_index=replica_index,
                        completed_at=now,
                        reason_code="STABILITY_REPLICA_DEADLINE_MISSED",
                    )
                    if now > assignment.completion_deadline_at
                    else _result_from_analyst(
                        assignment,
                        replica_index,
                        self.analyst.estimate(assignment, replica_index),
                        fallback_completed_at=now,
                    )
                )
                self.repository.record_result(result)
                result_by_key[key] = result
        return evaluate_context_forecast_stability(
            policy=self.policy,
            formal_producer_behavior_id=self.formal_producer_behavior_id,
            assignments=assignments,
            results=tuple(result_by_key.values()),
            formal_forecasts=self.repository.formal_forecasts(
                tuple(
                    target.formal_forecast_id
                    for assignment in assignments
                    for target in assignment.targets
                )
            ),
            as_of=now,
        )


def evaluate_context_forecast_stability(
    *,
    policy: ContextForecastStabilityPolicy,
    formal_producer_behavior_id: str,
    assignments: tuple[ContextForecastStabilityAssignment, ...],
    results: tuple[ContextForecastStabilityResult, ...],
    formal_forecasts: dict[str, BaseForecast],
    as_of: datetime,
) -> ContextForecastStabilityReport:
    result_by_key = {(item.assignment_id, item.replica_index): item for item in results}
    pending = failed = succeeded = 0
    total_variations: list[Decimal] = []
    expected_differences: list[Decimal] = []
    direction_flips = 0
    for assignment in assignments:
        replicas: list[
            ContextForecastStructuredOutput | QuantContextPosteriorStructuredOutput
        ] = []
        terminal = True
        successful = True
        for replica_index in range(1, assignment.replicas_per_input + 1):
            result = result_by_key.get((assignment.assignment_id, replica_index))
            if result is None:
                pending += 1
                terminal = successful = False
                continue
            if result.status != ContextForecastStabilityStatus.SUCCEEDED:
                failed += 1
                successful = False
                continue
            succeeded += 1
            assert result.output_json is not None
            replicas.append(
                _FORECAST_OUTPUT_ADAPTER.validate_json(result.output_json)
            )
        if not terminal or not successful:
            continue
        formal_by_slot = {
            target.decision_slot_id: formal_forecasts.get(target.formal_forecast_id)
            for target in assignment.targets
        }
        if any(item is None for item in formal_by_slot.values()):
            continue
        assignment_tv = Decimal("0")
        assignment_expected = Decimal("0")
        assignment_flipped = False
        target_by_slot = {item.decision_slot_id: item for item in assignment.targets}
        for replica in replicas:
            replica_by_slot = {item.decision_slot_id: item for item in replica.forecasts}
            if set(replica_by_slot) != set(target_by_slot):
                raise ValueError("Context Forecast stability replica target 集合漂移")
            for slot_id, target in target_by_slot.items():
                formal = formal_by_slot[slot_id]
                assert formal is not None
                formal_probabilities = {
                    item.bucket_id: item.probability
                    for item in formal.outcome_probabilities
                }
                replica_probabilities = {
                    item.bucket_id: Decimal(item.probability)
                    for item in replica_by_slot[slot_id].outcome_probabilities
                }
                representative = dict(target.representative_bps)
                if set(formal_probabilities) != set(replica_probabilities) or set(
                    representative
                ) != set(formal_probabilities):
                    raise ValueError("Context Forecast stability bucket 集合漂移")
                tv = sum(
                    (
                        abs(replica_probabilities[key] - formal_probabilities[key])
                        for key in formal_probabilities
                    ),
                    Decimal("0"),
                ) / Decimal("2")
                formal_expected = sum(
                    (
                        formal_probabilities[key] * representative[key]
                        for key in formal_probabilities
                    ),
                    Decimal("0"),
                )
                replica_expected = sum(
                    (
                        replica_probabilities[key] * representative[key]
                        for key in formal_probabilities
                    ),
                    Decimal("0"),
                )
                expected_difference = abs(replica_expected - formal_expected)
                assignment_tv = max(assignment_tv, tv)
                assignment_expected = max(assignment_expected, expected_difference)
                assignment_flipped = assignment_flipped or _direction(
                    formal_expected
                ) != _direction(replica_expected)
        total_variations.append(assignment_tv)
        expected_differences.append(assignment_expected)
        direction_flips += assignment_flipped
    return ContextForecastStabilityReport(
        evaluation_version=STABILITY_EVALUATION_VERSION,
        policy_version=policy.version,
        formal_producer_behavior_id=formal_producer_behavior_id,
        as_of=require_utc(as_of),
        assignment_count=len(assignments),
        pending_replica_count=pending,
        successful_replica_count=succeeded,
        failed_replica_count=failed,
        complete_sample_count=len(total_variations),
        mean_max_total_variation=_mean(total_variations),
        maximum_total_variation=max(total_variations, default=None),
        mean_max_expected_gross_difference_bps=_mean(expected_differences),
        p95_max_expected_gross_difference_bps=_nearest_rank(
            expected_differences,
            Decimal("0.95"),
        ),
        maximum_expected_gross_difference_bps=max(expected_differences, default=None),
        canonical_direction_flip_count=direction_flips,
    )


def assemble_context_forecast_stability_preallocator(
    config: AppConfig,
    *,
    engine: Engine,
    clock,
) -> ContextForecastStabilityPreallocator | None:
    policy = config.outcome_evaluation.context_forecast_stability
    context = config.capital.context_forecast
    if policy is None or not policy.enabled:
        return None
    if context is None or not context.enabled:
        raise ValueError("启用 Context Forecast stability 必须绑定正式 Forecast")
    return ContextForecastStabilityPreallocator(
        policy=policy,
        formal_producer_behavior_id=context.producer_behavior_id,
        repository=SqlContextForecastStabilityRepository(engine),
        clock=clock,
    )


def assemble_context_forecast_stability_runner(
    config: AppConfig,
    *,
    engine: Engine,
    producer_behavior_id: str | None = None,
) -> ContextForecastStabilityRunner | None:
    policy = config.outcome_evaluation.context_forecast_stability
    context = config.capital.context_forecast
    if policy is None or not policy.enabled:
        return None
    if context is None or not context.enabled:
        raise ValueError("启用 Context Forecast stability 必须绑定正式 Forecast")
    runtime = context_forecast_runtime(config.codex_runtime, context)
    router = assemble_codex_router(
        config,
        leases=SqlAccountLeaseStore(engine),
        audit=SqlCodexAuditStore(engine),
        output_adapter=_FORECAST_OUTPUT_ADAPTER,
        runtime_policy=runtime,
    )
    return ContextForecastStabilityRunner(
        policy=policy,
        formal_producer_behavior_id=(
            context.producer_behavior_id
            if producer_behavior_id is None
            else producer_behavior_id
        ),
        repository=SqlContextForecastStabilityRepository(engine),
        analyst=CodexContextForecastReplicaAnalyst(
            bundle_root=runtime.bundle_root,
            router=router,
        ),
    )


def _result_from_analyst(
    assignment: ContextForecastStabilityAssignment,
    replica_index: int,
    analyst: AnalystResult,
    *,
    fallback_completed_at: datetime,
) -> ContextForecastStabilityResult:
    completed = require_utc(analyst.completed_at or fallback_completed_at)
    common = {
        "result_id": stable_id(
            "context_forecast_stability_result",
            assignment.assignment_id,
            replica_index,
        ),
        "assignment_id": assignment.assignment_id,
        "replica_index": replica_index,
        "completed_at": completed,
        "account_id": analyst.account_id,
        "attempts": analyst.attempts,
        "usage": tuple(sorted((key, int(value)) for key, value in analyst.usage.items())),
        "codex_run_id": analyst.run_id,
    }
    if completed > assignment.completion_deadline_at:
        return ContextForecastStabilityResult(
            **common,
            status=ContextForecastStabilityStatus.FAILED,
            reason_code="STABILITY_REPLICA_DEADLINE_MISSED",
        )
    if not analyst.success or not isinstance(
        analyst.output,
        (ContextForecastStructuredOutput, QuantContextPosteriorStructuredOutput),
    ):
        return ContextForecastStabilityResult(
            **common,
            status=ContextForecastStabilityStatus.FAILED,
            reason_code=analyst.reason_code,
        )
    expected_buckets = {
        item.decision_slot_id: tuple(bucket_id for bucket_id, _ in item.representative_bps)
        for item in assignment.targets
    }
    drafts = {item.decision_slot_id: item for item in analyst.output.forecasts}
    valid = len(drafts) == len(analyst.output.forecasts) and set(drafts) == set(
        expected_buckets
    )
    if valid:
        for slot_id, draft in drafts.items():
            bucket_ids = tuple(item.bucket_id for item in draft.outcome_probabilities)
            probabilities = tuple(
                Decimal(item.probability) for item in draft.outcome_probabilities
            )
            if bucket_ids != expected_buckets[slot_id] or sum(
                probabilities,
                Decimal("0"),
            ) != Decimal("1"):
                valid = False
                break
    if not valid:
        return ContextForecastStabilityResult(
            **common,
            status=ContextForecastStabilityStatus.FAILED,
            reason_code="STABILITY_REPLICA_OUTPUT_CONTRACT_INVALID",
        )
    output = analyst.output.model_dump(mode="json")
    return ContextForecastStabilityResult(
        **common,
        status=ContextForecastStabilityStatus.SUCCEEDED,
        reason_code="STABILITY_REPLICA_SUCCEEDED",
        output_json=canonical_json(output),
        output_hash=content_hash(output),
    )


def _failed_result(
    assignment: ContextForecastStabilityAssignment,
    *,
    replica_index: int,
    completed_at: datetime,
    reason_code: str,
) -> ContextForecastStabilityResult:
    return ContextForecastStabilityResult(
        result_id=stable_id(
            "context_forecast_stability_result",
            assignment.assignment_id,
            replica_index,
        ),
        assignment_id=assignment.assignment_id,
        replica_index=replica_index,
        status=ContextForecastStabilityStatus.FAILED,
        completed_at=require_utc(completed_at),
        reason_code=reason_code,
    )


def _canonical_object(raw: str, name: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} 不是有效 JSON") from exc
    if not isinstance(value, dict) or canonical_json(value) != raw:
        raise ValueError(f"{name} 必须是规范 JSON 对象")
    return value


def _stability_prompt(analysis_input: dict[str, object]) -> str:
    purpose = analysis_input.get("purpose")
    if purpose == "FORECAST_ESTIMATE":
        return context_forecast_prompt(analysis_input)
    if purpose == "QUANT_CONTEXT_POSTERIOR":
        return quant_context_posterior_prompt(analysis_input)
    raise ValueError("Context Forecast stability 不支持该输入用途")


def _stability_input_contract(
    assignment: ContextForecastStabilityAssignment,
) -> tuple[str, str]:
    purpose = _canonical_object(
        assignment.formal_analysis_input_json,
        "stability analysis input",
    ).get("purpose")
    if purpose == "FORECAST_ESTIMATE":
        return CONTEXT_FORECAST_INPUT_VERSION, "context_forecast_input.json"
    if purpose == "QUANT_CONTEXT_POSTERIOR":
        return POSTERIOR_INPUT_VERSION, "quant_context_posterior_input.json"
    raise ValueError("Context Forecast stability 不支持该输入用途")


def _stability_assignment_id(
    *,
    policy: ContextForecastStabilityPolicy,
    producer_behavior_id: str,
    analysis_input: dict[str, object],
) -> str:
    raw_targets = analysis_input.get("forecast_targets")
    first_target = raw_targets[0] if isinstance(raw_targets, list) and raw_targets else None
    decision_slot = (
        first_target.get("decision_slot") if isinstance(first_target, dict) else None
    )
    slot_id = decision_slot.get("decision_slot_id") if isinstance(decision_slot, dict) else None
    if not isinstance(slot_id, str):
        raise ValueError("Context Forecast stability 正式输入缺少主目标")
    return stable_id(
        "context_forecast_stability_assignment",
        policy.version,
        producer_behavior_id,
        stable_id("base_forecast", slot_id, producer_behavior_id),
    )


def _direction(value: Decimal) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def _mean(values: list[Decimal]) -> Decimal | None:
    return None if not values else sum(values, Decimal("0")) / Decimal(len(values))


def _nearest_rank(values: list[Decimal], quantile: Decimal) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(
        1,
        int(
            (quantile * Decimal(len(ordered))).to_integral_value(
                rounding="ROUND_CEILING"
            )
        ),
    )
    return ordered[rank - 1]


__all__ = [
    "ContextForecastStabilityAssignment",
    "ContextForecastStabilityPreallocator",
    "ContextForecastStabilityReport",
    "ContextForecastStabilityResult",
    "ContextForecastStabilityRunner",
    "ContextForecastStabilityStatus",
    "ContextForecastStabilityTarget",
    "SqlContextForecastStabilityRepository",
    "assemble_context_forecast_stability_preallocator",
    "assemble_context_forecast_stability_runner",
    "build_context_forecast_stability_assignment",
    "evaluate_context_forecast_stability",
    "preregister_context_forecast_stability",
]
