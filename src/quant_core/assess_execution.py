from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import Field, model_validator

from quant_core.analyst import AnalystResult
from quant_core.asset_management import ContextAssessment
from quant_core.context_assessment_sql import SqlContextAssessmentStore
from quant_core.decision_packet import DecisionPacket
from quant_core.domain import FrozenModel


class ContextAnalyst(Protocol):
    def behavior_hash(self, packet: DecisionPacket) -> str: ...

    def assess(self, packet: DecisionPacket) -> AnalystResult: ...


class AssessmentExecutionStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class AssessmentExecution(FrozenModel):
    status: AssessmentExecutionStatus
    packet_id: str
    analysis_behavior_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    assessment: ContextAssessment | None = None
    reused_authoritative: bool = False
    reason_code: str
    source_run_id: str | None = None
    account_id: str | None = None

    @model_validator(mode="after")
    def result_shape_must_match_status(self):
        if self.status == AssessmentExecutionStatus.SUCCEEDED:
            if self.assessment is None:
                raise ValueError("成功的 ASSESS execution 必须包含 Assessment")
            if self.assessment.analysis_behavior_hash != self.analysis_behavior_hash:
                raise ValueError("ASSESS execution 行为身份不一致")
        elif self.assessment is not None or self.reused_authoritative:
            raise ValueError("失败的 ASSESS execution 不得携带权威结果")
        return self


class ContextAssessmentExecutor:
    """Crash-safe Packet+Behavior execution boundary around the Codex analyst."""

    def __init__(
        self,
        store: SqlContextAssessmentStore,
        analyst: ContextAnalyst,
    ) -> None:
        self._store = store
        self._analyst = analyst

    def execute(
        self,
        packet: DecisionPacket,
        *,
        expected_analysis_behavior_hash: str | None = None,
    ) -> AssessmentExecution:
        authoritative_packet = self._store.record_packet(packet)
        behavior_hash = self._analyst.behavior_hash(authoritative_packet)
        if (
            expected_analysis_behavior_hash is not None
            and behavior_hash != expected_analysis_behavior_hash
        ):
            raise ValueError("当前 Codex 分析行为与冻结 Workflow 输入不一致")
        existing = self._store.assessment_for(
            packet_id=authoritative_packet.packet_id,
            analysis_behavior_hash=behavior_hash,
        )
        if existing is not None:
            return AssessmentExecution(
                status=AssessmentExecutionStatus.SUCCEEDED,
                packet_id=authoritative_packet.packet_id,
                analysis_behavior_hash=behavior_hash,
                assessment=existing,
                reused_authoritative=True,
                reason_code="AUTHORITATIVE_ASSESSMENT_REUSED",
            )

        result = self._analyst.assess(authoritative_packet)
        if not result.success or not isinstance(result.output, ContextAssessment):
            return AssessmentExecution(
                status=AssessmentExecutionStatus.FAILED,
                packet_id=authoritative_packet.packet_id,
                analysis_behavior_hash=behavior_hash,
                reason_code=result.reason_code,
                source_run_id=result.run_id,
                account_id=result.account_id,
            )
        if result.output.analysis_behavior_hash != behavior_hash:
            raise ValueError("Codex Assessment 与冻结分析行为身份不一致")
        authoritative = self._store.record_assessment(
            authoritative_packet.packet_id,
            result.output,
        )
        return AssessmentExecution(
            status=AssessmentExecutionStatus.SUCCEEDED,
            packet_id=authoritative_packet.packet_id,
            analysis_behavior_hash=behavior_hash,
            assessment=authoritative,
            reused_authoritative=False,
            reason_code=result.reason_code,
            source_run_id=result.run_id,
            account_id=result.account_id,
        )
