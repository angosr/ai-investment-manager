from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pydantic import Field, field_validator, model_validator
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from quant_core.assess_execution import (
    AssessmentExecution,
    AssessmentExecutionStatus,
)
from quant_core.config import TemporalPolicy
from quant_core.decision_packet import DecisionPacket
from quant_core.domain import FrozenModel, _require_utc
from quant_core.ids import content_hash, stable_id
from quant_core.temporal_compat import default_activity_versioning_intent
from quant_core.workflow import OrchestrationPolicySnapshot

ASSESS_CONTEXT_ACTIVITY = "assess-context-packet-v1"


class AssessmentWorkflowRequest(FrozenModel):
    workflow_id: str
    packet: DecisionPacket
    analysis_behavior_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    orchestration: OrchestrationPolicySnapshot
    created_at: datetime
    deadline: datetime
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    _utc_created_at = field_validator("created_at")(_require_utc)
    _utc_deadline = field_validator("deadline")(_require_utc)

    @model_validator(mode="after")
    def identity_must_match_frozen_input(self):
        if self.deadline < self.created_at:
            raise ValueError("ASSESS Workflow deadline 不得早于 created_at")
        if self.input_hash != content_hash(_identity_payload(self)):
            raise ValueError("ASSESS Workflow input_hash 与冻结输入不一致")
        expected_id = stable_id(
            "assessment_workflow",
            self.packet.packet_id,
            self.analysis_behavior_hash,
        )
        if self.workflow_id != expected_id:
            raise ValueError("ASSESS Workflow workflow_id 与冻结输入不一致")
        return self


def _identity_payload(request: AssessmentWorkflowRequest) -> dict[str, object]:
    return {
        "packet": request.packet.model_dump(mode="json"),
        "analysis_behavior_hash": request.analysis_behavior_hash,
        "orchestration": request.orchestration.model_dump(mode="json"),
        "created_at": request.created_at.isoformat(),
        "deadline": request.deadline.isoformat(),
    }


def build_assessment_workflow_request(
    *,
    packet: DecisionPacket,
    analysis_behavior_hash: str,
    temporal_policy: TemporalPolicy,
    created_at: datetime,
    deadline: datetime,
) -> AssessmentWorkflowRequest:
    created_at = _require_utc(created_at)
    deadline = _require_utc(deadline)
    orchestration = OrchestrationPolicySnapshot.from_config(temporal_policy)
    payload = {
        "packet": packet.model_dump(mode="json"),
        "analysis_behavior_hash": analysis_behavior_hash,
        "orchestration": orchestration.model_dump(mode="json"),
        "created_at": created_at.isoformat(),
        "deadline": deadline.isoformat(),
    }
    return AssessmentWorkflowRequest(
        workflow_id=stable_id(
            "assessment_workflow",
            packet.packet_id,
            analysis_behavior_hash,
        ),
        packet=packet,
        analysis_behavior_hash=analysis_behavior_hash,
        orchestration=orchestration,
        created_at=created_at,
        deadline=deadline,
        input_hash=content_hash(payload),
    )


@workflow.defn(name="ContextAssessmentWorkflow")
class ContextAssessmentWorkflow:
    """Durably run one frozen Packet/Behavior assessment without trading authority."""

    def __init__(self) -> None:
        self._input_hash: str | None = None

    @workflow.query
    def input_hash(self) -> str | None:
        return self._input_hash

    @workflow.run
    async def run(self, raw_request: dict[str, Any]) -> dict[str, Any]:
        request = AssessmentWorkflowRequest.model_validate(raw_request)
        self._input_hash = request.input_hash
        remaining = request.deadline - workflow.now()
        if remaining <= timedelta(0):
            return _failed(request, "ASSESSMENT_DEADLINE_EXPIRED")

        policy = request.orchestration
        retry_policy = RetryPolicy(
            initial_interval=timedelta(seconds=policy.retry_initial_seconds),
            maximum_interval=timedelta(seconds=policy.retry_maximum_seconds),
            backoff_coefficient=policy.retry_backoff_coefficient,
            maximum_attempts=policy.retry_maximum_attempts,
            non_retryable_error_types=["InvalidWorkflowInput", "PermanentDomainError"],
        )
        try:
            raw_result = await workflow.execute_activity(
                ASSESS_CONTEXT_ACTIVITY,
                request.model_dump(mode="json"),
                result_type=dict,
                start_to_close_timeout=timedelta(
                    seconds=policy.activity_start_to_close_seconds
                ),
                schedule_to_close_timeout=min(
                    timedelta(seconds=policy.activity_schedule_to_close_seconds),
                    remaining,
                ),
                retry_policy=retry_policy,
                versioning_intent=default_activity_versioning_intent(),
                summary="分析冻结资产上下文",
            )
        except ActivityError:
            return _failed(request, "ASSESSMENT_ACTIVITY_FAILED")
        try:
            return AssessmentExecution.model_validate(raw_result).model_dump(mode="json")
        except (TypeError, ValueError):
            return _failed(request, "INVALID_ASSESSMENT_ACTIVITY_RESULT")


def _failed(request: AssessmentWorkflowRequest, reason_code: str) -> dict[str, Any]:
    return AssessmentExecution(
        status=AssessmentExecutionStatus.FAILED,
        packet_id=request.packet.packet_id,
        analysis_behavior_hash=request.analysis_behavior_hash,
        reason_code=reason_code,
    ).model_dump(mode="json")
