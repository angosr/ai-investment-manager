"""Durable orchestration for one immutable joint context posterior."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator, model_validator
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from investment_manager.forecast.context.application import AssessmentCommand
from investment_manager.forecast.context.posterior_contract import ContextPosteriorSeed
from investment_manager.forecast.context.posterior_execution import (
    PosteriorExecution,
    PosteriorExecutionStatus,
)
from investment_manager.forecast.context.workflow import activity_options_from_request
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.platform.orchestration import OrchestrationPolicySnapshot
from investment_manager.platform.temporal import default_activity_versioning_intent

POSTERIOR_ACTIVITY_NAME = "execute-context-assessment-posterior-v1"
POSTERIOR_CLOSE_ACTIVITY_NAME = "close-context-posterior-obligations-v1"
POSTERIOR_WORKFLOW_NAME = "ContextPosteriorWorkflow"


class PosteriorWorkflowRequest(FrozenModel):
    workflow_id: str = Field(min_length=1)
    seed: ContextPosteriorSeed
    assessment_command: AssessmentCommand
    producer_behavior_id: str = Field(min_length=1)
    orchestration: OrchestrationPolicySnapshot
    created_at: datetime
    deadline: datetime
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    _utc_created_at = field_validator("created_at")(require_utc)
    _utc_deadline = field_validator("deadline")(require_utc)

    @classmethod
    def create(
        cls,
        *,
        seed: ContextPosteriorSeed,
        assessment_command: AssessmentCommand,
        producer_behavior_id: str,
        orchestration: OrchestrationPolicySnapshot,
        created_at: datetime,
    ) -> PosteriorWorkflowRequest:
        deadline = min(item.slot.completion_deadline_at for item in seed.targets)
        payload = {
            "seed": seed,
            "assessment_command": assessment_command,
            "producer_behavior_id": producer_behavior_id,
            "orchestration": orchestration,
            "created_at": require_utc(created_at),
            "deadline": deadline,
        }
        digest = content_hash(payload)
        return cls(
            workflow_id=stable_id(
                "context_posterior_workflow",
                seed.seed_id,
                producer_behavior_id,
            ),
            input_hash=digest,
            **payload,
        )

    @model_validator(mode="after")
    def identity_and_deadline_are_canonical(self):
        expected_deadline = min(
            item.slot.completion_deadline_at for item in self.seed.targets
        )
        if self.deadline != expected_deadline or self.created_at >= self.deadline:
            raise ValueError("Posterior Workflow deadline 与冻结槽不一致")
        if self.assessment_command.packet.as_of != self.seed.information_cutoff_at:
            raise ValueError("Posterior Workflow 的 WorldModel Packet 与 seed 截止不一致")
        expected_hash = content_hash(
            self.model_dump(mode="json", exclude={"workflow_id", "input_hash"})
        )
        if self.input_hash != expected_hash:
            raise ValueError("Posterior Workflow input_hash 与内容不一致")
        expected_id = stable_id(
            "context_posterior_workflow",
            self.seed.seed_id,
            self.producer_behavior_id,
        )
        if self.workflow_id != expected_id:
            raise ValueError("Posterior workflow_id 与冻结输入/行为不一致")
        return self


class PosteriorWorkflowStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    NO_ESTIMATE = "NO_ESTIMATE"
    FAILED = "FAILED"


class PosteriorWorkflowExecution(FrozenModel):
    workflow_id: str = Field(min_length=1)
    status: PosteriorWorkflowStatus
    reason_code: str = Field(min_length=1)
    attempt: int = Field(ge=0)
    execution: PosteriorExecution | None = None

    @model_validator(mode="after")
    def terminal_shape_is_unambiguous(self):
        if self.status == PosteriorWorkflowStatus.SUCCEEDED:
            if (
                self.execution is None
                or self.execution.status != PosteriorExecutionStatus.SUCCEEDED
            ):
                raise ValueError("成功 Posterior Workflow 必须包含成功执行")
        elif self.status == PosteriorWorkflowStatus.NO_ESTIMATE:
            if (
                self.execution is None
                or self.execution.status != PosteriorExecutionStatus.NO_ESTIMATE
            ):
                raise ValueError("NO_ESTIMATE Workflow 必须包含缺失结果")
        elif self.execution is not None:
            raise ValueError("基础设施失败不得伪装成 Posterior 业务结果")
        return self


@workflow.defn(name=POSTERIOR_WORKFLOW_NAME)
class ContextPosteriorWorkflow:
    def __init__(self) -> None:
        self._input_hash: str | None = None

    @workflow.query
    def input_hash(self) -> str | None:
        return self._input_hash

    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        workflow_id = str(request.get("workflow_id") or workflow.info().workflow_id)
        raw_input_hash = request.get("input_hash")
        if not isinstance(raw_input_hash, str) or not raw_input_hash:
            return _failure(workflow_id, "INVALID_WORKFLOW_INPUT")
        self._input_hash = raw_input_hash
        try:
            deadline, start_to_close, schedule_to_close, retry = activity_options_from_request(
                request
            )
        except (KeyError, TypeError, ValueError):
            return _failure(workflow_id, "INVALID_WORKFLOW_INPUT")
        remaining = deadline - workflow.now()
        if remaining <= timedelta(0):
            return await _close_obligations(
                request,
                workflow_id=workflow_id,
                reason_code="POSTERIOR_DEADLINE_EXPIRED",
            )
        try:
            raw_result = await workflow.execute_activity(
                POSTERIOR_ACTIVITY_NAME,
                request,
                result_type=dict,
                start_to_close_timeout=start_to_close,
                schedule_to_close_timeout=min(schedule_to_close, remaining),
                retry_policy=retry,
                versioning_intent=default_activity_versioning_intent(),
                summary="依次执行同截止世界认知与后验预测",
            )
        except ActivityError:
            return await _close_obligations(
                request,
                workflow_id=workflow_id,
                reason_code="POSTERIOR_ACTIVITY_FAILED",
            )
        if not isinstance(raw_result, dict):
            return await _close_obligations(
                request,
                workflow_id=workflow_id,
                reason_code="INVALID_ACTIVITY_RESULT",
            )
        raw_execution = raw_result.get("execution")
        attempt = raw_result.get("attempt")
        if not isinstance(raw_execution, dict) or not isinstance(attempt, int) or attempt < 1:
            return await _close_obligations(
                request,
                workflow_id=workflow_id,
                reason_code="INVALID_ACTIVITY_RESULT",
            )
        try:
            execution = PosteriorExecution.model_validate(raw_execution)
        except ValueError:
            return await _close_obligations(
                request,
                workflow_id=workflow_id,
                reason_code="INVALID_ACTIVITY_RESULT",
            )
        status = (
            PosteriorWorkflowStatus.SUCCEEDED
            if execution.status == PosteriorExecutionStatus.SUCCEEDED
            else PosteriorWorkflowStatus.NO_ESTIMATE
        )
        return PosteriorWorkflowExecution(
            workflow_id=workflow_id,
            status=status,
            reason_code=execution.reason_code,
            attempt=attempt,
            execution=execution,
        ).model_dump(mode="json")


async def _close_obligations(
    request: dict[str, Any],
    *,
    workflow_id: str,
    reason_code: str,
) -> dict[str, Any]:
    """Keep a registered Forecast duty visible until its idempotent terminal write succeeds."""

    policy = request.get("orchestration")
    if not isinstance(policy, dict):
        return _failure(workflow_id, reason_code)
    retry = RetryPolicy(
        initial_interval=timedelta(seconds=int(policy["retry_initial_seconds"])),
        maximum_interval=timedelta(seconds=int(policy["retry_maximum_seconds"])),
        backoff_coefficient=float(policy["retry_backoff_coefficient"]),
        non_retryable_error_types=["InvalidWorkflowInput", "PermanentDomainError"],
    )
    try:
        completed_at = max(
            workflow.now(),
            require_utc(datetime.fromisoformat(str(request["created_at"]))),
        )
        raw_result = await workflow.execute_activity(
            POSTERIOR_CLOSE_ACTIVITY_NAME,
            {
                "request": request,
                "completed_at": completed_at.isoformat(),
                "reason_code": reason_code,
            },
            result_type=dict,
            start_to_close_timeout=timedelta(
                seconds=min(60, int(policy["activity_start_to_close_seconds"]))
            ),
            retry_policy=retry,
            versioning_intent=default_activity_versioning_intent(),
            summary="闭合未完成的世界认知后验义务",
        )
    except (ActivityError, KeyError, TypeError, ValueError):
        return _failure(workflow_id, reason_code)
    if not isinstance(raw_result, dict):
        return _failure(workflow_id, reason_code)
    raw_execution = raw_result.get("execution")
    attempt = raw_result.get("attempt")
    if not isinstance(raw_execution, dict) or not isinstance(attempt, int) or attempt < 1:
        return _failure(workflow_id, reason_code)
    execution = PosteriorExecution.model_validate(raw_execution)
    if execution.status != PosteriorExecutionStatus.NO_ESTIMATE:
        return _failure(workflow_id, reason_code)
    return PosteriorWorkflowExecution(
        workflow_id=workflow_id,
        status=PosteriorWorkflowStatus.NO_ESTIMATE,
        reason_code=execution.reason_code,
        attempt=attempt,
        execution=execution,
    ).model_dump(mode="json")


def _failure(workflow_id: str, reason_code: str) -> dict[str, Any]:
    return PosteriorWorkflowExecution(
        workflow_id=workflow_id,
        status=PosteriorWorkflowStatus.FAILED,
        reason_code=reason_code,
        attempt=0,
    ).model_dump(mode="json")
