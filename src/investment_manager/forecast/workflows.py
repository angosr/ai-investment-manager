from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pydantic import field_validator, model_validator
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from investment_manager.forecast.application import (
    AssessmentCommand,
    AssessmentWorkflowExecution,
    AssessmentWorkflowStatus,
)
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.platform.orchestration import OrchestrationPolicySnapshot
from investment_manager.platform.temporal import default_activity_versioning_intent

ASSESSMENT_ACTIVITY_NAME = "execute-context-assessment-v1"
ASSESSMENT_WORKFLOW_NAME = "ContextAssessmentWorkflow"


class AssessmentWorkflowRequest(FrozenModel):
    workflow_id: str
    command: AssessmentCommand
    orchestration: OrchestrationPolicySnapshot
    created_at: datetime
    deadline: datetime
    input_hash: str

    _utc_created_at = field_validator("created_at")(require_utc)
    _utc_deadline = field_validator("deadline")(require_utc)

    @classmethod
    def create(
        cls,
        *,
        command: AssessmentCommand,
        orchestration: OrchestrationPolicySnapshot,
        created_at: datetime,
        deadline: datetime,
    ) -> AssessmentWorkflowRequest:
        payload = {
            "command": command,
            "orchestration": orchestration,
            "created_at": require_utc(created_at),
            "deadline": require_utc(deadline),
        }
        digest = content_hash(payload)
        return cls(
            workflow_id=stable_id("context_assessment_workflow", command.command_id),
            input_hash=digest,
            **payload,
        )

    @model_validator(mode="after")
    def identity_and_deadline_must_be_valid(self):
        if self.deadline <= self.created_at:
            raise ValueError("ContextAssessment deadline 必须晚于 created_at")
        expected_hash = content_hash(
            self.model_dump(mode="json", exclude={"workflow_id", "input_hash"})
        )
        if self.input_hash != expected_hash:
            raise ValueError("ContextAssessment Workflow input_hash 与内容不一致")
        expected_id = stable_id(
            "context_assessment_workflow",
            self.command.command_id,
        )
        if self.workflow_id != expected_id:
            raise ValueError("ContextAssessment workflow_id 与 command 不一致")
        return self


@workflow.defn(name=ASSESSMENT_WORKFLOW_NAME)
class ContextAssessmentWorkflow:
    """Retry infrastructure failures around one immutable, non-trading assessment."""

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
            return _infrastructure_failure(workflow_id, "INVALID_WORKFLOW_INPUT")
        self._input_hash = raw_input_hash
        try:
            deadline, start_to_close, schedule_to_close, retry = _activity_options(
                request
            )
        except (KeyError, TypeError, ValueError):
            return _infrastructure_failure(workflow_id, "INVALID_WORKFLOW_INPUT")
        remaining = deadline - workflow.now()
        if remaining <= timedelta(0):
            return _infrastructure_failure(workflow_id, "ASSESSMENT_DEADLINE_EXPIRED")
        try:
            raw_result = await workflow.execute_activity(
                ASSESSMENT_ACTIVITY_NAME,
                request,
                result_type=dict,
                start_to_close_timeout=start_to_close,
                schedule_to_close_timeout=min(schedule_to_close, remaining),
                retry_policy=retry,
                versioning_intent=default_activity_versioning_intent(),
                summary="执行冻结上下文研判",
            )
        except ActivityError:
            return _infrastructure_failure(workflow_id, "ASSESSMENT_ACTIVITY_FAILED")
        if not isinstance(raw_result, dict):
            return _infrastructure_failure(workflow_id, "INVALID_ACTIVITY_RESULT")
        execution = raw_result.get("execution")
        attempt = raw_result.get("attempt")
        if not isinstance(execution, dict) or not isinstance(attempt, int) or attempt < 1:
            return _infrastructure_failure(workflow_id, "INVALID_ACTIVITY_RESULT")
        succeeded = execution.get("status") == "SUCCEEDED"
        result = AssessmentWorkflowExecution(
            workflow_id=workflow_id,
            status=(
                AssessmentWorkflowStatus.SUCCEEDED
                if succeeded
                else AssessmentWorkflowStatus.NO_ASSESSMENT
            ),
            reason_code=str(execution.get("reason_code") or "UNKNOWN"),
            attempt=attempt,
            execution=execution,
        )
        return result.model_dump(mode="json")


def _activity_options(
    request: dict[str, Any],
) -> tuple[datetime, timedelta, timedelta, RetryPolicy]:
    deadline = require_utc(datetime.fromisoformat(str(request["deadline"])))
    policy = request["orchestration"]
    start_to_close = timedelta(seconds=int(policy["activity_start_to_close_seconds"]))
    schedule_to_close = timedelta(
        seconds=int(policy["activity_schedule_to_close_seconds"])
    )
    retry = RetryPolicy(
        initial_interval=timedelta(seconds=int(policy["retry_initial_seconds"])),
        maximum_interval=timedelta(seconds=int(policy["retry_maximum_seconds"])),
        backoff_coefficient=float(policy["retry_backoff_coefficient"]),
        maximum_attempts=int(policy["retry_maximum_attempts"]),
        non_retryable_error_types=["InvalidWorkflowInput", "PermanentDomainError"],
    )
    return deadline, start_to_close, schedule_to_close, retry


def _infrastructure_failure(workflow_id: str, reason_code: str) -> dict[str, Any]:
    return AssessmentWorkflowExecution(
        workflow_id=workflow_id,
        status=AssessmentWorkflowStatus.FAILED,
        reason_code=reason_code,
        attempt=0,
    ).model_dump(mode="json")
