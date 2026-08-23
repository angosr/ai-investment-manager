from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from pydantic import Field, field_validator, model_validator

from investment_manager.forecast.codex.router import AnalystResult
from investment_manager.forecast.models import ContextAssessment
from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.state.decision.packet import DecisionPacket


class ContextAnalyst(Protocol):
    def behavior_hash(self, packet: DecisionPacket) -> str: ...

    def assess(self, packet: DecisionPacket) -> AnalystResult: ...


class ContextAssessmentStore(Protocol):
    def record_packet(self, packet: DecisionPacket) -> DecisionPacket: ...

    def assessment_for(
        self,
        *,
        packet_id: str,
        analysis_behavior_hash: str,
    ) -> ContextAssessment | None: ...

    def record_assessment(
        self,
        packet_id: str,
        assessment: ContextAssessment,
    ) -> ContextAssessment: ...

    def record_execution(self, execution: AssessmentExecution) -> AssessmentExecution: ...


class AssessmentExecutionStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class AssessmentExecution(FrozenModel):
    execution_id: str = Field(min_length=1)
    status: AssessmentExecutionStatus
    packet_id: str
    analysis_behavior_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_at: datetime
    codex_attempts: int = Field(default=0, ge=0)
    usage: tuple[tuple[str, int], ...] = ()
    assessment: ContextAssessment | None = None
    reused_authoritative: bool = False
    reason_code: str
    source_run_id: str | None = None
    account_id: str | None = None

    _utc_completed_at = field_validator("completed_at")(require_utc)

    @classmethod
    def create(cls, **values) -> AssessmentExecution:
        completed_at = require_utc(values.pop("completed_at"))
        usage = tuple(sorted(values.pop("usage", ())))
        identity = stable_id(
            "assessment_execution",
            values["packet_id"],
            values["analysis_behavior_hash"],
            completed_at,
            values.get("source_run_id"),
            values["status"],
            values.get("reused_authoritative", False),
        )
        return cls(
            execution_id=identity,
            completed_at=completed_at,
            usage=usage,
            **values,
        )

    @model_validator(mode="after")
    def result_shape_must_match_status(self):
        if tuple(sorted(set(key for key, _value in self.usage))) != tuple(
            key for key, _value in self.usage
        ):
            raise ValueError("ASSESS execution usage 必须按名称唯一且排序")
        if self.status == AssessmentExecutionStatus.SUCCEEDED:
            if self.assessment is None:
                raise ValueError("成功的 ASSESS execution 必须包含 Assessment")
            if self.assessment.analysis_behavior_hash != self.analysis_behavior_hash:
                raise ValueError("ASSESS execution 行为身份不一致")
        elif self.assessment is not None or self.reused_authoritative:
            raise ValueError("失败的 ASSESS execution 不得携带权威结果")
        expected_id = stable_id(
            "assessment_execution",
            self.packet_id,
            self.analysis_behavior_hash,
            self.completed_at,
            self.source_run_id,
            self.status,
            self.reused_authoritative,
        )
        if self.execution_id != expected_id:
            raise ValueError("ASSESS execution 身份与最终结果不一致")
        return self


class ContextAssessmentExecutor:
    """Crash-safe Packet+Behavior execution boundary around the Codex analyst."""

    def __init__(
        self,
        store: ContextAssessmentStore,
        analyst: ContextAnalyst,
        *,
        on_success: Callable[[ContextAssessment], None] | None = None,
        clock=lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._analyst = analyst
        self._on_success = on_success
        self._clock = clock

    def execute(
        self,
        packet: DecisionPacket,
        *,
        expected_behavior_hash: str | None = None,
    ) -> AssessmentExecution:
        authoritative_packet = self._store.record_packet(packet)
        behavior_hash = self._analyst.behavior_hash(authoritative_packet)
        if (
            expected_behavior_hash is not None
            and behavior_hash != expected_behavior_hash
        ):
            raise ValueError("ContextAssessment runtime 行为身份与冻结 command 不一致")
        existing = self._store.assessment_for(
            packet_id=authoritative_packet.packet_id,
            analysis_behavior_hash=behavior_hash,
        )
        if existing is not None:
            if self._on_success is not None:
                self._on_success(existing)
            execution = AssessmentExecution.create(
                status=AssessmentExecutionStatus.SUCCEEDED,
                packet_id=authoritative_packet.packet_id,
                analysis_behavior_hash=behavior_hash,
                completed_at=max(require_utc(self._clock()), existing.available_at),
                assessment=existing,
                reused_authoritative=True,
                reason_code="AUTHORITATIVE_ASSESSMENT_REUSED",
            )
            self._store.record_execution(execution)
            return execution

        result = self._analyst.assess(authoritative_packet)
        if not result.success or not isinstance(result.output, ContextAssessment):
            execution = AssessmentExecution.create(
                status=AssessmentExecutionStatus.FAILED,
                packet_id=authoritative_packet.packet_id,
                analysis_behavior_hash=behavior_hash,
                completed_at=(
                    result.completed_at
                    or max(require_utc(self._clock()), authoritative_packet.as_of)
                ),
                codex_attempts=result.attempts,
                usage=result.usage.items(),
                reason_code=result.reason_code,
                source_run_id=result.run_id,
                account_id=result.account_id,
            )
            self._store.record_execution(execution)
            return execution
        if result.output.analysis_behavior_hash != behavior_hash:
            raise ValueError("Codex Assessment 与冻结分析行为身份不一致")
        authoritative = self._store.record_assessment(
            authoritative_packet.packet_id,
            result.output,
        )
        if self._on_success is not None:
            self._on_success(authoritative)
        execution = AssessmentExecution.create(
            status=AssessmentExecutionStatus.SUCCEEDED,
            packet_id=authoritative_packet.packet_id,
            analysis_behavior_hash=behavior_hash,
            completed_at=(
                result.completed_at
                or max(require_utc(self._clock()), authoritative.available_at)
            ),
            codex_attempts=result.attempts,
            usage=result.usage.items(),
            assessment=authoritative,
            reused_authoritative=False,
            reason_code=result.reason_code,
            source_run_id=result.run_id,
            account_id=result.account_id,
        )
        self._store.record_execution(execution)
        return execution
