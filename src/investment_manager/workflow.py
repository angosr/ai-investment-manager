from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from investment_manager.config import TemporalPolicy
from investment_manager.cycle import CycleInput, CycleResult
from investment_manager.domain import FrozenModel, _require_utc
from investment_manager.ids import content_hash, stable_id
from investment_manager.trigger import TriggerDecision


class OrchestrationPolicySnapshot(FrozenModel):
    """随 Workflow 输入冻结，避免运行中配置漂移改变重放结果。"""

    version: str
    activity_start_to_close_seconds: int = Field(ge=10, le=900)
    activity_schedule_to_close_seconds: int = Field(ge=10, le=1800)
    retry_initial_seconds: int = Field(ge=1, le=60)
    retry_maximum_seconds: int = Field(ge=1, le=300)
    retry_backoff_coefficient: float = Field(ge=1, le=10)
    retry_maximum_attempts: int = Field(ge=1, le=10)

    @model_validator(mode="after")
    def bounds_must_be_consistent(self):
        if self.activity_schedule_to_close_seconds < self.activity_start_to_close_seconds:
            raise ValueError("schedule-to-close 不得短于 start-to-close")
        if self.retry_maximum_seconds < self.retry_initial_seconds:
            raise ValueError("最大重试间隔不得短于初始间隔")
        return self

    @classmethod
    def from_config(cls, policy: TemporalPolicy) -> OrchestrationPolicySnapshot:
        return cls.model_validate(
            policy.model_dump(
                include={
                    "version",
                    "activity_start_to_close_seconds",
                    "activity_schedule_to_close_seconds",
                    "retry_initial_seconds",
                    "retry_maximum_seconds",
                    "retry_backoff_coefficient",
                    "retry_maximum_attempts",
                }
            )
        )


class WorkflowRequest(FrozenModel):
    workflow_id: str
    cycle_input: CycleInput
    trigger: TriggerDecision
    orchestration: OrchestrationPolicySnapshot
    created_at: datetime
    deadline: datetime
    input_hash: str

    _utc_created_at = field_validator("created_at")(_require_utc)
    _utc_deadline = field_validator("deadline")(_require_utc)

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
    created_at = _require_utc(created_at)
    deadline = _require_utc(deadline)
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
