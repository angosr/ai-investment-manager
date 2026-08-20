from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import ValidationError, field_validator, model_validator
from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

from investment_manager.config import TemporalPolicy
from investment_manager.governance.models import (
    EvaluationResult,
    EvaluationTarget,
    ReleaseApprovalDecision,
    ReleaseApprovalStatus,
    ReleaseGate,
    ReleaseManifest,
)
from investment_manager.governance.release_workflows import (
    EVALUATE_RELEASE_ACTIVITY,
    ReleaseWorkflow,
)
from investment_manager.governance.repository import SqlGovernanceRepository
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.temporal_worker import SingleActivityWorker
from investment_manager.workflow import OrchestrationPolicySnapshot


class ReleaseWorkflowRequest(FrozenModel):
    workflow_id: str
    target: EvaluationTarget
    evaluation: EvaluationResult
    current_champion: ReleaseManifest
    complexity_limit: int
    requested_at: datetime
    orchestration: OrchestrationPolicySnapshot
    input_hash: str

    _utc_requested_at = field_validator("requested_at")(require_utc)

    @model_validator(mode="after")
    def identity_and_frozen_relationships_must_match(self):
        if self.complexity_limit < 0:
            raise ValueError("复杂度上限不得为负数")
        if self.current_champion.status != "CHAMPION":
            raise ValueError("ReleaseWorkflow 当前版本必须是 CHAMPION")
        if self.input_hash != content_hash(_request_identity(self)):
            raise ValueError("ReleaseWorkflow input_hash 不一致")
        expected_id = stable_id(
            "release_workflow",
            self.evaluation.evaluation_id,
            self.target.candidate.manifest_id,
            self.current_champion.manifest_id,
        )
        if self.workflow_id != expected_id:
            raise ValueError("ReleaseWorkflow workflow_id 不一致")
        return self


class ReleaseWorkflowStatus(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ReleaseWorkflowExecution(FrozenModel):
    workflow_id: str
    status: ReleaseWorkflowStatus
    reason_code: str
    decision: ReleaseApprovalDecision | None = None


def _request_identity(request: ReleaseWorkflowRequest) -> dict[str, Any]:
    return {
        "target": request.target.model_dump(mode="json"),
        "evaluation": request.evaluation.model_dump(mode="json"),
        "current_champion": request.current_champion.model_dump(mode="json"),
        "complexity_limit": request.complexity_limit,
        "requested_at": request.requested_at.isoformat(),
        "orchestration": request.orchestration.model_dump(mode="json"),
    }


def build_release_workflow_request(
    *,
    target: EvaluationTarget,
    evaluation: EvaluationResult,
    current_champion: ReleaseManifest,
    complexity_limit: int,
    requested_at: datetime,
    temporal_policy: TemporalPolicy,
) -> ReleaseWorkflowRequest:
    requested_at = require_utc(requested_at)
    orchestration = OrchestrationPolicySnapshot.from_config(temporal_policy)
    payload = {
        "target": target.model_dump(mode="json"),
        "evaluation": evaluation.model_dump(mode="json"),
        "current_champion": current_champion.model_dump(mode="json"),
        "complexity_limit": complexity_limit,
        "requested_at": requested_at.isoformat(),
        "orchestration": orchestration.model_dump(mode="json"),
    }
    return ReleaseWorkflowRequest(
        workflow_id=stable_id(
            "release_workflow",
            evaluation.evaluation_id,
            target.candidate.manifest_id,
            current_champion.manifest_id,
        ),
        target=target,
        evaluation=evaluation,
        current_champion=current_champion,
        complexity_limit=complexity_limit,
        requested_at=requested_at,
        orchestration=orchestration,
        input_hash=content_hash(payload),
    )


@dataclass(slots=True)
class ReleaseActivities:
    repository: SqlGovernanceRepository
    gate: ReleaseGate = field(default_factory=ReleaseGate)

    @activity.defn(name=EVALUATE_RELEASE_ACTIVITY)
    def evaluate(self, raw_request: dict[str, Any]) -> dict[str, Any]:
        try:
            request = ReleaseWorkflowRequest.model_validate(raw_request)
        except ValidationError as exc:
            raise ApplicationError(
                "ReleaseWorkflow 输入未通过冻结契约校验",
                type="InvalidReleaseInput",
                non_retryable=True,
            ) from exc
        try:
            self.repository.require_registered_release_inputs(
                target=request.target,
                evaluation=request.evaluation,
                current_champion=request.current_champion,
            )
        except ValueError as exc:
            raise ApplicationError(
                str(exc),
                type="InvalidReleaseInput",
                non_retryable=True,
            ) from exc
        decision = self.gate.evaluate(
            target=request.target,
            evaluation=request.evaluation,
            current_champion=request.current_champion,
            complexity_limit=request.complexity_limit,
            created_at=request.requested_at,
        )
        self.repository.record_release_approval(decision)
        reason_code = (
            "HUMAN_APPROVAL_REQUIRED"
            if decision.status == ReleaseApprovalStatus.AWAITING_HUMAN_APPROVAL
            else "RELEASE_BLOCKED"
        )
        return {
            "reason_code": reason_code,
            "decision": decision.model_dump(mode="json"),
        }


@dataclass(slots=True)
class ReleaseTemporalCoordinator:
    client: Client
    policy: TemporalPolicy

    async def execute(self, request: ReleaseWorkflowRequest) -> ReleaseWorkflowExecution:
        try:
            raw = await self.client.execute_workflow(
                ReleaseWorkflow.run,
                request.model_dump(mode="json"),
                id=request.workflow_id,
                task_queue=self.policy.release_task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            handle = self.client.get_workflow_handle(request.workflow_id)
            existing_hash = await handle.query(ReleaseWorkflow.input_hash)
            if existing_hash != request.input_hash:
                raise ValueError("相同 ReleaseWorkflow 的冻结输入不同") from None
            raw = await handle.result()
        return ReleaseWorkflowExecution.model_validate(raw)


class ReleaseTemporalWorker(SingleActivityWorker):
    def __init__(
        self,
        client: Client,
        policy: TemporalPolicy,
        activities: ReleaseActivities,
    ) -> None:
        super().__init__(
            client,
            task_queue=policy.release_task_queue,
            workflows=[ReleaseWorkflow],
            activities=[activities.evaluate],
            thread_name_prefix="release-activity",
        )
