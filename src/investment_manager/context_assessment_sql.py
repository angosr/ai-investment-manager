from __future__ import annotations

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.asset_management import ContextAssessment
from investment_manager.persistence import context_assessments
from investment_manager.state.decision_packet import DecisionPacket
from investment_manager.state.tables import decision_packets


class SqlContextAssessmentStore:
    """Immutable DecisionPacket/ContextAssessment evidence ledger."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record_packet(self, packet: DecisionPacket) -> DecisionPacket:
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(decision_packets).values(
                        packet_id=packet.packet_id,
                        analysis_scope=packet.analysis_scope,
                        as_of=packet.as_of,
                        policy_version=packet.policy_version,
                        content_hash=packet.content_hash,
                        payload=packet.model_dump(mode="json"),
                    )
                )
        except IntegrityError:
            existing = self.packet(packet.packet_id)
            if existing != packet:
                raise
            return existing
        return packet

    def record_assessment(
        self,
        packet_id: str,
        assessment: ContextAssessment,
    ) -> ContextAssessment:
        packet = self.packet(packet_id)
        if packet is None:
            raise ValueError("ContextAssessment 必须引用已冻结的 DecisionPacket")
        if (
            assessment.analysis_scope != packet.analysis_scope
            or assessment.decision_packet_hash != packet.content_hash
        ):
            raise ValueError("ContextAssessment 与 DecisionPacket 身份不一致")
        existing = self.assessment_for(
            packet_id=packet_id,
            analysis_behavior_hash=assessment.analysis_behavior_hash,
        )
        if existing is not None:
            if existing != assessment:
                raise ValueError("该 DecisionPacket/Behavior 已有不同的权威 Assessment")
            return existing
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(context_assessments).values(
                        assessment_id=assessment.assessment_id,
                        packet_id=packet_id,
                        analysis_scope=assessment.analysis_scope,
                        available_at=assessment.available_at,
                        analysis_behavior_hash=assessment.analysis_behavior_hash,
                        view_count=len(assessment.views),
                        payload=assessment.model_dump(mode="json"),
                    )
                )
        except IntegrityError as exc:
            raced = self.assessment_for(
                packet_id=packet_id,
                analysis_behavior_hash=assessment.analysis_behavior_hash,
            )
            if raced is None:
                raise
            if raced != assessment:
                raise ValueError(
                    "该 DecisionPacket/Behavior 已并发写入不同的权威 Assessment"
                ) from exc
            return raced
        return assessment

    def packet(self, packet_id: str) -> DecisionPacket | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(decision_packets.c.payload).where(
                    decision_packets.c.packet_id == packet_id
                )
            ).scalar_one_or_none()
        return None if payload is None else DecisionPacket.model_validate(payload)

    def assessment(self, assessment_id: str) -> ContextAssessment | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(context_assessments.c.payload).where(
                    context_assessments.c.assessment_id == assessment_id
                )
            ).scalar_one_or_none()
        return None if payload is None else ContextAssessment.model_validate(payload)

    def assessment_for(
        self,
        *,
        packet_id: str,
        analysis_behavior_hash: str,
    ) -> ContextAssessment | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(context_assessments.c.payload).where(
                    context_assessments.c.packet_id == packet_id,
                    context_assessments.c.analysis_behavior_hash
                    == analysis_behavior_hash,
                )
            ).scalar_one_or_none()
        return None if payload is None else ContextAssessment.model_validate(payload)
