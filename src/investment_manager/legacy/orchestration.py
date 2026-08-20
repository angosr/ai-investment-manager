from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.legacy.cycle import CycleInput, CycleResult
from investment_manager.platform.orchestration import OrchestrationPolicySnapshot
from investment_manager.scheduling.models import TriggerDecision
from investment_manager.scheduling.policy import TemporalPolicy


class WorkflowRequest(FrozenModel):
    workflow_id: str
    cycle_input: CycleInput
    trigger: TriggerDecision
    orchestration: OrchestrationPolicySnapshot
    created_at: datetime
    deadline: datetime
    input_hash: str

    _utc_created_at = field_validator("created_at")(require_utc)
    _utc_deadline = field_validator("deadline")(require_utc)

    @model_validator(mode="after")
    def identity_must_match_frozen_input(self):
        if self.deadline < self.created_at:
            raise ValueError("Workflow deadline 不得早于 created_at")
        expected_hash = content_hash(_identity_payload(self))
        if self.input_hash != expected_hash:
            raise ValueError("Workflow input_hash 与冻结输入不一致")
        expected_id = stable_id("analysis_workflow", self.cycle_input.market.cycle_id)
        if self.workflow_id != expected_id:
            raise ValueError("Workflow workflow_id 与冻结输入不一致")
        return self


class WorkflowExecutionStatus(StrEnum):
    COMPLETED = "COMPLETED"
    NO_TRADE = "NO_TRADE"


class WorkflowExecution(FrozenModel):
    workflow_id: str
    status: WorkflowExecutionStatus
    reason_code: str
    attempt: int = Field(ge=0)
    cycle_result: CycleResult | None = None

    @model_validator(mode="after")
    def result_must_match_status(self):
        if self.status == WorkflowExecutionStatus.COMPLETED and self.cycle_result is None:
            raise ValueError("COMPLETED Workflow 必须包含 cycle_result")
        if self.status == WorkflowExecutionStatus.NO_TRADE and self.cycle_result is not None:
            raise ValueError("NO_TRADE Workflow 不得包含 cycle_result")
        return self


def _identity_payload(request: WorkflowRequest) -> dict[str, object]:
    return {
        "cycle_input": request.cycle_input.model_dump(mode="json"),
        "trigger": request.trigger.model_dump(mode="json"),
        "orchestration": request.orchestration.model_dump(mode="json"),
        "created_at": request.created_at.isoformat(),
        "deadline": request.deadline.isoformat(),
    }


def build_workflow_request(
    *,
    cycle_input: CycleInput,
    trigger: TriggerDecision,
    temporal_policy: TemporalPolicy,
    created_at: datetime,
    deadline: datetime,
) -> WorkflowRequest:
    created_at = require_utc(created_at)
    deadline = require_utc(deadline)
    orchestration = OrchestrationPolicySnapshot.from_config(temporal_policy)
    payload = {
        "cycle_input": cycle_input.model_dump(mode="json"),
        "trigger": trigger.model_dump(mode="json"),
        "orchestration": orchestration.model_dump(mode="json"),
        "created_at": created_at.isoformat(),
        "deadline": deadline.isoformat(),
    }
    digest = content_hash(payload)
    return WorkflowRequest(
        workflow_id=stable_id("analysis_workflow", cycle_input.market.cycle_id),
        cycle_input=cycle_input,
        trigger=trigger,
        orchestration=orchestration,
        created_at=created_at,
        deadline=deadline,
        input_hash=digest,
    )
