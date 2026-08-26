from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, field_validator, model_validator

from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import (
    optional_utc,
    require_utc,
)
from investment_manager.kernel.types import FrozenModel
from investment_manager.scheduling.models import AnalysisTriggerPlan
from investment_manager.settings import AppConfig


class ChangeType(StrEnum):
    PROMPT_PACK = "PROMPT_PACK"
    PANEL_POLICY = "PANEL_POLICY"
    FEATURE_SET = "FEATURE_SET"
    STRATEGY_PIPELINE = "STRATEGY_PIPELINE"
    COMPOSITION_POLICY = "COMPOSITION_POLICY"
    FREQUENCY_POLICY = "FREQUENCY_POLICY"
    METRIC_DEFINITION = "METRIC_DEFINITION"
    CODE = "CODE"
    DEPENDENCY = "DEPENDENCY"
    RISK_POLICY = "RISK_POLICY"


class EvaluationStage(StrEnum):
    STATIC = "STATIC"
    FIXED_REGRESSION = "FIXED_REGRESSION"
    WALK_FORWARD = "WALK_FORWARD"
    BLIND = "BLIND"
    FORWARD = "FORWARD"
    SHADOW = "SHADOW"
    CANARY = "CANARY"


class StageOutcome(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"


class SystemConstitution(FrozenModel):
    version: str
    objective: str
    immutable_rules: tuple[str, ...] = Field(min_length=1)
    human_reserved_powers: tuple[str, ...] = Field(min_length=1)
    agent_forbidden_changes: tuple[str, ...] = Field(min_length=1)


class RegressionCase(FrozenModel):
    id: str
    fixture: str | None = None
    test: str | None = None
    invariants: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def case_has_exactly_one_target(self):
        if (self.fixture is None) == (self.test is None):
            raise ValueError("回归用例必须且只能指定 fixture 或 test")
        return self


class RegressionSuite(FrozenModel):
    version: str
    immutable: bool
    cases: tuple[RegressionCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def case_ids_are_unique(self):
        ids = [item.id for item in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("固定回归集 case id 必须唯一")
        if not self.immutable:
            raise ValueError("生产回归集必须标记 immutable")
        return self


class ReleaseArtifact(FrozenModel):
    artifact_id: str
    relative_path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def path_must_be_repository_relative(self):
        path = Path(self.relative_path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("ReleaseArtifact 必须使用仓库内相对路径")
        return self


class ReleaseManifest(FrozenModel):
    manifest_id: str
    created_at: datetime
    status: str
    code_version: str
    configuration_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )
    component_versions: tuple[tuple[str, str], ...]
    artifacts: tuple[ReleaseArtifact, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    constitution_version: str
    parent_manifest_id: str | None = None
    complexity_score: int = Field(default=0, ge=0)

    _utc_created_at = field_validator("created_at")(require_utc)

    @model_validator(mode="after")
    def artifacts_are_unique_and_sorted(self):
        ids = tuple(item.artifact_id for item in self.artifacts)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("ReleaseManifest 制品必须按唯一 ID 排序")
        paths = tuple(item.relative_path for item in self.artifacts)
        if len(paths) != len(set(paths)):
            raise ValueError("ReleaseManifest 制品路径不得重复")
        return self


class FailedExperiment(FrozenModel):
    experiment_id: str
    hypothesis_fingerprint: str
    evidence_ids: tuple[str, ...]
    rejected_at: datetime
    reason_codes: tuple[str, ...]

    _utc_rejected_at = field_validator("rejected_at")(require_utc)


def evaluation_plan_invalidation_id(plan_id: str) -> str:
    return stable_id("invalidated_evaluation_plan", plan_id)


def build_evaluation_plan_invalidation(
    *,
    plan_id: str,
    invalidated_at: datetime,
    reason_codes: tuple[str, ...],
    evidence_ids: tuple[str, ...],
) -> FailedExperiment:
    if not reason_codes or not evidence_ids:
        raise ValueError("EvaluationPlan 失效必须包含原因码和证据")
    return FailedExperiment(
        experiment_id=evaluation_plan_invalidation_id(plan_id),
        hypothesis_fingerprint=content_hash({"invalidated_evaluation_plan": plan_id}),
        evidence_ids=(f"evaluation_plan:{plan_id}", *evidence_ids),
        rejected_at=require_utc(invalidated_at),
        reason_codes=("EVALUATION_PLAN_INVALIDATED", *reason_codes),
    )


class GovernanceSnapshot(FrozenModel):
    snapshot_id: str
    as_of: datetime
    constitution: SystemConstitution
    champion: ReleaseManifest
    previous_stable_manifest_ids: tuple[str, ...]
    metric_summaries: tuple[tuple[str, str], ...] = ()
    failed_experiments: tuple[FailedExperiment, ...] = ()
    open_proposal_ids: tuple[str, ...] = ()
    available_evaluation_plans: tuple[EvaluationPlan, ...] = ()
    architecture_decision_ids: tuple[str, ...] = ()
    analysis_trigger_plans: tuple[AnalysisTriggerPlan, ...] = ()
    complexity_used: int = Field(default=0, ge=0)
    complexity_limit: int = Field(default=10, ge=0)
    content_hash: str

    _utc_as_of = field_validator("as_of")(require_utc)

    @model_validator(mode="after")
    def plans_must_be_preregistered_for_champion(self):
        plan_ids = [item.plan_id for item in self.available_evaluation_plans]
        if len(plan_ids) != len(set(plan_ids)):
            raise ValueError("治理快照中的 EvaluationPlan 不得重复")
        if any(
            item.base_manifest_id != self.champion.manifest_id or item.registered_at > self.as_of
            for item in self.available_evaluation_plans
        ):
            raise ValueError("治理快照只能包含当前 Champion 已预登记的 EvaluationPlan")
        trigger_scopes = [(item.symbol, item.pipeline_id) for item in self.analysis_trigger_plans]
        if len(trigger_scopes) != len(set(trigger_scopes)):
            raise ValueError("治理快照中的 AnalysisTriggerPlan 作用域不得重复")
        if any(
            item.manifest_id != self.champion.manifest_id for item in self.analysis_trigger_plans
        ):
            raise ValueError("治理快照只能包含当前 Champion 的 AnalysisTriggerPlan")
        return self


class EvaluationPlan(FrozenModel):
    plan_id: str
    registered_at: datetime
    base_manifest_id: str
    primary_metric: str
    minimum_sample_size: int = Field(gt=0)
    hard_guardrails: tuple[str, ...] = Field(min_length=1)
    required_stages: tuple[EvaluationStage, ...] = Field(min_length=1)
    fixed_regression_suite_version: str
    candidate_spec_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    candidate_spec_snapshot: dict[str, object] | None = None
    blind_query_budget: int = Field(default=0, ge=0)

    _utc_registered_at = field_validator("registered_at")(require_utc)

    @field_validator("required_stages")
    @classmethod
    def stages_are_unique_and_ordered(
        cls, stages: tuple[EvaluationStage, ...]
    ) -> tuple[EvaluationStage, ...]:
        canonical = tuple(EvaluationStage)
        positions = [canonical.index(item) for item in stages]
        if len(set(stages)) != len(stages) or positions != sorted(positions):
            raise ValueError("评估阶段必须唯一并按固定顺序登记")
        return stages

    @model_validator(mode="after")
    def blind_stage_requires_query_budget(self):
        if EvaluationStage.BLIND in self.required_stages and self.blind_query_budget != 1:
            raise ValueError("BLIND 阶段必须预登记恰好一次查询预算")
        if self.candidate_spec_snapshot is not None and (
            self.candidate_spec_hash is None
            or content_hash(self.candidate_spec_snapshot) != self.candidate_spec_hash
        ):
            raise ValueError("EvaluationPlan 候选规格快照与哈希不一致")
        return self


class BlindEvaluationClaim(FrozenModel):
    """Single durable claim for revealing one plan's reserved blind window."""

    query_id: str
    blind_scope_id: str
    blind_symbol: str = Field(pattern=r"^[A-Z0-9]{2,32}$")
    blind_start: datetime
    blind_end: datetime
    plan_id: str
    source_evaluation_id: str
    claimed_at: datetime
    completed_at: datetime | None = None
    result_id: str | None = None
    result_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    _utc_claimed_at = field_validator("claimed_at")(require_utc)
    _utc_completed_at = field_validator("completed_at")(optional_utc)
    _utc_blind_start = field_validator("blind_start")(require_utc)
    _utc_blind_end = field_validator("blind_end")(require_utc)

    @model_validator(mode="after")
    def completion_fields_are_atomic(self):
        if self.blind_start >= self.blind_end:
            raise ValueError("盲测时间窗起点必须早于终点")
        expected_scope_id = stable_id(
            "blind_evaluation_scope",
            self.blind_symbol,
            self.blind_start,
            self.blind_end,
        )
        if self.blind_scope_id != expected_scope_id:
            raise ValueError("盲测时间窗身份与边界不一致")
        completion = (self.completed_at, self.result_id, self.result_hash)
        if any(item is not None for item in completion) and not all(
            item is not None for item in completion
        ):
            raise ValueError("盲测认领的完成字段必须同时存在")
        if self.completed_at is not None and self.completed_at < self.claimed_at:
            raise ValueError("盲测完成时间不能早于认领时间")
        return self


class ChangeProposal(FrozenModel):
    decision_type: Literal["CHANGE_PROPOSAL"] = "CHANGE_PROPOSAL"
    proposal_id: str
    created_at: datetime
    change_type: ChangeType
    base_manifest_id: str
    hypothesis: str = Field(min_length=10, max_length=1_000)
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    affected_layers: tuple[str, ...] = Field(min_length=1)
    expected_effects: tuple[str, ...] = Field(min_length=1)
    economic_case: str = Field(min_length=10, max_length=1_000)
    simplest_alternative: str = Field(min_length=10, max_length=1_000)
    guardrails: tuple[str, ...] = Field(min_length=1)
    evaluation_plan_id: str
    rollback_to_manifest_id: str
    complexity_delta: int
    sunset_condition: str = Field(min_length=10, max_length=1_000)
    manual_only: bool = False
    indivisible_change_rationale: str | None = None

    _utc_created_at = field_validator("created_at")(require_utc)

    @property
    def hypothesis_fingerprint(self) -> str:
        return content_hash({"hypothesis": self.hypothesis.strip().lower()})

    @model_validator(mode="after")
    def cross_layer_change_requires_rationale(self):
        if len(self.affected_layers) > 1 and not self.indivisible_change_rationale:
            raise ValueError("跨层变更必须解释为何无法拆分")
        return self


class NoChange(FrozenModel):
    decision_type: Literal["NO_CHANGE"] = "NO_CHANGE"
    decision_id: str
    observed_at: datetime
    reason_codes: tuple[str, ...] = Field(min_length=1)
    revisit_conditions: tuple[str, ...] = Field(min_length=1)

    _utc_observed_at = field_validator("observed_at")(require_utc)


class GovernanceGateResult(FrozenModel):
    accepted: bool
    reason_codes: tuple[str, ...]


class GovernanceGate:
    """只登记候选，不实现、不评估、更不发布。"""

    def validate(
        self,
        proposal: ChangeProposal,
        plan: EvaluationPlan,
        snapshot: GovernanceSnapshot,
    ) -> GovernanceGateResult:
        reasons: list[str] = []
        if snapshot.open_proposal_ids:
            reasons.append("OPEN_PROPOSAL_ALREADY_EXISTS")
        if proposal.base_manifest_id != snapshot.champion.manifest_id:
            reasons.append("BASE_IS_NOT_CURRENT_CHAMPION")
        if plan.base_manifest_id != proposal.base_manifest_id:
            reasons.append("EVALUATION_BASE_MISMATCH")
        if plan.registered_at > proposal.created_at:
            reasons.append("EVALUATION_PLAN_NOT_PREREGISTERED")
        if plan.plan_id != proposal.evaluation_plan_id:
            reasons.append("EVALUATION_PLAN_ID_MISMATCH")
        if proposal.rollback_to_manifest_id not in {
            snapshot.champion.manifest_id,
            *snapshot.previous_stable_manifest_ids,
        }:
            reasons.append("ROLLBACK_TARGET_NOT_STABLE")
        if proposal.change_type == ChangeType.RISK_POLICY and not proposal.manual_only:
            reasons.append("RISK_POLICY_MUST_BE_MANUAL_ONLY")
        if snapshot.complexity_used + proposal.complexity_delta > snapshot.complexity_limit:
            reasons.append("COMPLEXITY_BUDGET_EXCEEDED")
        if not set(plan.hard_guardrails).issubset(set(proposal.guardrails)):
            reasons.append("HARD_GUARDRAIL_MISSING")
        previous = next(
            (
                item
                for item in snapshot.failed_experiments
                if item.hypothesis_fingerprint == proposal.hypothesis_fingerprint
            ),
            None,
        )
        if previous is not None and set(proposal.evidence_ids).issubset(previous.evidence_ids):
            reasons.append("FAILED_HYPOTHESIS_WITHOUT_NEW_EVIDENCE")
        return GovernanceGateResult(accepted=not reasons, reason_codes=tuple(reasons))


class StageResult(FrozenModel):
    stage: EvaluationStage
    outcome: StageOutcome
    artifact_hash: str = Field(min_length=16)
    evidence_set_version: str = Field(min_length=1)
    evidence_hashes: tuple[str, ...] = Field(min_length=1)
    sample_size: int = Field(ge=0)
    safety_violations: int = Field(ge=0)
    metric_values: tuple[tuple[str, str], ...] = ()
    reason_codes: tuple[str, ...] = ()


class EvaluationResult(FrozenModel):
    evaluation_id: str
    proposal_id: str
    plan_id: str
    candidate_manifest_id: str
    completed_at: datetime
    stage_results: tuple[StageResult, ...]

    _utc_completed_at = field_validator("completed_at")(require_utc)

    @model_validator(mode="after")
    def stages_must_be_unique_and_bound_to_one_artifact(self):
        stages = [item.stage for item in self.stage_results]
        if len(stages) != len(set(stages)):
            raise ValueError("EvaluationResult 的阶段不得重复")
        artifact_hashes = {item.artifact_hash for item in self.stage_results}
        if len(artifact_hashes) > 1:
            raise ValueError("EvaluationResult 的所有阶段必须绑定同一候选制品")
        return self


class EvaluationTarget(FrozenModel):
    proposal: ChangeProposal
    plan: EvaluationPlan
    candidate: ReleaseManifest
    artifact_hash: str = Field(min_length=16)

    @model_validator(mode="after")
    def relationships_must_match(self):
        if self.proposal.evaluation_plan_id != self.plan.plan_id:
            raise ValueError("候选提案与 EvaluationPlan 不一致")
        if self.proposal.base_manifest_id != self.plan.base_manifest_id:
            raise ValueError("候选提案与 EvaluationPlan 基线不一致")
        if self.candidate.parent_manifest_id != self.plan.base_manifest_id:
            raise ValueError("候选 ReleaseManifest 必须从评估基线分叉")
        if self.candidate.status != "CHALLENGER":
            raise ValueError("VersionEvaluation 只接受 CHALLENGER")
        return self


def build_evaluation_result(
    *,
    target: EvaluationTarget,
    completed_at: datetime,
    stage_results: tuple[StageResult, ...],
) -> EvaluationResult:
    completed_at = require_utc(completed_at)
    if any(item.artifact_hash != target.artifact_hash for item in stage_results):
        raise ValueError("StageResult 与候选制品哈希不一致")
    evaluation_id = stable_id(
        "evaluation",
        target.proposal.proposal_id,
        target.plan.plan_id,
        target.candidate.manifest_id,
        target.artifact_hash,
        content_hash([item.model_dump(mode="json") for item in stage_results]),
    )
    return EvaluationResult(
        evaluation_id=evaluation_id,
        proposal_id=target.proposal.proposal_id,
        plan_id=target.plan.plan_id,
        candidate_manifest_id=target.candidate.manifest_id,
        completed_at=completed_at,
        stage_results=stage_results,
    )


class PromotionDecision(FrozenModel):
    eligible_for_human_approval: bool
    reason_codes: tuple[str, ...]


class PromotionGate:
    def evaluate(self, result: EvaluationResult, plan: EvaluationPlan) -> PromotionDecision:
        reasons: list[str] = []
        by_stage = {item.stage: item for item in result.stage_results}
        for stage in plan.required_stages:
            stage_result = by_stage.get(stage)
            if stage_result is None:
                reasons.append(f"MISSING_STAGE:{stage.value}")
                continue
            if stage_result.outcome != StageOutcome.PASSED:
                reasons.append(f"STAGE_FAILED:{stage.value}")
            if stage_result.safety_violations:
                reasons.append(f"SAFETY_VIOLATION:{stage.value}")
        final_sample = max((item.sample_size for item in result.stage_results), default=0)
        if final_sample < plan.minimum_sample_size:
            reasons.append("MINIMUM_SAMPLE_NOT_MET")
        if result.plan_id != plan.plan_id:
            reasons.append("RESULT_PLAN_MISMATCH")
        return PromotionDecision(
            eligible_for_human_approval=not reasons,
            reason_codes=tuple(reasons) or ("HUMAN_APPROVAL_REQUIRED",),
        )


class ReleaseApprovalStatus(StrEnum):
    AWAITING_HUMAN_APPROVAL = "AWAITING_HUMAN_APPROVAL"
    BLOCKED = "BLOCKED"


class ReleaseApprovalDecision(FrozenModel):
    decision_id: str
    evaluation_id: str
    candidate_manifest_id: str
    current_champion_manifest_id: str
    created_at: datetime
    status: ReleaseApprovalStatus
    reason_codes: tuple[str, ...] = Field(min_length=1)

    _utc_created_at = field_validator("created_at")(require_utc)


class ReleaseGate:
    """首阶段只签发人工审批请求；绝不修改 Champion 或部署。"""

    def evaluate(
        self,
        *,
        target: EvaluationTarget,
        evaluation: EvaluationResult,
        current_champion: ReleaseManifest,
        complexity_limit: int,
        created_at: datetime,
    ) -> ReleaseApprovalDecision:
        created_at = require_utc(created_at)
        reasons: list[str] = []
        if evaluation.proposal_id != target.proposal.proposal_id:
            reasons.append("EVALUATION_PROPOSAL_MISMATCH")
        if evaluation.plan_id != target.plan.plan_id:
            reasons.append("EVALUATION_PLAN_MISMATCH")
        if evaluation.candidate_manifest_id != target.candidate.manifest_id:
            reasons.append("EVALUATION_CANDIDATE_MISMATCH")
        if target.plan.base_manifest_id != current_champion.manifest_id:
            reasons.append("CHAMPION_CHANGED_SINCE_PLAN")
        if target.candidate.parent_manifest_id != current_champion.manifest_id:
            reasons.append("CANDIDATE_PARENT_IS_NOT_CHAMPION")
        if target.candidate.constitution_version != current_champion.constitution_version:
            reasons.append("CONSTITUTION_VERSION_MISMATCH")
        if target.candidate.complexity_score > complexity_limit:
            reasons.append("COMPLEXITY_BUDGET_EXCEEDED")
        if evaluation.completed_at > created_at:
            reasons.append("EVALUATION_COMPLETED_AFTER_REQUEST")
        evaluated_stages = tuple(item.stage for item in evaluation.stage_results)
        if evaluated_stages != target.plan.required_stages:
            reasons.append("EVALUATION_STAGE_SET_MISMATCH")
        fixed_regression = next(
            (
                item
                for item in evaluation.stage_results
                if item.stage == EvaluationStage.FIXED_REGRESSION
            ),
            None,
        )
        if (
            fixed_regression is not None
            and fixed_regression.evidence_set_version != target.plan.fixed_regression_suite_version
        ):
            reasons.append("FIXED_REGRESSION_VERSION_MISMATCH")
        artifact_hashes = {item.artifact_hash for item in evaluation.stage_results}
        if artifact_hashes != {target.artifact_hash}:
            reasons.append("EVALUATION_ARTIFACT_MISMATCH")
        promotion = PromotionGate().evaluate(evaluation, target.plan)
        if not promotion.eligible_for_human_approval:
            reasons.extend(promotion.reason_codes)
        status = (
            ReleaseApprovalStatus.BLOCKED
            if reasons
            else ReleaseApprovalStatus.AWAITING_HUMAN_APPROVAL
        )
        normalized_reasons = tuple(dict.fromkeys(reasons)) or ("HUMAN_APPROVAL_REQUIRED",)
        decision_id = stable_id(
            "release_approval",
            evaluation.evaluation_id,
            target.candidate.manifest_id,
            current_champion.manifest_id,
            status.value,
            normalized_reasons,
        )
        return ReleaseApprovalDecision(
            decision_id=decision_id,
            evaluation_id=evaluation.evaluation_id,
            candidate_manifest_id=target.candidate.manifest_id,
            current_champion_manifest_id=current_champion.manifest_id,
            created_at=created_at,
            status=status,
            reason_codes=normalized_reasons,
        )


def build_governance_snapshot(
    *,
    as_of: datetime,
    constitution: SystemConstitution,
    champion: ReleaseManifest,
    previous_stable_manifest_ids: tuple[str, ...] = (),
    metric_summaries: tuple[tuple[str, str], ...] = (),
    failed_experiments: tuple[FailedExperiment, ...] = (),
    open_proposal_ids: tuple[str, ...] = (),
    available_evaluation_plans: tuple[EvaluationPlan, ...] = (),
    architecture_decision_ids: tuple[str, ...] = (),
    analysis_trigger_plans: tuple[AnalysisTriggerPlan, ...] = (),
    complexity_used: int = 0,
    complexity_limit: int = 10,
) -> GovernanceSnapshot:
    payload = {
        "as_of": as_of.isoformat(),
        "constitution": constitution.model_dump(mode="json"),
        "champion": champion.model_dump(mode="json"),
        "previous_stable_manifest_ids": previous_stable_manifest_ids,
        "metric_summaries": metric_summaries,
        "failed_experiments": [item.model_dump(mode="json") for item in failed_experiments],
        "open_proposal_ids": open_proposal_ids,
        "available_evaluation_plans": [
            item.model_dump(mode="json") for item in available_evaluation_plans
        ],
        "architecture_decision_ids": architecture_decision_ids,
        "analysis_trigger_plans": [item.model_dump(mode="json") for item in analysis_trigger_plans],
        "complexity_used": complexity_used,
        "complexity_limit": complexity_limit,
    }
    digest = content_hash(payload)
    return GovernanceSnapshot(
        snapshot_id=stable_id("governance", as_of, champion.manifest_id, digest),
        as_of=as_of,
        constitution=constitution,
        champion=champion,
        previous_stable_manifest_ids=previous_stable_manifest_ids,
        metric_summaries=metric_summaries,
        failed_experiments=failed_experiments,
        open_proposal_ids=open_proposal_ids,
        available_evaluation_plans=available_evaluation_plans,
        architecture_decision_ids=architecture_decision_ids,
        analysis_trigger_plans=analysis_trigger_plans,
        complexity_used=complexity_used,
        complexity_limit=complexity_limit,
        content_hash=digest,
    )


def load_constitution(path: str | Path) -> SystemConstitution:
    with Path(path).open("r", encoding="utf-8") as handle:
        return SystemConstitution.model_validate(yaml.safe_load(handle))


def load_regression_suite(path: str | Path) -> RegressionSuite:
    with Path(path).open("r", encoding="utf-8") as handle:
        return RegressionSuite.model_validate(yaml.safe_load(handle))


def load_release_manifest(path: str | Path) -> ReleaseManifest:
    with Path(path).open("r", encoding="utf-8") as handle:
        return ReleaseManifest.model_validate(yaml.safe_load(handle))


_CONFIG_COMPONENT_NAMES = (
    "feature",
    "decision_state",
    "capital",
    "outcome_evaluation",
    "trigger",
    "temporal",
    "market_data",
    "shadow",
    "information",
    "pipeline",
    "codex_runtime",
    "assessment",
    "codex_accounts",
    "binance_testnet",
    "governance",
)


def validate_manifest_component_versions(
    manifest: ReleaseManifest,
    config: AppConfig,
) -> None:
    """Compare stable behavior versions without reinterpreting an old config hash."""

    declared = dict(manifest.component_versions)
    current = {name: getattr(config, name).version for name in _CONFIG_COMPONENT_NAMES}
    if declared != current:
        raise ValueError("ReleaseManifest 与当前类型化行为配置版本不一致")


def validate_manifest_against_config(
    manifest: ReleaseManifest,
    config: AppConfig,
    *,
    require_configuration_hash: bool = False,
) -> None:
    validate_manifest_component_versions(manifest, config)
    if manifest.configuration_hash is None:
        if require_configuration_hash:
            raise ValueError("运行 ReleaseManifest 缺少完整配置哈希")
        return
    if manifest.configuration_hash != content_hash(config):
        raise ValueError("ReleaseManifest 与当前完整配置内容不一致")


def validate_manifest_code_version(
    manifest: ReleaseManifest,
    *,
    repository_root: Path | None = None,
) -> Path:
    """Fail closed unless runtime imports come from the exact clean release commit."""

    root = (repository_root or _source_repository_root()).resolve()
    current_clean_code_version(
        repository_root=root,
        expected_version=manifest.code_version,
    )
    return root


def validate_runtime_release_checkout(repository_root: Path) -> None:
    """Runtime must import from a detached release checkout, never the dev tree."""

    completed = subprocess.run(
        ("git", "-C", str(repository_root), "symbolic-ref", "-q", "HEAD"),
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if completed.returncode == 0:
        raise ValueError("运行服务必须从 detached 的冻结 Release checkout 启动")
    if completed.returncode not in {1}:
        raise ValueError("无法确认 Release checkout 是否冻结")


def validate_manifest_artifacts(
    manifest: ReleaseManifest,
    *,
    repository_root: Path,
    required_ids: tuple[str, ...] = (),
) -> None:
    by_id = {item.artifact_id: item for item in manifest.artifacts}
    missing = set(required_ids) - set(by_id)
    if missing:
        raise ValueError(f"ReleaseManifest 缺少必需制品：{', '.join(sorted(missing))}")
    for artifact in manifest.artifacts:
        path = (repository_root / artifact.relative_path).resolve()
        try:
            path.relative_to(repository_root)
        except ValueError as exc:
            raise ValueError("ReleaseArtifact 解析到仓库目录之外") from exc
        observed = _artifact_sha256(path)
        if observed != artifact.sha256:
            raise ValueError(
                f"ReleaseArtifact 内容不一致：{artifact.artifact_id}"
            )


def resolve_manifest_artifact(
    manifest: ReleaseManifest,
    artifact_id: str,
    *,
    repository_root: Path | None = None,
) -> Path:
    root = (repository_root or _source_repository_root()).resolve()
    artifact = next(
        (item for item in manifest.artifacts if item.artifact_id == artifact_id),
        None,
    )
    if artifact is None:
        raise ValueError(f"ReleaseManifest 缺少制品：{artifact_id}")
    path = (root / artifact.relative_path).resolve()
    validate_manifest_artifacts(
        manifest.model_copy(update={"artifacts": (artifact,)}),
        repository_root=root,
        required_ids=(artifact_id,),
    )
    return path


def _artifact_sha256(path: Path) -> str:
    if path.is_symlink() or not path.exists():
        raise ValueError(f"ReleaseArtifact 不存在或为符号链接：{path}")
    digest = hashlib.sha256()
    if path.is_file():
        digest.update(path.read_bytes())
        return digest.hexdigest()
    if not path.is_dir():
        raise ValueError(f"ReleaseArtifact 类型不受支持：{path}")
    files = tuple(
        sorted(
            item
            for item in path.rglob("*")
            if item.is_file() and not item.is_symlink()
        )
    )
    if any(item.is_symlink() for item in path.rglob("*")):
        raise ValueError(f"ReleaseArtifact 目录不得包含符号链接：{path}")
    for item in files:
        relative = item.relative_to(path).as_posix().encode("utf-8")
        payload = item.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def current_clean_code_version(
    *,
    repository_root: Path | None = None,
    expected_version: str | None = None,
) -> str:
    """Return the exact commit only when all runtime-bearing paths are clean."""

    root = (repository_root or _source_repository_root()).resolve()
    head = _git_output(root, "rev-parse", "HEAD")
    if expected_version is not None and head != expected_version:
        raise ValueError(
            "ReleaseManifest 代码版本与实际运行源码不一致："
            f"expected={expected_version}, observed={head}"
        )
    dirty = _git_output(
        root,
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--",
        "src",
        "config",
        "migrations",
        "pyproject.toml",
        "web/src",
        "web/package.json",
        "web/package-lock.json",
    )
    if dirty:
        raise ValueError("ReleaseManifest 对应的运行源码存在未提交变更")
    return head


def committed_file_revision(
    path: Path,
    *,
    repository_root: Path | None = None,
) -> tuple[str, datetime]:
    """Return the commit that froze the exact current bytes of one tracked file."""

    root = (repository_root or _source_repository_root()).resolve()
    target = path.resolve()
    try:
        relative = target.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("预登记文件必须位于治理源码仓库内") from exc
    dirty = _git_output(
        root,
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--",
        relative,
    )
    if dirty:
        raise ValueError("预登记文件尚未提交或存在未提交变更")
    commit = _git_output(root, "log", "-1", "--format=%H", "--", relative)
    if not commit:
        raise ValueError("预登记文件没有可验证的提交历史")
    committed_blob = _git_output(root, "rev-parse", f"{commit}:{relative}")
    current_blob = _git_output(root, "hash-object", relative)
    if committed_blob != current_blob:
        raise ValueError("预登记文件内容与登记提交不一致")
    committed_at = datetime.fromisoformat(
        _git_output(root, "show", "-s", "--format=%cI", commit)
    )
    return commit, require_utc(committed_at)


def _source_repository_root() -> Path:
    source = Path(__file__).resolve()
    for candidate in source.parents:
        if (candidate / ".git").exists():
            return candidate
    raise ValueError("无法定位运行源码的 Git 仓库；拒绝接受未验证代码版本")


def _git_output(root: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ("git", "-C", str(root), *arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("无法验证 ReleaseManifest 代码版本") from exc
    if completed.returncode != 0:
        raise ValueError("无法验证 ReleaseManifest 代码版本")
    return completed.stdout.strip()
