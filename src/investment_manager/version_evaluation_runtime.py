from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import ValidationError, model_validator
from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

from investment_manager.config import TemporalPolicy
from investment_manager.domain import FrozenModel, _require_utc
from investment_manager.governance import (
    EvaluationResult,
    EvaluationStage,
    EvaluationTarget,
    StageResult,
    build_evaluation_result,
)
from investment_manager.ids import content_hash, stable_id
from investment_manager.persistence import SqlGovernanceRepository
from investment_manager.temporal_worker import SingleActivityWorker
from investment_manager.version_evaluation_workflows import (
    FINALIZE_EVALUATION_ACTIVITY,
    RUN_EVALUATION_STAGE_ACTIVITY,
    VersionEvaluationWorkflow,
)
from investment_manager.workflow import OrchestrationPolicySnapshot


class EvaluationStageRunner(Protocol):
    def run(self, stage: EvaluationStage, target: EvaluationTarget) -> StageResult: ...


class VersionEvaluationWorkflowRequest(FrozenModel):
    workflow_id: str
    target: EvaluationTarget
    orchestration: OrchestrationPolicySnapshot
    input_hash: str

    @model_validator(mode="after")
    def identity_must_match(self):
        if self.input_hash != content_hash(_request_identity(self)):
            raise ValueError("Version Evaluation Workflow input_hash 不一致")
        expected_id = stable_id(
            "version_evaluation_workflow",
            self.target.proposal.proposal_id,
            self.target.plan.plan_id,
            self.target.candidate.manifest_id,
            self.target.artifact_hash,
        )
        if self.workflow_id != expected_id:
            raise ValueError("Version Evaluation Workflow workflow_id 不一致")
        return self


class VersionEvaluationWorkflowStatus(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class VersionEvaluationWorkflowExecution(FrozenModel):
    workflow_id: str
    status: VersionEvaluationWorkflowStatus
    reason_code: str
    evaluation: EvaluationResult | None = None


def _request_identity(request: VersionEvaluationWorkflowRequest) -> dict[str, Any]:
    return {
        "target": request.target.model_dump(mode="json"),
        "orchestration": request.orchestration.model_dump(mode="json"),
    }


def build_version_evaluation_workflow_request(
    *,
    target: EvaluationTarget,
    temporal_policy: TemporalPolicy,
) -> VersionEvaluationWorkflowRequest:
    orchestration = OrchestrationPolicySnapshot.from_config(temporal_policy)
    payload = {
        "target": target.model_dump(mode="json"),
        "orchestration": orchestration.model_dump(mode="json"),
    }
    return VersionEvaluationWorkflowRequest(
        workflow_id=stable_id(
            "version_evaluation_workflow",
            target.proposal.proposal_id,
            target.plan.plan_id,
            target.candidate.manifest_id,
            target.artifact_hash,
        ),
        target=target,
        orchestration=orchestration,
        input_hash=content_hash(payload),
    )


@dataclass(slots=True)
class VersionEvaluationActivities:
    runner: EvaluationStageRunner
    repository: SqlGovernanceRepository

    @activity.defn(name=RUN_EVALUATION_STAGE_ACTIVITY)
    def run_stage(self, raw: dict[str, Any]) -> dict[str, Any]:
        try:
            request = VersionEvaluationWorkflowRequest.model_validate(raw["request"])
            stage = EvaluationStage(raw["stage"])
        except (KeyError, ValueError, ValidationError) as exc:
            raise ApplicationError(
                "版本评估输入未通过契约校验",
                type="InvalidEvaluationInput",
                non_retryable=True,
            ) from exc
        if stage not in request.target.plan.required_stages:
            raise ApplicationError(
                "评估阶段不在预登记计划内",
                type="InvalidEvaluationInput",
                non_retryable=True,
            )
        try:
            self.repository.require_registered_evaluation_target(request.target)
        except ValueError as exc:
            raise ApplicationError(
                str(exc),
                type="InvalidEvaluationInput",
                non_retryable=True,
            ) from exc
        result = self.runner.run(stage, request.target)
        if result.stage != stage or result.artifact_hash != request.target.artifact_hash:
            raise ApplicationError(
                "StageRunner 返回了其他阶段或候选制品",
                type="InvalidEvaluationInput",
                non_retryable=True,
            )
        if (
            stage == EvaluationStage.FIXED_REGRESSION
            and result.evidence_set_version != request.target.plan.fixed_regression_suite_version
        ):
            raise ApplicationError(
                "固定回归结果使用了非预登记回归集",
                type="InvalidEvaluationInput",
                non_retryable=True,
            )
        return {"stage_result": result.model_dump(mode="json")}

    @activity.defn(name=FINALIZE_EVALUATION_ACTIVITY)
    def finalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        try:
            request = VersionEvaluationWorkflowRequest.model_validate(raw["request"])
            results = tuple(StageResult.model_validate(item) for item in raw["stage_results"])
            completed_at = _require_utc(datetime.fromisoformat(raw["completed_at"]))
        except (KeyError, ValueError, ValidationError) as exc:
            raise ApplicationError(
                "版本评估结果未通过契约校验",
                type="InvalidEvaluationInput",
                non_retryable=True,
            ) from exc
        try:
            self.repository.require_registered_evaluation_target(request.target)
        except ValueError as exc:
            raise ApplicationError(
                str(exc),
                type="InvalidEvaluationInput",
                non_retryable=True,
            ) from exc
        evaluation = build_evaluation_result(
            target=request.target,
            completed_at=completed_at,
            stage_results=results,
        )
        self.repository.record_evaluation(evaluation)
        return {"evaluation": evaluation.model_dump(mode="json")}


@dataclass(slots=True)
class VersionEvaluationTemporalCoordinator:
    client: Client
    policy: TemporalPolicy

    async def execute(
        self, request: VersionEvaluationWorkflowRequest
    ) -> VersionEvaluationWorkflowExecution:
        try:
            raw = await self.client.execute_workflow(
                VersionEvaluationWorkflow.run,
                request.model_dump(mode="json"),
                id=request.workflow_id,
                task_queue=self.policy.version_evaluation_task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            handle = self.client.get_workflow_handle(request.workflow_id)
            existing_hash = await handle.query(VersionEvaluationWorkflow.input_hash)
            if existing_hash != request.input_hash:
                raise ValueError("相同版本评估 Workflow 的冻结输入不同") from None
            raw = await handle.result()
        return VersionEvaluationWorkflowExecution.model_validate(raw)


class VersionEvaluationTemporalWorker(SingleActivityWorker):
    def __init__(
        self,
        client: Client,
        policy: TemporalPolicy,
        activities: VersionEvaluationActivities,
    ) -> None:
        super().__init__(
            client,
            task_queue=policy.version_evaluation_task_queue,
            workflows=[VersionEvaluationWorkflow],
            activities=[activities.run_stage, activities.finalize],
            thread_name_prefix="version-evaluation-activity",
        )
