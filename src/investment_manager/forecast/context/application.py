from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from investment_manager.forecast.context.executor import (
    AssessmentExecution,
    AssessmentExecutionStatus,
    ContextAssessmentExecutor,
)
from investment_manager.kernel.identity import SHA256_PATTERN, content_hash, stable_id
from investment_manager.kernel.types import FrozenModel
from investment_manager.state.decision.packet import DecisionPacket

ASSESSMENT_COMMAND_VERSION = "context-assessment-command-v1"


class AssessmentCommand(FrozenModel):
    """Frozen handoff from point-in-time State to one analysis behavior."""

    command_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    packet: DecisionPacket
    analysis_behavior_hash: str = Field(pattern=SHA256_PATTERN)
    command_hash: str = Field(pattern=SHA256_PATTERN)

    @classmethod
    def create(
        cls,
        *,
        packet: DecisionPacket,
        analysis_behavior_hash: str,
    ) -> AssessmentCommand:
        payload = {
            "version": ASSESSMENT_COMMAND_VERSION,
            "packet": packet,
            "analysis_behavior_hash": analysis_behavior_hash,
        }
        digest = content_hash(payload)
        return cls(
            command_id=stable_id("assessment_command", digest),
            command_hash=digest,
            **payload,
        )

    @model_validator(mode="after")
    def identity_must_cover_packet_and_behavior(self):
        expected_hash = content_hash(
            self.model_dump(mode="json", exclude={"command_id", "command_hash"})
        )
        if self.version != ASSESSMENT_COMMAND_VERSION:
            raise ValueError("不支持的 ContextAssessment command 版本")
        if self.command_hash != expected_hash:
            raise ValueError("ContextAssessment command_hash 与内容不一致")
        if self.command_id != stable_id("assessment_command", expected_hash):
            raise ValueError("ContextAssessment command_id 与内容不一致")
        return self


class AssessmentApplication:
    """Execute exactly one frozen Packet/Behavior pair without trading authority."""

    def __init__(self, executor: ContextAssessmentExecutor) -> None:
        self._executor = executor

    def execute(self, command: AssessmentCommand) -> AssessmentExecution:
        return self._executor.execute(
            command.packet,
            expected_behavior_hash=command.analysis_behavior_hash,
        )


class AssessmentWorkflowStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    NO_ASSESSMENT = "NO_ASSESSMENT"
    FAILED = "FAILED"


class AssessmentWorkflowExecution(FrozenModel):
    workflow_id: str = Field(min_length=1)
    status: AssessmentWorkflowStatus
    reason_code: str = Field(min_length=1)
    attempt: int = Field(ge=0)
    execution: AssessmentExecution | None = None

    @model_validator(mode="after")
    def terminal_shape_must_be_unambiguous(self):
        if self.status == AssessmentWorkflowStatus.SUCCEEDED:
            if (
                self.execution is None
                or self.execution.status != AssessmentExecutionStatus.SUCCEEDED
            ):
                raise ValueError("成功 Workflow 必须包含成功的 Assessment execution")
        elif self.status == AssessmentWorkflowStatus.NO_ASSESSMENT:
            if (
                self.execution is None
                or self.execution.status != AssessmentExecutionStatus.FAILED
            ):
                raise ValueError("NO_ASSESSMENT 必须保留分析失败事实")
        elif self.execution is not None:
            raise ValueError("基础设施失败不得伪装成分析结果")
        return self
