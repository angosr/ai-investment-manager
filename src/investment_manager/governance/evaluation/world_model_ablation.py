"""Prospective paired test of whether WorldModel semantics improve forecasts."""

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
from sqlalchemy import and_, func, insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.forecast.codex.bundle import (
    load_existing_bundle,
    write_run_bundle,
)
from investment_manager.forecast.codex.output import strict_output_schema
from investment_manager.forecast.codex.protocol import codex_execution_contract
from investment_manager.forecast.codex.repository import (
    SqlAccountLeaseStore,
    SqlCodexAuditStore,
)
from investment_manager.forecast.codex.router import (
    AnalystResult,
    CodexAccountRouter,
    assemble_codex_router,
)
from investment_manager.forecast.context.estimate import (
    ContextForecastProbabilityDraft,
)
from investment_manager.forecast.context.evaluation import (
    multiclass_brier_score,
    select_non_overlapping_intervals,
)
from investment_manager.forecast.contracts import ForecastContract, ForecastDecisionSlot
from investment_manager.forecast.results import (
    BaseForecast,
    ForecastOutcome,
    ForecastOutcomeStatus,
)
from investment_manager.forecast.tables import (
    forecast_decision_slots,
    forecast_no_estimates,
    forecast_outcomes,
    forecasts,
)
from investment_manager.governance.evaluation.statistics import (
    conservative_newey_west_lower_bound,
)
from investment_manager.governance.models import (
    EvaluationPlan,
    EvaluationStage,
    ReleaseManifest,
)
from investment_manager.governance.policy import WorldModelAblationPolicy
from investment_manager.governance.repository import SqlGovernanceRepository
from investment_manager.governance.tables import (
    world_model_ablation_assignments,
    world_model_ablation_results,
)
from investment_manager.kernel.identity import canonical_json, content_hash, stable_id
from investment_manager.kernel.time import optional_utc, require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.settings import AppConfig

CONTROL_INPUT_VERSION = "context-forecast-no-world-model-v1"
CONTROL_OUTPUT_VERSION = "context-forecast-no-world-model-output-v2"
LEGACY_CONTROL_CALL_ORDER = "FORMAL_FIRST_CAPITAL_PRIORITY"
CONTROL_CALL_ORDER = "PREASSIGNED_INDEPENDENT_WORKERS"
SAMPLE_SELECTION_RULE = "GREEDY_NON_OVERLAPPING_INFORMATION_CUTOFF_TO_EVALUATION_V1"
UNCERTAINTY_METHOD = "NEWEY_WEST_LAG_1_ON_NON_OVERLAPPING_V1"
_MAXIMUM_MULTICLASS_BRIER_SCORE = Decimal("2")
CONTROL_INSTRUCTIONS = (
    "你是概率预测员。输入只包含一份预登记预测合同和目标相关的点时市场状态。",
    "只根据输入估计合同终点收益落入各 bucket 的概率，不得使用外部信息，不得输出订单、仓位、"
    "杠杆、收益点数、止损、风险预算或交易建议。",
    "outcome_probabilities 必须按合同 bucket_id 和顺序完整输出，概率为 0 到 1 的十进制字符串且"
    "总和精确等于 1；不确定时扩大中间和尾部概率，不得拒绝预测。",
    "输出只包含 decision_slot_id 和 outcome_probabilities；不得编造世界模型引用、解释或失效条件。",
)


class WorldModelControlForecastDraft(FrozenModel):
    """Probability-only control with no semantic WorldModel references."""

    decision_slot_id: str = Field(min_length=1)
    outcome_probabilities: tuple[ContextForecastProbabilityDraft, ...] = Field(min_length=3)


class WorldModelControlStructuredOutput(FrozenModel):
    forecast: WorldModelControlForecastDraft


def world_model_control_output_schema_for_ids(
    *,
    decision_slot_id: str,
    bucket_ids: tuple[str, ...],
) -> dict[str, object]:
    if not bucket_ids or len(set(bucket_ids)) != len(bucket_ids):
        raise ValueError("WorldModel control bucket_id 不能为空或重复")
    schema = strict_output_schema(WorldModelControlStructuredOutput.model_json_schema())
    definitions = schema["$defs"]
    draft = definitions["WorldModelControlForecastDraft"]
    draft["properties"]["decision_slot_id"]["const"] = decision_slot_id
    probability = definitions["ContextForecastProbabilityDraft"]
    probability["properties"]["bucket_id"]["enum"] = list(bucket_ids)
    return schema


class WorldModelAblationStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class WorldModelAblationAssignment(FrozenModel):
    assignment_id: str
    plan_id: str
    formal_forecast_id: str
    decision_slot_id: str
    contract_id: str
    formal_producer_behavior_id: str
    control_behavior_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    information_cutoff_at: datetime
    formal_available_at: datetime | None = None
    assigned_at: datetime
    completion_deadline_at: datetime
    evaluation_at: datetime
    call_order: str
    formal_analysis_input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    control_input_json: str
    control_input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_schema_json: str
    output_schema_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    _utc_information_cutoff_at = field_validator("information_cutoff_at")(require_utc)
    _utc_formal_available_at = field_validator("formal_available_at")(optional_utc)
    _utc_assigned_at = field_validator("assigned_at")(require_utc)
    _utc_completion_deadline_at = field_validator("completion_deadline_at")(require_utc)
    _utc_evaluation_at = field_validator("evaluation_at")(require_utc)

    @model_validator(mode="after")
    def identities_inputs_and_timing_are_frozen(self):
        if self.call_order not in {CONTROL_CALL_ORDER, LEGACY_CONTROL_CALL_ORDER}:
            raise ValueError("WorldModel control 调用安排不受支持")
        if self.call_order == LEGACY_CONTROL_CALL_ORDER:
            if self.formal_available_at is None or not (
                self.information_cutoff_at <= self.formal_available_at <= self.assigned_at
            ):
                raise ValueError("旧 WorldModel control 时间顺序非法")
        elif self.formal_available_at is not None or not (
            self.information_cutoff_at <= self.assigned_at < self.completion_deadline_at
        ):
            raise ValueError("WorldModel control 必须在正式调用前且共同截止前分配")
        if not self.information_cutoff_at < self.completion_deadline_at < self.evaluation_at:
            raise ValueError("WorldModel control 截止时间非法")
        control_input = _canonical_payload(self.control_input_json, "control input")
        if "world_model" in control_input:
            raise ValueError("WorldModel control 输入不得包含 world_model")
        if set(control_input) != {
            "purpose",
            "decision_slot",
            "forecast_contract",
            "target_state",
        }:
            raise ValueError("WorldModel control 输入边界不完整")
        output_schema = _canonical_payload(self.output_schema_json, "output schema")
        if content_hash(control_input) != self.control_input_hash:
            raise ValueError("WorldModel control 输入哈希不一致")
        if content_hash(output_schema) != self.output_schema_hash:
            raise ValueError("WorldModel control schema 哈希不一致")
        expected_id = stable_id(
            "world_model_ablation_assignment",
            self.plan_id,
            self.formal_forecast_id,
        )
        if self.assignment_id != expected_id:
            raise ValueError("WorldModel control assignment_id 不一致")
        identity = self.model_dump(
            mode="json",
            exclude={"source_hash"},
        )
        if content_hash(identity) != self.source_hash:
            raise ValueError("WorldModel control assignment source_hash 不一致")
        return self


class WorldModelAblationResult(FrozenModel):
    result_id: str
    assignment_id: str
    status: WorldModelAblationStatus
    completed_at: datetime
    reason_code: str
    account_id: str | None = None
    attempts: int = Field(default=0, ge=0)
    usage: tuple[tuple[str, int], ...] = ()
    codex_run_id: str | None = None
    output_json: str | None = None
    output_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    _utc_completed_at = field_validator("completed_at")(require_utc)

    @model_validator(mode="after")
    def completion_is_atomic_and_canonical(self):
        succeeded = self.status == WorldModelAblationStatus.SUCCEEDED
        if succeeded != (self.output_json is not None and self.output_hash is not None):
            raise ValueError("WorldModel control 成功状态与输出不一致")
        if tuple(sorted(set(self.usage))) != self.usage:
            raise ValueError("WorldModel control usage 必须唯一且排序")
        if self.output_json is not None:
            output = _canonical_payload(self.output_json, "control output")
            if content_hash(output) != self.output_hash:
                raise ValueError("WorldModel control 输出哈希不一致")
            WorldModelControlStructuredOutput.model_validate(output)
        expected_id = stable_id("world_model_ablation_result", self.assignment_id)
        if self.result_id != expected_id:
            raise ValueError("WorldModel control result_id 不一致")
        return self


class WorldModelAblationReport(FrozenModel):
    plan_id: str
    as_of: datetime
    formal_forecast_count: int = Field(ge=0)
    formal_no_estimate_count: int = Field(ge=0)
    assignments: int = Field(ge=0)
    pending_controls: int = Field(ge=0)
    successful_controls: int = Field(ge=0)
    failed_controls: int = Field(ge=0)
    settled_pairs: int = Field(ge=0)
    conservative_sample_count: int = Field(ge=0)
    formal_mean_brier: Decimal | None = None
    control_mean_brier: Decimal | None = None
    mean_brier_improvement: Decimal | None = None
    conservative_mean_brier_improvement: Decimal | None = None
    conservative_improvement_lower_bound: Decimal | None = None
    minimum_sample_size: int = Field(ge=2)
    evidence_sufficient: bool

    _utc_as_of = field_validator("as_of")(require_utc)


@dataclass(frozen=True, slots=True)
class _AblationScoreCase:
    identity: str
    information_cutoff_at: datetime
    evaluation_at: datetime
    conservative_improvement: Decimal
    formal_score: Decimal | None = None
    control_score: Decimal | None = None


def world_model_ablation_behavior_hash(
    *,
    config: AppConfig,
    contract: ForecastContract,
) -> str:
    context = config.capital.context_forecast
    if context is None:
        raise ValueError("WorldModel control 缺少正式 Context Forecast")
    return content_hash(
        {
            "input_version": CONTROL_INPUT_VERSION,
            "output_version": CONTROL_OUTPUT_VERSION,
            "instructions": CONTROL_INSTRUCTIONS,
            "output_schema": strict_output_schema(
                WorldModelControlStructuredOutput.model_json_schema()
            ),
            "formal_contract": contract,
            "formal_producer_behavior_id": context.producer_behavior_id,
            "execution_contract": codex_execution_contract(),
            "runtime_policy_version": config.codex_runtime.version,
            "expected_cli_version": config.codex_runtime.expected_cli_version,
            "expected_binary_sha256": config.codex_runtime.expected_binary_sha256,
            "model": config.codex_runtime.model,
            "reasoning_effort": config.codex_runtime.reasoning_effort,
            "maximum_schema_attempts": 1 + config.codex_runtime.max_account_switches,
            "call_order": CONTROL_CALL_ORDER,
        }
    )


def ensure_world_model_ablation_plan(
    *,
    governance: SqlGovernanceRepository,
    config: AppConfig,
    contract: ForecastContract,
    release: ReleaseManifest,
    registered_at: datetime,
) -> EvaluationPlan:
    policy = _enabled_policy(config)
    now = require_utc(registered_at)
    behavior_hash = world_model_ablation_behavior_hash(config=config, contract=contract)
    context = config.capital.context_forecast
    assert context is not None
    spec: dict[str, object] = {
        "version": policy.version,
        "activated_at": policy.activated_at.isoformat().replace("+00:00", "Z"),
        "formal_contract_id": contract.contract_id,
        "formal_producer_behavior_id": context.producer_behavior_id,
        "control_behavior_hash": behavior_hash,
        "assignment_rule": "ALL_INPUT_READY_SLOTS_BEFORE_FORMAL_CALL_AT_OR_AFTER_ACTIVATION",
        "formal_missing_rule": "COUNT_AUTHORITATIVE_FORECAST_NO_ESTIMATE_TERMINALS",
        "missing_score_rule": (
            "LOWER_BOUND_MISSING_CONTROL_AS_PERFECT_AND_FORMAL_NO_ESTIMATE_AS_WORST"
        ),
        "permission_rule": "ALL_SETTLED_TERMINALS_ENTER_CONSERVATIVE_SKILL_BOUND",
        "sample_selection": SAMPLE_SELECTION_RULE,
        "uncertainty_method": UNCERTAINTY_METHOD,
        "call_order": CONTROL_CALL_ORDER,
        "input_difference": "REMOVE_WORLD_MODEL_SEMANTIC_CONTENT_ONLY",
        "outcome_join": config.outcome_evaluation.target_forecast_version,
        "minimum_sample_size": policy.minimum_sample_size,
    }
    candidate_hash = content_hash(spec)
    expected = EvaluationPlan(
        plan_id=policy.plan_id,
        registered_at=now,
        base_manifest_id=release.manifest_id,
        primary_metric="PAIRED_BRIER_CONTROL_MINUS_FORMAL",
        minimum_sample_size=policy.minimum_sample_size,
        hard_guardrails=(
            "CONTROL_NEVER_ENTERS_CAPITAL",
            "ASSIGNMENT_PRECEDES_BOTH_CALLS",
            "INDEPENDENT_WORKERS_SHARE_SLOT_DEADLINE",
            "NO_HISTORICAL_BACKFILL",
            "OVERLAPPING_OUTCOME_WINDOWS_COUNT_ONCE",
            "SAME_SLOT_STATE_CONTRACT_AND_MODEL",
            "CONTROL_OUTPUT_PROBABILITY_ONLY",
        ),
        required_stages=(EvaluationStage.FORWARD,),
        fixed_regression_suite_version=policy.version,
        candidate_spec_hash=candidate_hash,
        candidate_spec_snapshot=spec,
    )
    existing = governance.get_plan(policy.plan_id)
    if existing is not None:
        _validate_existing_plan(
            existing,
            expected=expected,
            candidate_hash=candidate_hash,
            activated_at=policy.activated_at,
        )
        return existing
    if now >= policy.activated_at:
        raise ValueError("WorldModel control 必须在首个前向时点前登记")
    try:
        governance.register_plan(expected)
        return expected
    except ValueError:
        concurrent = governance.get_plan(policy.plan_id)
        if concurrent is None:
            raise
        _validate_existing_plan(
            concurrent,
            expected=expected,
            candidate_hash=candidate_hash,
            activated_at=policy.activated_at,
        )
        return concurrent


def build_world_model_ablation_assignment(
    *,
    plan: EvaluationPlan,
    slot: ForecastDecisionSlot,
    formal_producer_behavior_id: str,
    formal_analysis_input: dict[str, object],
    assigned_at: datetime,
) -> WorldModelAblationAssignment:
    spec = plan.candidate_spec_snapshot
    if not isinstance(spec, dict):
        raise ValueError("WorldModel control plan 缺少冻结规格")
    if formal_producer_behavior_id != spec.get(
        "formal_producer_behavior_id"
    ) or slot.contract_id != spec.get("formal_contract_id"):
        raise ValueError("WorldModel control 正式行为不属于预登记 cohort")
    raw = _canonical_payload(canonical_json(formal_analysis_input), "formal input")
    world_model = raw.get("world_model")
    if not isinstance(world_model, dict):
        raise ValueError("WorldModel control 正式输入缺少 world_model")
    _validate_world_model_reference_ids(world_model)
    forecast_contract = raw.get("forecast_contract")
    decision_slot = raw.get("decision_slot")
    target_state = raw.get("target_state")
    if not all(isinstance(item, dict) for item in (forecast_contract, decision_slot, target_state)):
        raise ValueError("WorldModel control 正式输入结构不完整")
    if decision_slot.get("decision_slot_id") != slot.slot_id:
        raise ValueError("WorldModel control 正式输入绑定了错误 slot")
    parsed_contract = ForecastContract.model_validate(forecast_contract)
    if parsed_contract.contract_id != slot.contract_id:
        raise ValueError("WorldModel control 输入合同与 slot 不一致")
    bucket_ids = tuple(item.bucket_id for item in parsed_contract.outcome_buckets)
    schema = world_model_control_output_schema_for_ids(
        decision_slot_id=slot.slot_id,
        bucket_ids=bucket_ids,
    )
    control_input = {
        "purpose": "FORECAST_ESTIMATE",
        "decision_slot": decision_slot,
        "forecast_contract": forecast_contract,
        "target_state": target_state,
    }
    control_input_json = canonical_json(control_input)
    output_schema_json = canonical_json(schema)
    behavior_hash = str(spec["control_behavior_hash"])
    formal_forecast_id = stable_id(
        "base_forecast",
        slot.slot_id,
        formal_producer_behavior_id,
    )
    values = {
        "assignment_id": stable_id(
            "world_model_ablation_assignment",
            plan.plan_id,
            formal_forecast_id,
        ),
        "plan_id": plan.plan_id,
        "formal_forecast_id": formal_forecast_id,
        "decision_slot_id": slot.slot_id,
        "contract_id": slot.contract_id,
        "formal_producer_behavior_id": formal_producer_behavior_id,
        "control_behavior_hash": behavior_hash,
        "information_cutoff_at": slot.information_cutoff_at,
        "formal_available_at": None,
        "assigned_at": require_utc(assigned_at),
        "completion_deadline_at": slot.completion_deadline_at,
        "evaluation_at": slot.evaluation_at,
        "call_order": CONTROL_CALL_ORDER,
        "formal_analysis_input_hash": content_hash(raw),
        "control_input_json": control_input_json,
        "control_input_hash": content_hash(control_input),
        "output_schema_json": output_schema_json,
        "output_schema_hash": content_hash(schema),
    }
    values["source_hash"] = content_hash(values)
    return WorldModelAblationAssignment.model_validate(values)


class WorldModelControlAnalyst(Protocol):
    def estimate(self, assignment: WorldModelAblationAssignment) -> AnalystResult: ...


@dataclass(slots=True)
class CodexWorldModelControlAnalyst:
    bundle_root: Path
    maximum_prompt_characters: int
    router: CodexAccountRouter

    def estimate(self, assignment: WorldModelAblationAssignment) -> AnalystResult:
        target = self.bundle_root / stable_id(
            "world_model_ablation_bundle",
            assignment.assignment_id,
            assignment.control_behavior_hash,
        )
        prompt = "\n".join(
            (
                *CONTROL_INSTRUCTIONS,
                "context_forecast_input_json=",
                assignment.control_input_json,
            )
        )
        if len(prompt) > self.maximum_prompt_characters:
            return AnalystResult(False, None, "FORECAST_INPUT_TOO_LARGE")
        try:
            bundle = load_existing_bundle(
                cycle_id=assignment.assignment_id,
                target=target,
                expected_manifest={
                    "analysis_mode": "WORLD_MODEL_ABLATION_CONTROL",
                    "assignment_id": assignment.assignment_id,
                    "analysis_behavior_hash": assignment.control_behavior_hash,
                },
            )
            if bundle is None:
                bundle = write_run_bundle(
                    cycle_id=assignment.assignment_id,
                    target=target,
                    prompt=prompt,
                    files={
                        "context_forecast_input.json": assignment.control_input_json + "\n",
                        "analyst_prompt.md": prompt + "\n",
                        "output.schema.json": assignment.output_schema_json + "\n",
                    },
                    manifest={
                        "analysis_mode": "WORLD_MODEL_ABLATION_CONTROL",
                        "input_version": CONTROL_INPUT_VERSION,
                        "output_version": CONTROL_OUTPUT_VERSION,
                        "assignment_id": assignment.assignment_id,
                        "decision_slot_id": assignment.decision_slot_id,
                        "plan_id": assignment.plan_id,
                        "call_order": assignment.call_order,
                        "analysis_behavior_hash": assignment.control_behavior_hash,
                        "output_schema_hash": assignment.output_schema_hash,
                    },
                )
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return AnalystResult(False, None, "CODEX_BUNDLE_INVALID")
        return self.router.run(bundle)


class SqlWorldModelAblationRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record_assignment(self, assignment: WorldModelAblationAssignment) -> bool:
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(world_model_ablation_assignments).values(
                        assignment_id=assignment.assignment_id,
                        plan_id=assignment.plan_id,
                        formal_forecast_id=assignment.formal_forecast_id,
                        decision_slot_id=assignment.decision_slot_id,
                        assigned_at=assignment.assigned_at,
                        completion_deadline_at=assignment.completion_deadline_at,
                        evaluation_at=assignment.evaluation_at,
                        control_behavior_hash=assignment.control_behavior_hash,
                        source_hash=assignment.source_hash,
                        payload=assignment.model_dump(mode="json"),
                    )
                )
            return True
        except IntegrityError:
            existing = self.assignment(assignment.assignment_id)
            if existing != assignment:
                raise ValueError("WorldModel control assignment 已存在且内容不同") from None
            return False

    def assignment(self, assignment_id: str) -> WorldModelAblationAssignment | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(world_model_ablation_assignments.c.payload).where(
                    world_model_ablation_assignments.c.assignment_id == assignment_id
                )
            ).scalar_one_or_none()
        return None if payload is None else WorldModelAblationAssignment.model_validate(payload)

    def pending_assignments(
        self,
        *,
        plan_id: str,
        limit: int,
    ) -> tuple[WorldModelAblationAssignment, ...]:
        joined = world_model_ablation_assignments.outerjoin(
            world_model_ablation_results,
            world_model_ablation_results.c.assignment_id
            == world_model_ablation_assignments.c.assignment_id,
        )
        with self._engine.connect() as connection:
            payloads = connection.execute(
                select(world_model_ablation_assignments.c.payload)
                .select_from(joined)
                .where(
                    world_model_ablation_assignments.c.plan_id == plan_id,
                    world_model_ablation_results.c.result_id.is_(None),
                )
                .order_by(
                    world_model_ablation_assignments.c.assigned_at,
                    world_model_ablation_assignments.c.assignment_id,
                )
                .limit(limit)
            ).scalars()
            return tuple(WorldModelAblationAssignment.model_validate(item) for item in payloads)

    def record_result(self, result: WorldModelAblationResult) -> bool:
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(world_model_ablation_results).values(
                        result_id=result.result_id,
                        assignment_id=result.assignment_id,
                        status=result.status.value,
                        completed_at=result.completed_at,
                        payload=result.model_dump(mode="json"),
                    )
                )
            return True
        except IntegrityError:
            existing = self.result(result.assignment_id)
            if existing != result:
                raise ValueError("WorldModel control result 已存在且内容不同") from None
            return False

    def result(self, assignment_id: str) -> WorldModelAblationResult | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(world_model_ablation_results.c.payload).where(
                    world_model_ablation_results.c.assignment_id == assignment_id
                )
            ).scalar_one_or_none()
        return None if payload is None else WorldModelAblationResult.model_validate(payload)

    def report(
        self,
        *,
        plan_id: str,
        evaluation_version: str,
        minimum_sample_size: int,
        formal_producer_behavior_id: str,
        activated_at: datetime,
        as_of: datetime,
    ) -> WorldModelAblationReport:
        plan = SqlGovernanceRepository(self._engine).get_plan(plan_id)
        if plan is None or not isinstance(plan.candidate_spec_snapshot, dict):
            raise ValueError("WorldModel control report 缺少预登记计划")
        spec = plan.candidate_spec_snapshot
        formal_contract_id = spec.get("formal_contract_id")
        if not isinstance(formal_contract_id, str) or not formal_contract_id:
            raise ValueError("WorldModel control plan 缺少冻结正式合同")
        activated_at_raw = spec.get("activated_at")
        if not isinstance(activated_at_raw, str):
            raise ValueError("WorldModel control plan 缺少冻结激活时间")
        try:
            plan_activated_at = require_utc(
                datetime.fromisoformat(activated_at_raw.replace("Z", "+00:00"))
            )
        except ValueError as exc:
            raise ValueError("WorldModel control plan 激活时间非法") from exc
        if (
            spec.get("formal_producer_behavior_id") != formal_producer_behavior_id
            or spec.get("sample_selection") != SAMPLE_SELECTION_RULE
            or spec.get("uncertainty_method") != UNCERTAINTY_METHOD
            or spec.get("outcome_join") != evaluation_version
            or spec.get("minimum_sample_size") != minimum_sample_size
            or plan_activated_at != require_utc(activated_at)
        ):
            raise ValueError("WorldModel control report 与预登记评价语义不一致")
        outcome_join = and_(
            forecast_outcomes.c.decision_slot_id
            == world_model_ablation_assignments.c.decision_slot_id,
            forecast_outcomes.c.evaluation_version == evaluation_version,
        )
        joined = (
            world_model_ablation_assignments.outerjoin(
                forecasts,
                forecasts.c.forecast_id == world_model_ablation_assignments.c.formal_forecast_id,
            )
            .outerjoin(
                world_model_ablation_results,
                world_model_ablation_results.c.assignment_id
                == world_model_ablation_assignments.c.assignment_id,
            )
            .outerjoin(forecast_outcomes, outcome_join)
        )
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(
                    world_model_ablation_assignments.c.payload,
                    world_model_ablation_results.c.payload,
                    forecasts.c.payload,
                    forecast_outcomes.c.payload,
                )
                .select_from(joined)
                .where(world_model_ablation_assignments.c.plan_id == plan_id)
                .order_by(world_model_ablation_assignments.c.evaluation_at)
            ).all()
            formal_forecast_count = connection.execute(
                select(func.count())
                .select_from(
                    forecasts.join(
                        forecast_decision_slots,
                        forecast_decision_slots.c.slot_id == forecasts.c.decision_slot_id,
                    )
                )
                .where(
                    forecasts.c.kind == "BASE",
                    forecasts.c.contract_id == formal_contract_id,
                    forecasts.c.producer_behavior_id == formal_producer_behavior_id,
                    forecast_decision_slots.c.information_cutoff_at >= require_utc(activated_at),
                )
            ).scalar_one()
            no_estimate_rows = connection.execute(
                select(
                    forecast_no_estimates.c.result_id,
                    forecast_decision_slots.c.evaluation_at,
                    forecast_outcomes.c.payload,
                )
                .select_from(
                    forecast_no_estimates.join(
                        forecast_decision_slots,
                        forecast_decision_slots.c.slot_id == forecast_no_estimates.c.slot_id,
                    ).outerjoin(
                        forecast_outcomes,
                        and_(
                            forecast_outcomes.c.decision_slot_id == forecast_no_estimates.c.slot_id,
                            forecast_outcomes.c.evaluation_version == evaluation_version,
                        ),
                    )
                )
                .where(
                    forecast_no_estimates.c.contract_id == formal_contract_id,
                    forecast_no_estimates.c.producer_behavior_id == formal_producer_behavior_id,
                    forecast_decision_slots.c.information_cutoff_at >= require_utc(activated_at),
                )
            ).all()
            formal_no_estimate_count = len(no_estimate_rows)
        current = require_utc(as_of)
        pending = 0
        failed = 0
        succeeded = 0
        settled_pairs = 0
        score_cases: list[_AblationScoreCase] = []
        for assignment_raw, result_raw, formal_raw, outcome_raw in rows:
            assignment = WorldModelAblationAssignment.model_validate(assignment_raw)
            outcome = None if outcome_raw is None else ForecastOutcome.model_validate(outcome_raw)
            settled_outcome = (
                outcome
                if outcome is not None and outcome.status == ForecastOutcomeStatus.SETTLED
                else None
            )
            formal = None if formal_raw is None else BaseForecast.model_validate(formal_raw)
            formal_score = None
            if settled_outcome is not None and formal is not None:
                assert settled_outcome.realized_bucket_id is not None
                formal_score = multiclass_brier_score(
                    tuple(
                        (item.bucket_id, item.probability) for item in formal.outcome_probabilities
                    ),
                    settled_outcome.realized_bucket_id,
                )
            if result_raw is None:
                if current <= assignment.completion_deadline_at:
                    pending += 1
                else:
                    failed += 1
                    if formal_score is not None:
                        score_cases.append(
                            _AblationScoreCase(
                                identity=assignment.assignment_id,
                                information_cutoff_at=assignment.information_cutoff_at,
                                evaluation_at=assignment.evaluation_at,
                                conservative_improvement=-formal_score,
                            )
                        )
                continue
            result = WorldModelAblationResult.model_validate(result_raw)
            if result.status != WorldModelAblationStatus.SUCCEEDED:
                failed += 1
                if formal_score is not None:
                    score_cases.append(
                        _AblationScoreCase(
                            identity=assignment.assignment_id,
                            information_cutoff_at=assignment.information_cutoff_at,
                            evaluation_at=assignment.evaluation_at,
                            conservative_improvement=-formal_score,
                        )
                    )
                continue
            succeeded += 1
            if settled_outcome is None or formal is None:
                continue
            control = WorldModelControlStructuredOutput.model_validate_json(result.output_json)
            assert formal_score is not None
            assert settled_outcome.realized_bucket_id is not None
            control_score = multiclass_brier_score(
                tuple(
                    (item.bucket_id, Decimal(item.probability))
                    for item in control.forecast.outcome_probabilities
                ),
                settled_outcome.realized_bucket_id,
            )
            settled_pairs += 1
            score_cases.append(
                _AblationScoreCase(
                    identity=assignment.assignment_id,
                    information_cutoff_at=assignment.information_cutoff_at,
                    evaluation_at=assignment.evaluation_at,
                    formal_score=formal_score,
                    control_score=control_score,
                    conservative_improvement=control_score - formal_score,
                )
            )
        for result_id, _evaluation_at, outcome_raw in no_estimate_rows:
            if outcome_raw is None:
                continue
            outcome = ForecastOutcome.model_validate(outcome_raw)
            if outcome.status != ForecastOutcomeStatus.SETTLED:
                continue
            # The formal side supplied no distribution.  For a lower bound on
            # WorldModel skill, assume the absent formal forecast was maximally
            # wrong and the unobserved control was perfect.  This is the exact
            # multiclass Brier range, not an operational failure-rate threshold.
            score_cases.append(
                _AblationScoreCase(
                    identity=result_id,
                    information_cutoff_at=outcome.information_cutoff_at,
                    evaluation_at=outcome.evaluation_at,
                    conservative_improvement=-_MAXIMUM_MULTICLASS_BRIER_SCORE,
                )
            )
        independent = select_non_overlapping_intervals(
            tuple(score_cases),
            identity=lambda item: item.identity,
            information_cutoff_at=lambda item: item.information_cutoff_at,
            evaluation_at=lambda item: item.evaluation_at,
            stratum=lambda _item: plan_id,
        )
        paired = tuple(
            item
            for item in independent
            if item.formal_score is not None and item.control_score is not None
        )
        resolved_pairs: list[tuple[Decimal, Decimal]] = []
        for item in paired:
            assert item.formal_score is not None
            assert item.control_score is not None
            resolved_pairs.append((item.formal_score, item.control_score))
        resolved_formal_scores = [formal for formal, _control in resolved_pairs]
        resolved_control_scores = [control for _formal, control in resolved_pairs]
        improvements = [
            control - formal
            for formal, control in zip(
                resolved_formal_scores,
                resolved_control_scores,
                strict=True,
            )
        ]
        formal_mean = _mean(resolved_formal_scores)
        control_mean = _mean(resolved_control_scores)
        improvement_mean = _mean(improvements)
        conservative_values = tuple(item.conservative_improvement for item in independent)
        conservative_mean = _mean(list(conservative_values))
        lower_bound = (
            None
            if len(conservative_values) < 2
            else conservative_newey_west_lower_bound(
                conservative_values,
                z=Decimal("1.96"),
                lag=1,
            )
        )
        return WorldModelAblationReport(
            plan_id=plan_id,
            as_of=current,
            formal_forecast_count=formal_forecast_count,
            formal_no_estimate_count=formal_no_estimate_count,
            assignments=len(rows),
            pending_controls=pending,
            successful_controls=succeeded,
            failed_controls=failed,
            settled_pairs=settled_pairs,
            conservative_sample_count=len(conservative_values),
            formal_mean_brier=formal_mean,
            control_mean_brier=control_mean,
            mean_brier_improvement=improvement_mean,
            conservative_mean_brier_improvement=conservative_mean,
            conservative_improvement_lower_bound=lower_bound,
            minimum_sample_size=minimum_sample_size,
            evidence_sufficient=(
                len(conservative_values) >= minimum_sample_size
                and lower_bound is not None
                and lower_bound > 0
            ),
        )


@dataclass(slots=True)
class WorldModelAblationRunner:
    policy: WorldModelAblationPolicy
    plan: EvaluationPlan
    formal_producer_behavior_id: str
    evaluation_version: str
    repository: SqlWorldModelAblationRepository
    analyst: WorldModelControlAnalyst

    def reconcile(self, *, as_of: datetime) -> WorldModelAblationReport:
        now = require_utc(as_of)
        for assignment in self.repository.pending_assignments(
            plan_id=self.plan.plan_id,
            limit=self.policy.maximum_batch_size,
        ):
            if now > assignment.completion_deadline_at:
                result = _failed_result(
                    assignment,
                    completed_at=now,
                    reason_code="CONTROL_DEADLINE_MISSED",
                )
            else:
                result = _result_from_analyst(
                    assignment,
                    self.analyst.estimate(assignment),
                    fallback_completed_at=now,
                )
            self.repository.record_result(result)
        return self.repository.report(
            plan_id=self.plan.plan_id,
            evaluation_version=self.evaluation_version,
            minimum_sample_size=self.policy.minimum_sample_size,
            formal_producer_behavior_id=self.formal_producer_behavior_id,
            activated_at=self.policy.activated_at,
            as_of=now,
        )


@dataclass(slots=True)
class WorldModelAblationPreallocator:
    """Freeze the pair before the formal producer is allowed to call Codex."""

    policy: WorldModelAblationPolicy
    plan: EvaluationPlan
    formal_producer_behavior_id: str
    repository: SqlWorldModelAblationRepository
    clock: Callable[[], datetime]

    def before_estimate(
        self,
        *,
        slot: ForecastDecisionSlot,
        formal_producer_behavior_id: str,
        formal_analysis_input: dict[str, object],
    ) -> None:
        if slot.information_cutoff_at < self.policy.activated_at:
            return
        if formal_producer_behavior_id != self.formal_producer_behavior_id:
            raise ValueError("WorldModel control preflight 绑定了错误正式行为")
        formal_forecast_id = stable_id(
            "base_forecast",
            slot.slot_id,
            formal_producer_behavior_id,
        )
        assignment_id = stable_id(
            "world_model_ablation_assignment",
            self.plan.plan_id,
            formal_forecast_id,
        )
        existing = self.repository.assignment(assignment_id)
        if existing is not None:
            expected = build_world_model_ablation_assignment(
                plan=self.plan,
                slot=slot,
                formal_producer_behavior_id=formal_producer_behavior_id,
                formal_analysis_input=formal_analysis_input,
                assigned_at=existing.assigned_at,
            )
            if existing != expected:
                raise ValueError("WorldModel control 重试绑定了不同输入")
            return
        assigned_at = require_utc(self.clock())
        if assigned_at >= slot.completion_deadline_at:
            return
        assignment = build_world_model_ablation_assignment(
            plan=self.plan,
            slot=slot,
            formal_producer_behavior_id=formal_producer_behavior_id,
            formal_analysis_input=formal_analysis_input,
            assigned_at=assigned_at,
        )
        self.repository.record_assignment(assignment)


def assemble_world_model_ablation_preallocator(
    config: AppConfig,
    *,
    engine: Engine,
    release: ReleaseManifest,
    contract: ForecastContract,
    clock: Callable[[], datetime],
) -> WorldModelAblationPreallocator | None:
    policy = config.outcome_evaluation.world_model_ablation
    context = config.capital.context_forecast
    if policy is None or not policy.enabled:
        return None
    if context is None or not context.enabled:
        raise ValueError("启用 WorldModel control 必须绑定 Context Forecast")
    plan = ensure_world_model_ablation_plan(
        governance=SqlGovernanceRepository(engine),
        config=config,
        contract=contract,
        release=release,
        registered_at=clock(),
    )
    return WorldModelAblationPreallocator(
        policy=policy,
        plan=plan,
        formal_producer_behavior_id=context.producer_behavior_id,
        repository=SqlWorldModelAblationRepository(engine),
        clock=clock,
    )


def assemble_world_model_ablation_analyst(
    config: AppConfig,
    *,
    engine: Engine,
) -> CodexWorldModelControlAnalyst:
    router = assemble_codex_router(
        config,
        leases=SqlAccountLeaseStore(engine),
        audit=SqlCodexAuditStore(engine),
        output_adapter=TypeAdapter(WorldModelControlStructuredOutput),
    )
    return CodexWorldModelControlAnalyst(
        bundle_root=config.codex_runtime.bundle_root,
        maximum_prompt_characters=config.codex_runtime.maximum_prompt_characters,
        router=router,
    )


def _enabled_policy(config: AppConfig) -> WorldModelAblationPolicy:
    policy = config.outcome_evaluation.world_model_ablation
    if policy is None or not policy.enabled:
        raise ValueError("WorldModel control 未启用")
    return policy


def _validate_existing_plan(
    existing: EvaluationPlan,
    *,
    expected: EvaluationPlan,
    candidate_hash: str,
    activated_at: datetime,
) -> None:
    if (
        existing.candidate_spec_hash != candidate_hash
        or existing.primary_metric != expected.primary_metric
        or existing.minimum_sample_size != expected.minimum_sample_size
        or existing.hard_guardrails != expected.hard_guardrails
        or existing.required_stages != expected.required_stages
    ):
        raise ValueError("已登记 WorldModel control plan 与当前行为不一致")
    if existing.registered_at >= activated_at:
        raise ValueError("WorldModel control plan 未在激活前登记")


def _validate_world_model_reference_ids(
    world_model: dict[str, object],
) -> None:
    raw_mechanisms = world_model.get("mechanisms")
    raw_events = world_model.get("event_references")
    if not isinstance(raw_mechanisms, list) or not isinstance(raw_events, list):
        raise ValueError("WorldModel control 无法冻结输出标识符")
    mechanism_ids: list[str] = []
    evidence_ids: set[str] = set()
    for raw in raw_events:
        if isinstance(raw, dict) and isinstance(raw.get("evidence_id"), str):
            evidence_ids.add(raw["evidence_id"])
    for raw in raw_mechanisms:
        if not isinstance(raw, dict) or not isinstance(raw.get("mechanism_id"), str):
            raise ValueError("WorldModel mechanism_id 非法")
        mechanism_ids.append(raw["mechanism_id"])
        for evidence_id in raw.get("conflicting_evidence_ids", []):
            if isinstance(evidence_id, str):
                evidence_ids.add(evidence_id)
        for node in raw.get("causal_chain", []):
            if isinstance(node, dict):
                evidence_ids.update(
                    item for item in node.get("evidence_ids", []) if isinstance(item, str)
                )
    canonical_mechanisms = tuple(mechanism_ids)
    canonical_evidence = tuple(sorted(evidence_ids))
    if (
        not canonical_mechanisms
        or len(set(canonical_mechanisms)) != len(canonical_mechanisms)
        or not canonical_evidence
    ):
        raise ValueError("WorldModel control 输出标识符为空或重复")


def _result_from_analyst(
    assignment: WorldModelAblationAssignment,
    analyst: AnalystResult,
    *,
    fallback_completed_at: datetime,
) -> WorldModelAblationResult:
    completed = require_utc(analyst.completed_at or fallback_completed_at)
    common = {
        "result_id": stable_id("world_model_ablation_result", assignment.assignment_id),
        "assignment_id": assignment.assignment_id,
        "completed_at": completed,
        "account_id": analyst.account_id,
        "attempts": analyst.attempts,
        "usage": tuple(sorted((key, int(value)) for key, value in analyst.usage.items())),
        "codex_run_id": analyst.run_id,
    }
    if completed > assignment.completion_deadline_at:
        return WorldModelAblationResult(
            **common,
            status=WorldModelAblationStatus.FAILED,
            reason_code="CONTROL_DEADLINE_MISSED",
        )
    if not analyst.success or not isinstance(
        analyst.output,
        WorldModelControlStructuredOutput,
    ):
        return WorldModelAblationResult(
            **common,
            status=WorldModelAblationStatus.FAILED,
            reason_code=analyst.reason_code,
        )
    draft = analyst.output.forecast
    expected_buckets = tuple(
        item["bucket_id"]
        for item in _canonical_payload(
            assignment.control_input_json,
            "control input",
        )["forecast_contract"]["outcome_buckets"]
    )
    actual_buckets = tuple(item.bucket_id for item in draft.outcome_probabilities)
    probabilities = tuple(Decimal(item.probability) for item in draft.outcome_probabilities)
    output_invalid = (
        draft.decision_slot_id != assignment.decision_slot_id
        or actual_buckets != expected_buckets
        or sum(probabilities, Decimal("0")) != 1
    )
    if output_invalid:
        return WorldModelAblationResult(
            **common,
            status=WorldModelAblationStatus.FAILED,
            reason_code="CONTROL_OUTPUT_CONTRACT_INVALID",
        )
    output = analyst.output.model_dump(mode="json")
    return WorldModelAblationResult(
        **common,
        status=WorldModelAblationStatus.SUCCEEDED,
        reason_code="CONTROL_FORECAST_SUCCEEDED",
        output_json=canonical_json(output),
        output_hash=content_hash(output),
    )


def _failed_result(
    assignment: WorldModelAblationAssignment,
    *,
    completed_at: datetime,
    reason_code: str,
) -> WorldModelAblationResult:
    return WorldModelAblationResult(
        result_id=stable_id("world_model_ablation_result", assignment.assignment_id),
        assignment_id=assignment.assignment_id,
        status=WorldModelAblationStatus.FAILED,
        completed_at=require_utc(completed_at),
        reason_code=reason_code,
    )


def _canonical_payload(raw: str, name: str) -> dict[str, object]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} 不是有效 JSON") from exc
    if not isinstance(payload, dict) or canonical_json(payload) != raw:
        raise ValueError(f"{name} 必须是规范 JSON 对象")
    return payload


def _mean(values: list[Decimal]) -> Decimal | None:
    return None if not values else sum(values, Decimal("0")) / Decimal(len(values))


__all__ = [
    "CodexWorldModelControlAnalyst",
    "SqlWorldModelAblationRepository",
    "WorldModelAblationAssignment",
    "WorldModelAblationPreallocator",
    "WorldModelAblationReport",
    "WorldModelAblationResult",
    "WorldModelAblationRunner",
    "WorldModelAblationStatus",
    "assemble_world_model_ablation_analyst",
    "assemble_world_model_ablation_preallocator",
    "build_world_model_ablation_assignment",
    "ensure_world_model_ablation_plan",
    "world_model_ablation_behavior_hash",
]
