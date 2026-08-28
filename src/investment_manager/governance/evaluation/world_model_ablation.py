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
    context_forecast_runtime,
)
from investment_manager.forecast.context.evaluation import (
    multiclass_brier_score,
    ordinal_ranked_probability_score,
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

CONTROL_INPUT_VERSION = "joint-context-forecast-no-world-model-v2"
CONTROL_OUTPUT_VERSION = "joint-context-forecast-no-world-model-output-v3"
LEGACY_CONTROL_CALL_ORDER = "FORMAL_FIRST_CAPITAL_PRIORITY"
CONTROL_CALL_ORDER = "PREASSIGNED_INDEPENDENT_WORKERS"
SAMPLE_SELECTION_RULE = "GREEDY_NON_OVERLAPPING_INFORMATION_CUTOFF_TO_EVALUATION_V1"
UNCERTAINTY_METHOD = "NEWEY_WEST_LAG_1_ON_NON_OVERLAPPING_V1"
_MAXIMUM_MULTICLASS_BRIER_SCORE = Decimal("2")
_MAXIMUM_RANKED_PROBABILITY_SCORE = Decimal("1")
CONTROL_INSTRUCTIONS = (
    "你是组合概率预测员。输入保留正式调用的全部预登记目标、合同和点时市场状态，只移除了共享世界模型。",
    "必须为每个可见 decision_slot_id 恰好输出一份合同终点收益概率，不得使用外部信息，"
    "不得输出订单、仓位、杠杆、收益点数、止损、风险预算或交易建议。",
    "outcome_probabilities 必须按合同 bucket_id 和顺序完整输出，概率为 0 到 1 的十进制字符串且"
    "总和精确等于 1；不确定时扩大中间和尾部概率，不得拒绝预测。",
    "每份输出只包含 decision_slot_id 和 outcome_probabilities；"
    "不得编造世界模型引用、解释或失效条件。",
)


class WorldModelControlForecastDraft(FrozenModel):
    """Probability-only control with no semantic WorldModel references."""

    decision_slot_id: str = Field(min_length=1)
    outcome_probabilities: tuple[ContextForecastProbabilityDraft, ...] = Field(min_length=3)


class WorldModelControlStructuredOutput(FrozenModel):
    forecasts: tuple[WorldModelControlForecastDraft, ...] = Field(min_length=1)


class _LegacyWorldModelControlStructuredOutput(FrozenModel):
    forecast: WorldModelControlForecastDraft


def world_model_control_output_schema_for_targets(
    *,
    target_buckets: tuple[tuple[str, tuple[str, ...]], ...],
) -> dict[str, object]:
    decision_slot_ids = tuple(item[0] for item in target_buckets)
    if not decision_slot_ids or len(set(decision_slot_ids)) != len(decision_slot_ids):
        raise ValueError("WorldModel control decision_slot_id 不能为空或重复")
    bucket_ids = tuple(sorted({bucket for _, buckets in target_buckets for bucket in buckets}))
    if any(not buckets or len(set(buckets)) != len(buckets) for _, buckets in target_buckets):
        raise ValueError("WorldModel control bucket_id 不能为空或重复")
    schema = strict_output_schema(WorldModelControlStructuredOutput.model_json_schema())
    definitions = schema["$defs"]
    draft = definitions["WorldModelControlForecastDraft"]
    draft["properties"]["decision_slot_id"]["enum"] = list(decision_slot_ids)
    probability = definitions["ContextForecastProbabilityDraft"]
    probability["properties"]["bucket_id"]["enum"] = list(bucket_ids)
    forecasts = schema["properties"]["forecasts"]
    forecasts["minItems"] = len(decision_slot_ids)
    forecasts["maxItems"] = len(decision_slot_ids)
    return schema


class WorldModelAblationStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class WorldModelAblationTarget(FrozenModel):
    formal_forecast_id: str = Field(min_length=1)
    decision_slot_id: str = Field(min_length=1)
    contract_id: str = Field(min_length=1)


class WorldModelAblationAssignment(FrozenModel):
    assignment_id: str
    plan_id: str
    formal_forecast_id: str
    decision_slot_id: str
    contract_id: str
    targets: tuple[WorldModelAblationTarget, ...] = ()
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
        if self.targets:
            if set(control_input) != {"purpose", "forecast_targets"}:
                raise ValueError("联合 WorldModel control 输入必须只移除 world_model")
            raw_targets = control_input.get("forecast_targets")
            if not isinstance(raw_targets, list) or len(raw_targets) != len(self.targets):
                raise ValueError("联合 WorldModel control 目标集合不一致")
            input_slot_ids = tuple(
                item.get("decision_slot", {}).get("decision_slot_id")
                for item in raw_targets
                if isinstance(item, dict)
            )
            if input_slot_ids != tuple(item.decision_slot_id for item in self.targets):
                raise ValueError("联合 WorldModel control 目标顺序或身份不一致")
            if (
                self.formal_forecast_id != self.targets[0].formal_forecast_id
                or self.decision_slot_id != self.targets[0].decision_slot_id
                or self.contract_id != self.targets[0].contract_id
            ):
                raise ValueError("联合 WorldModel control 锚点必须等于首个目标")
            if len({item.decision_slot_id for item in self.targets}) != len(self.targets):
                raise ValueError("联合 WorldModel control 目标不得重复")
        elif set(control_input) != {
            "purpose",
            "decision_slot",
            "forecast_contract",
            "target_state",
        }:
            raise ValueError("旧 WorldModel control 输入边界不完整")
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
            _validate_persisted_control_output(output)
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
    formal_mean_ranked_probability_score: Decimal | None = None
    control_mean_ranked_probability_score: Decimal | None = None
    mean_ranked_probability_improvement: Decimal | None = None
    conservative_mean_ranked_probability_improvement: Decimal | None = None
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
    conservative_ranked_improvement: Decimal
    conservative_brier_improvement: Decimal
    formal_ranked_score: Decimal | None = None
    control_ranked_score: Decimal | None = None
    formal_brier_score: Decimal | None = None
    control_brier_score: Decimal | None = None


def world_model_ablation_behavior_hash(
    *,
    config: AppConfig,
    contracts: tuple[ForecastContract, ...],
) -> str:
    context = config.capital.context_forecast
    if context is None:
        raise ValueError("WorldModel control 缺少正式 Context Forecast")
    runtime = context_forecast_runtime(config.codex_runtime, context)
    return content_hash(
        {
            "input_version": CONTROL_INPUT_VERSION,
            "output_version": CONTROL_OUTPUT_VERSION,
            "instructions": CONTROL_INSTRUCTIONS,
            "output_schema": strict_output_schema(
                WorldModelControlStructuredOutput.model_json_schema()
            ),
            "formal_contracts": contracts,
            "formal_producer_behavior_id": context.producer_behavior_id,
            "execution_contract": codex_execution_contract(),
            "runtime_policy_version": runtime.version,
            "expected_cli_version": runtime.expected_cli_version,
            "expected_binary_sha256": runtime.expected_binary_sha256,
            "model": runtime.model,
            "reasoning_effort": runtime.reasoning_effort,
            "maximum_schema_attempts": 1 + runtime.max_account_switches,
            "call_order": CONTROL_CALL_ORDER,
        }
    )


def ensure_world_model_ablation_plan(
    *,
    governance: SqlGovernanceRepository,
    config: AppConfig,
    contracts: tuple[ForecastContract, ...],
    release: ReleaseManifest,
    registered_at: datetime,
) -> EvaluationPlan:
    policy = _enabled_policy(config)
    now = require_utc(registered_at)
    if not contracts or len({item.contract_id for item in contracts}) != len(contracts):
        raise ValueError("WorldModel control 正式合同必须非空且唯一")
    behavior_hash = world_model_ablation_behavior_hash(config=config, contracts=contracts)
    context = config.capital.context_forecast
    assert context is not None
    spec: dict[str, object] = {
        "version": policy.version,
        "activated_at": policy.activated_at.isoformat().replace("+00:00", "Z"),
        "formal_contract_ids": [item.contract_id for item in contracts],
        "formal_producer_behavior_id": context.producer_behavior_id,
        "control_behavior_hash": behavior_hash,
        "assignment_rule": "ONE_ASSIGNMENT_FOR_EXACT_FORMAL_TARGET_SET_BEFORE_BOTH_CALLS",
        "formal_missing_rule": (
            "COUNT_PREASSIGNED_TARGET_TERMINALS_ONLY;"
            "UNPAIRED_STATE_FAILURES_ARE_DIAGNOSTIC_ONLY"
        ),
        "missing_score_rule": (
            "LOWER_BOUND_MISSING_CONTROL_AS_PERFECT_AND_FORMAL_NO_ESTIMATE_AS_WORST"
        ),
        "permission_rule": "ALL_SETTLED_PREASSIGNED_TARGETS_ENTER_CONSERVATIVE_BOUND",
        "sample_selection": SAMPLE_SELECTION_RULE,
        "uncertainty_method": UNCERTAINTY_METHOD,
        "call_order": CONTROL_CALL_ORDER,
        "input_difference": "REMOVE_SHARED_WORLD_MODEL_ONLY_KEEP_FULL_TARGET_SET",
        "evaluation_unit": "ONE_JOINT_CALL_WITH_PER_TARGET_PAIRED_SCORES",
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
            "FULL_JOINT_TARGET_SET_PRESERVED",
            "ONE_CONTROL_CALL_PER_FORMAL_CALL",
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
    registered_contract_ids = spec.get("formal_contract_ids")
    if (
        formal_producer_behavior_id != spec.get("formal_producer_behavior_id")
        or not isinstance(registered_contract_ids, (list, tuple))
        or slot.contract_id not in registered_contract_ids
    ):
        raise ValueError("WorldModel control 正式行为不属于预登记 cohort")
    raw = _canonical_payload(canonical_json(formal_analysis_input), "formal input")
    world_model = raw.get("world_model")
    if not isinstance(world_model, dict):
        raise ValueError("WorldModel control 正式输入缺少 world_model")
    _validate_world_model_reference_ids(world_model)
    forecast_targets = raw.get("forecast_targets")
    if not isinstance(forecast_targets, list) or not forecast_targets:
        raise ValueError("联合 WorldModel control 正式输入缺少 forecast_targets")
    targets: list[WorldModelAblationTarget] = []
    target_buckets: list[tuple[str, tuple[str, ...]]] = []
    for item in forecast_targets:
        if not isinstance(item, dict):
            raise ValueError("联合 WorldModel control 目标结构非法")
        decision_slot = item.get("decision_slot")
        forecast_contract = item.get("forecast_contract")
        target_state = item.get("target_state")
        if not all(
            isinstance(value, dict)
            for value in (decision_slot, forecast_contract, target_state)
        ):
            raise ValueError("联合 WorldModel control 目标结构不完整")
        decision_slot_id = decision_slot.get("decision_slot_id")
        contract_id = forecast_contract.get("contract_id")
        if not isinstance(decision_slot_id, str) or not isinstance(contract_id, str):
            raise ValueError("联合 WorldModel control 目标身份非法")
        if contract_id not in registered_contract_ids:
            raise ValueError("联合 WorldModel control 包含未登记合同")
        if (
            decision_slot.get("information_cutoff_at")
            != slot.information_cutoff_at.isoformat().replace("+00:00", "Z")
            or decision_slot.get("completion_deadline_at")
            != slot.completion_deadline_at.isoformat().replace("+00:00", "Z")
            or decision_slot.get("evaluation_at")
            != slot.evaluation_at.isoformat().replace("+00:00", "Z")
        ):
            raise ValueError("联合 WorldModel control 目标必须共享正式调用时间边界")
        raw_buckets = forecast_contract.get("outcome_buckets")
        if not isinstance(raw_buckets, list) or not raw_buckets:
            raise ValueError("联合 WorldModel control 输入合同缺少结果桶")
        bucket_ids = tuple(
            bucket.get("bucket_id")
            for bucket in raw_buckets
            if isinstance(bucket, dict) and isinstance(bucket.get("bucket_id"), str)
        )
        if len(bucket_ids) != len(raw_buckets) or len(set(bucket_ids)) != len(bucket_ids):
            raise ValueError("联合 WorldModel control 输入合同结果桶非法")
        targets.append(
            WorldModelAblationTarget(
                formal_forecast_id=stable_id(
                    "base_forecast", decision_slot_id, formal_producer_behavior_id
                ),
                decision_slot_id=decision_slot_id,
                contract_id=contract_id,
            )
        )
        target_buckets.append((decision_slot_id, bucket_ids))
    if slot.slot_id not in {item.decision_slot_id for item in targets}:
        raise ValueError("联合 WorldModel control 正式组合输入缺少锚点 slot")
    schema = world_model_control_output_schema_for_targets(
        target_buckets=tuple(target_buckets),
    )
    control_input = {key: value for key, value in raw.items() if key != "world_model"}
    if set(control_input) != {"purpose", "forecast_targets"}:
        raise ValueError("联合 WorldModel control 正式输入边界发生漂移")
    control_input_json = canonical_json(control_input)
    output_schema_json = canonical_json(schema)
    behavior_hash = str(spec["control_behavior_hash"])
    anchor = targets[0]
    formal_forecast_id = anchor.formal_forecast_id
    values = {
        "assignment_id": stable_id(
            "world_model_ablation_assignment",
            plan.plan_id,
            formal_forecast_id,
        ),
        "plan_id": plan.plan_id,
        "formal_forecast_id": formal_forecast_id,
        "decision_slot_id": anchor.decision_slot_id,
        "contract_id": anchor.contract_id,
        "targets": tuple(targets),
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
        configured_contract_ids = spec.get("formal_contract_ids")
        if isinstance(configured_contract_ids, (list, tuple)) and configured_contract_ids:
            formal_contract_ids = tuple(str(item) for item in configured_contract_ids)
            joint_plan = True
        else:
            legacy_contract_id = spec.get("formal_contract_id")
            if not isinstance(legacy_contract_id, str) or not legacy_contract_id:
                raise ValueError("WorldModel control plan 缺少冻结正式合同")
            formal_contract_ids = (legacy_contract_id,)
            joint_plan = False
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
        joined = world_model_ablation_assignments.outerjoin(
            world_model_ablation_results,
            world_model_ablation_results.c.assignment_id
            == world_model_ablation_assignments.c.assignment_id,
        )
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(
                    world_model_ablation_assignments.c.payload,
                    world_model_ablation_results.c.payload,
                )
                .select_from(joined)
                .where(world_model_ablation_assignments.c.plan_id == plan_id)
                .order_by(world_model_ablation_assignments.c.evaluation_at)
            ).all()
            assignments = tuple(
                WorldModelAblationAssignment.model_validate(row[0]) for row in rows
            )
            assignment_targets = tuple(
                target
                for assignment in assignments
                for target in _assignment_targets(assignment)
            )
            formal_ids = tuple(item.formal_forecast_id for item in assignment_targets)
            slot_ids = tuple(item.decision_slot_id for item in assignment_targets)
            formal_payloads = (
                connection.execute(
                    select(forecasts.c.payload).where(
                        forecasts.c.forecast_id.in_(formal_ids)
                    )
                ).scalars()
                if formal_ids
                else ()
            )
            outcome_payloads = (
                connection.execute(
                    select(forecast_outcomes.c.payload).where(
                        forecast_outcomes.c.decision_slot_id.in_(slot_ids),
                        forecast_outcomes.c.evaluation_version == evaluation_version,
                    )
                ).scalars()
                if slot_ids
                else ()
            )
            formal_by_id = {
                item.forecast_id: item
                for payload in formal_payloads
                for item in (BaseForecast.model_validate(payload),)
            }
            outcome_by_slot = {
                item.decision_slot_id: item
                for payload in outcome_payloads
                for item in (ForecastOutcome.model_validate(payload),)
            }
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
                    forecasts.c.contract_id.in_(formal_contract_ids),
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
                    forecast_no_estimates.c.contract_id.in_(formal_contract_ids),
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
        for (assignment_raw, result_raw), assignment in zip(
            rows, assignments, strict=True
        ):
            del assignment_raw
            if result_raw is None:
                if current <= assignment.completion_deadline_at:
                    pending += 1
                    continue
                failed += 1
                result = None
            else:
                result = WorldModelAblationResult.model_validate(result_raw)
                if result.status == WorldModelAblationStatus.SUCCEEDED:
                    succeeded += 1
                else:
                    failed += 1
            control_by_slot: dict[str, WorldModelControlForecastDraft] = {}
            if result is not None and result.status == WorldModelAblationStatus.SUCCEEDED:
                assert result.output_json is not None
                parsed = _validate_persisted_control_output(
                    _canonical_payload(result.output_json, "control output")
                )
                drafts = (
                    parsed.forecasts
                    if isinstance(parsed, WorldModelControlStructuredOutput)
                    else (parsed.forecast,)
                )
                control_by_slot = {item.decision_slot_id: item for item in drafts}
            target_cases: list[_AblationScoreCase] = []
            for target in _assignment_targets(assignment):
                outcome = outcome_by_slot.get(target.decision_slot_id)
                if outcome is None or outcome.status != ForecastOutcomeStatus.SETTLED:
                    continue
                formal = formal_by_id.get(target.formal_forecast_id)
                formal_ranked_score = None
                formal_brier_score = None
                if formal is not None:
                    assert outcome.realized_bucket_id is not None
                    formal_probabilities = tuple(
                        (item.bucket_id, item.probability)
                        for item in formal.outcome_probabilities
                    )
                    formal_ranked_score = ordinal_ranked_probability_score(
                        formal_probabilities,
                        outcome.realized_bucket_id,
                    )
                    formal_brier_score = multiclass_brier_score(
                        formal_probabilities,
                        outcome.realized_bucket_id,
                    )
                draft = control_by_slot.get(target.decision_slot_id)
                if formal_ranked_score is not None and draft is not None:
                    assert outcome.realized_bucket_id is not None
                    control_probabilities = tuple(
                        (item.bucket_id, Decimal(item.probability))
                        for item in draft.outcome_probabilities
                    )
                    control_ranked_score = ordinal_ranked_probability_score(
                        control_probabilities,
                        outcome.realized_bucket_id,
                    )
                    control_brier_score = multiclass_brier_score(
                        control_probabilities,
                        outcome.realized_bucket_id,
                    )
                    settled_pairs += 1
                    target_cases.append(
                        _AblationScoreCase(
                            identity=target.decision_slot_id,
                            information_cutoff_at=assignment.information_cutoff_at,
                            evaluation_at=assignment.evaluation_at,
                            formal_ranked_score=formal_ranked_score,
                            control_ranked_score=control_ranked_score,
                            formal_brier_score=formal_brier_score,
                            control_brier_score=control_brier_score,
                            conservative_ranked_improvement=(
                                control_ranked_score - formal_ranked_score
                            ),
                            conservative_brier_improvement=(
                                control_brier_score - formal_brier_score
                            ),
                        )
                    )
                else:
                    assert (formal_ranked_score is None) == (formal_brier_score is None)
                    target_cases.append(
                        _AblationScoreCase(
                            identity=target.decision_slot_id,
                            information_cutoff_at=assignment.information_cutoff_at,
                            evaluation_at=assignment.evaluation_at,
                            conservative_ranked_improvement=(
                                -formal_ranked_score
                                if formal_ranked_score is not None
                                else -_MAXIMUM_RANKED_PROBABILITY_SCORE
                            ),
                            conservative_brier_improvement=(
                                -formal_brier_score
                                if formal_brier_score is not None
                                else -_MAXIMUM_MULTICLASS_BRIER_SCORE
                            ),
                        )
                    )
            if target_cases:
                paired_target_cases = tuple(
                    item
                    for item in target_cases
                    if item.formal_ranked_score is not None
                    and item.control_ranked_score is not None
                )
                score_cases.append(
                    _AblationScoreCase(
                        identity=assignment.assignment_id,
                        information_cutoff_at=assignment.information_cutoff_at,
                        evaluation_at=assignment.evaluation_at,
                        formal_ranked_score=_mean(
                            [item.formal_ranked_score for item in paired_target_cases]
                        ),
                        control_ranked_score=_mean(
                            [item.control_ranked_score for item in paired_target_cases]
                        ),
                        formal_brier_score=_mean(
                            [item.formal_brier_score for item in paired_target_cases]
                        ),
                        control_brier_score=_mean(
                            [item.control_brier_score for item in paired_target_cases]
                        ),
                        conservative_ranked_improvement=_mean(
                            [item.conservative_ranked_improvement for item in target_cases]
                        )
                        or Decimal("0"),
                        conservative_brier_improvement=_mean(
                            [item.conservative_brier_improvement for item in target_cases]
                        )
                        or Decimal("0"),
                    )
                )
        for result_id, _evaluation_at, outcome_raw in (
            no_estimate_rows if not joint_plan else ()
        ):
            if outcome_raw is None:
                continue
            outcome = ForecastOutcome.model_validate(outcome_raw)
            if outcome.status != ForecastOutcomeStatus.SETTLED:
                continue
            # The formal side supplied no distribution.  For a lower bound on
            # WorldModel skill, assume the absent formal forecast was maximally
            # wrong and the unobserved control was perfect under both proper scores.
            score_cases.append(
                _AblationScoreCase(
                    identity=result_id,
                    information_cutoff_at=outcome.information_cutoff_at,
                    evaluation_at=outcome.evaluation_at,
                    conservative_ranked_improvement=(
                        -_MAXIMUM_RANKED_PROBABILITY_SCORE
                    ),
                    conservative_brier_improvement=-_MAXIMUM_MULTICLASS_BRIER_SCORE,
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
            if item.formal_ranked_score is not None
            and item.control_ranked_score is not None
        )
        ranked_pairs: list[tuple[Decimal, Decimal]] = []
        brier_pairs: list[tuple[Decimal, Decimal]] = []
        for item in paired:
            assert item.formal_ranked_score is not None
            assert item.control_ranked_score is not None
            assert item.formal_brier_score is not None
            assert item.control_brier_score is not None
            ranked_pairs.append((item.formal_ranked_score, item.control_ranked_score))
            brier_pairs.append((item.formal_brier_score, item.control_brier_score))
        formal_ranked_scores = [formal for formal, _control in ranked_pairs]
        control_ranked_scores = [control for _formal, control in ranked_pairs]
        ranked_improvements = [
            control - formal
            for formal, control in zip(
                formal_ranked_scores,
                control_ranked_scores,
                strict=True,
            )
        ]
        formal_brier_scores = [formal for formal, _control in brier_pairs]
        control_brier_scores = [control for _formal, control in brier_pairs]
        brier_improvements = [
            control - formal
            for formal, control in zip(
                formal_brier_scores,
                control_brier_scores,
                strict=True,
            )
        ]
        conservative_ranked_values = tuple(
            item.conservative_ranked_improvement for item in independent
        )
        conservative_brier_values = tuple(
            item.conservative_brier_improvement for item in independent
        )
        lower_bound = (
            None
            if len(conservative_ranked_values) < 2
            else conservative_newey_west_lower_bound(
                conservative_ranked_values,
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
            conservative_sample_count=len(conservative_ranked_values),
            formal_mean_ranked_probability_score=_mean(formal_ranked_scores),
            control_mean_ranked_probability_score=_mean(control_ranked_scores),
            mean_ranked_probability_improvement=_mean(ranked_improvements),
            conservative_mean_ranked_probability_improvement=_mean(
                list(conservative_ranked_values)
            ),
            formal_mean_brier=_mean(formal_brier_scores),
            control_mean_brier=_mean(control_brier_scores),
            mean_brier_improvement=_mean(brier_improvements),
            conservative_mean_brier_improvement=_mean(
                list(conservative_brier_values)
            ),
            conservative_improvement_lower_bound=lower_bound,
            minimum_sample_size=minimum_sample_size,
            evidence_sufficient=(
                len(conservative_ranked_values) >= minimum_sample_size
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
        formal_analysis_input: dict[str, object] | None,
        formal_output_schema: dict[str, object] | None,
    ) -> None:
        del formal_output_schema
        if slot.information_cutoff_at < self.policy.activated_at:
            return
        if formal_producer_behavior_id != self.formal_producer_behavior_id:
            raise ValueError("WorldModel control preflight 绑定了错误正式行为")
        if formal_analysis_input is None:
            return
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
    contracts: tuple[ForecastContract, ...],
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
        contracts=contracts,
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
    context = config.capital.context_forecast
    if context is None:
        raise ValueError("WorldModel control 缺少正式 Context Forecast")
    runtime = context_forecast_runtime(config.codex_runtime, context)
    router = assemble_codex_router(
        config,
        leases=SqlAccountLeaseStore(engine),
        audit=SqlCodexAuditStore(engine),
        output_adapter=TypeAdapter(WorldModelControlStructuredOutput),
        runtime_policy=runtime,
    )
    return CodexWorldModelControlAnalyst(
        bundle_root=runtime.bundle_root,
        maximum_prompt_characters=runtime.maximum_prompt_characters,
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
        compact_ids = raw.get("evidence_ids", [])
        if not isinstance(compact_ids, list) or not all(
            isinstance(item, str) for item in compact_ids
        ):
            raise ValueError("WorldModel mechanism evidence_ids 非法")
        evidence_ids.update(compact_ids)
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
    control_input = _canonical_payload(assignment.control_input_json, "control input")
    raw_targets = control_input.get("forecast_targets")
    if not isinstance(raw_targets, list):
        raise ValueError("联合 WorldModel control 输入缺少目标")
    expected_buckets = {
        item["decision_slot"]["decision_slot_id"]: tuple(
            bucket["bucket_id"]
            for bucket in item["forecast_contract"]["outcome_buckets"]
        )
        for item in raw_targets
    }
    drafts = {item.decision_slot_id: item for item in analyst.output.forecasts}
    output_invalid = len(drafts) != len(analyst.output.forecasts) or set(drafts) != set(
        expected_buckets
    )
    if not output_invalid:
        for decision_slot_id, draft in drafts.items():
            actual_buckets = tuple(
                item.bucket_id for item in draft.outcome_probabilities
            )
            probabilities = tuple(
                Decimal(item.probability) for item in draft.outcome_probabilities
            )
            if (
                actual_buckets != expected_buckets[decision_slot_id]
                or sum(probabilities, Decimal("0")) != 1
            ):
                output_invalid = True
                break
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


def _validate_persisted_control_output(
    output: dict[str, object],
) -> WorldModelControlStructuredOutput | _LegacyWorldModelControlStructuredOutput:
    if "forecasts" in output:
        return WorldModelControlStructuredOutput.model_validate(output)
    return _LegacyWorldModelControlStructuredOutput.model_validate(output)


def _assignment_targets(
    assignment: WorldModelAblationAssignment,
) -> tuple[WorldModelAblationTarget, ...]:
    if assignment.targets:
        return assignment.targets
    return (
        WorldModelAblationTarget(
            formal_forecast_id=assignment.formal_forecast_id,
            decision_slot_id=assignment.decision_slot_id,
            contract_id=assignment.contract_id,
        ),
    )


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
    "WorldModelAblationTarget",
    "assemble_world_model_ablation_analyst",
    "assemble_world_model_ablation_preallocator",
    "build_world_model_ablation_assignment",
    "ensure_world_model_ablation_plan",
    "world_model_ablation_behavior_hash",
]
