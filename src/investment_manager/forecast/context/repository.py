from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.forecast.context.review import (
    OpportunityAssessment,
    OpportunityReviewInput,
)
from investment_manager.forecast.context.verification import observe_world_model
from investment_manager.forecast.models import (
    ContextAssessment,
    ContextMechanismObservation,
)
from investment_manager.forecast.tables import (
    assessment_executions,
    codex_runs,
    context_assessments,
    context_mechanism_observations,
    opportunity_assessments,
    opportunity_reviews,
)
from investment_manager.kernel.time import require_utc
from investment_manager.platform.fact_store import analysis_behavior_not_quarantined
from investment_manager.state.decision.packet import DecisionPacket
from investment_manager.state.tables import decision_packets

if TYPE_CHECKING:
    from investment_manager.forecast.context.executor import AssessmentExecution


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

    def record_execution(
        self,
        execution: AssessmentExecution,
    ) -> AssessmentExecution:
        if self.packet(execution.packet_id) is None:
            raise ValueError("AssessmentExecution 必须引用已冻结的 DecisionPacket")
        values = {
            "execution_id": execution.execution_id,
            "packet_id": execution.packet_id,
            "analysis_behavior_hash": execution.analysis_behavior_hash,
            "completed_at": execution.completed_at,
            "status": execution.status.value,
            "source_run_id": execution.source_run_id,
            "payload": execution.model_dump(mode="json"),
        }
        try:
            with self._engine.begin() as connection:
                connection.execute(insert(assessment_executions).values(**values))
        except IntegrityError as exc:
            with self._engine.connect() as connection:
                existing = connection.execute(
                    select(assessment_executions.c.payload).where(
                        assessment_executions.c.execution_id
                        == execution.execution_id
                    )
                ).scalar_one_or_none()
            if existing != execution.model_dump(mode="json"):
                raise ValueError("相同 AssessmentExecution 身份对应不同内容") from exc
        return execution

    def observe_mechanisms(
        self,
        *,
        assessment: ContextAssessment,
        packet: DecisionPacket,
    ) -> tuple[ContextMechanismObservation, ...]:
        """Append deterministic observations before the next model update runs."""

        self.record_packet(packet)
        previous = self.mechanism_observations(assessment.assessment_id)
        observations = observe_world_model(assessment, packet, previous=previous)
        recorded: list[ContextMechanismObservation] = []
        for observation in observations:
            try:
                with self._engine.begin() as connection:
                    connection.execute(
                        insert(context_mechanism_observations).values(
                            observation_id=observation.observation_id,
                            assessment_id=observation.assessment_id,
                            mechanism_id=observation.mechanism_id,
                            test_id=observation.test_id,
                            packet_id=observation.packet_id,
                            observed_at=observation.observed_at,
                            resolution=observation.resolution.value,
                            payload=observation.model_dump(mode="json"),
                        )
                    )
            except IntegrityError as exc:
                with self._engine.connect() as connection:
                    payload = connection.execute(
                        select(context_mechanism_observations.c.payload).where(
                            context_mechanism_observations.c.assessment_id
                            == observation.assessment_id,
                            context_mechanism_observations.c.test_id
                            == observation.test_id,
                            context_mechanism_observations.c.packet_id
                            == observation.packet_id,
                        )
                    ).scalar_one_or_none()
                if payload is None:
                    raise
                existing = ContextMechanismObservation.model_validate(payload)
                if existing != observation:
                    raise ValueError(
                        "相同世界机制测试与 Packet 已存在不同观测"
                    ) from exc
                observation = existing
            recorded.append(observation)
        return tuple(recorded)

    def mechanism_observations(
        self,
        assessment_id: str,
    ) -> tuple[ContextMechanismObservation, ...]:
        with self._engine.connect() as connection:
            payloads = connection.execute(
                select(context_mechanism_observations.c.payload)
                .where(
                    context_mechanism_observations.c.assessment_id == assessment_id
                )
                .order_by(
                    context_mechanism_observations.c.observed_at,
                    context_mechanism_observations.c.observation_id,
                )
            ).scalars()
            return tuple(
                ContextMechanismObservation.model_validate(item) for item in payloads
            )

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
                    context_assessments.c.assessment_id == assessment_id,
                    analysis_behavior_not_quarantined(
                        context_assessments.c.analysis_behavior_hash
                    ),
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
                    analysis_behavior_not_quarantined(
                        context_assessments.c.analysis_behavior_hash
                    ),
                )
            ).scalar_one_or_none()
        return None if payload is None else ContextAssessment.model_validate(payload)

    def latest_before(
        self,
        *,
        analysis_scope: str,
        as_of: datetime,
    ) -> ContextAssessment | None:
        """Latest cognition that was actually available at the new decision time."""

        with self._engine.connect() as connection:
            payload = connection.execute(
                select(context_assessments.c.payload)
                .where(
                    context_assessments.c.analysis_scope == analysis_scope,
                    context_assessments.c.available_at <= require_utc(as_of),
                    analysis_behavior_not_quarantined(
                        context_assessments.c.analysis_behavior_hash
                    ),
                )
                .order_by(
                    context_assessments.c.available_at.desc(),
                    context_assessments.c.assessment_id.desc(),
                )
                .limit(1)
            ).scalar_one_or_none()
        return None if payload is None else ContextAssessment.model_validate(payload)


class SqlOpportunityAssessmentStore:
    """Append-only candidate review ledger, isolated from capital authority."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record_review(self, review: OpportunityReviewInput) -> OpportunityReviewInput:
        values = {
            "review_id": review.review_id,
            "opportunity_id": review.forecast.forecast_id,
            "world_model_id": review.world_model.assessment_id,
            "created_at": review.created_at,
            "content_hash": review.content_hash,
            "payload": review.model_dump(mode="json"),
        }
        try:
            with self._engine.begin() as connection:
                connection.execute(insert(opportunity_reviews).values(**values))
        except IntegrityError as exc:
            existing = self.review(review.review_id)
            if existing != review:
                raise ValueError("相同机会复核输入身份对应不同内容") from exc
            return existing
        return review

    def review(self, review_id: str) -> OpportunityReviewInput | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(opportunity_reviews.c.payload).where(
                    opportunity_reviews.c.review_id == review_id
                )
            ).scalar_one_or_none()
        return None if payload is None else OpportunityReviewInput.model_validate(payload)

    def record_assessment(
        self,
        assessment: OpportunityAssessment,
    ) -> OpportunityAssessment:
        review = self.review(assessment.review_id)
        if review is None:
            raise ValueError("OpportunityAssessment 必须引用已冻结的复核输入")
        if (
            assessment.opportunity_id != review.forecast.forecast_id
            or assessment.world_model_id != review.world_model.assessment_id
        ):
            raise ValueError("OpportunityAssessment 与复核输入身份不一致")
        existing = self.assessment_for(
            review_id=assessment.review_id,
            analysis_behavior_hash=assessment.analysis_behavior_hash,
        )
        if existing is not None:
            if existing != assessment:
                raise ValueError("该机会复核行为已有不同权威结果")
            return existing
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(opportunity_assessments).values(
                        assessment_id=assessment.assessment_id,
                        review_id=assessment.review_id,
                        opportunity_id=assessment.opportunity_id,
                        world_model_id=assessment.world_model_id,
                        analysis_behavior_hash=assessment.analysis_behavior_hash,
                        available_at=assessment.available_at,
                        payload=assessment.model_dump(mode="json"),
                    )
                )
        except IntegrityError as exc:
            raced = self.assessment_for(
                review_id=assessment.review_id,
                analysis_behavior_hash=assessment.analysis_behavior_hash,
            )
            if raced is None or raced != assessment:
                raise ValueError("机会复核结果并发写入冲突") from exc
            return raced
        return assessment

    def assessment_for(
        self,
        *,
        review_id: str,
        analysis_behavior_hash: str,
    ) -> OpportunityAssessment | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(opportunity_assessments.c.payload).where(
                    opportunity_assessments.c.review_id == review_id,
                    opportunity_assessments.c.analysis_behavior_hash
                    == analysis_behavior_hash,
                )
            ).scalar_one_or_none()
        return None if payload is None else OpportunityAssessment.model_validate(payload)

    def attempted(self, review_id: str) -> bool:
        """A final Codex run is a terminal research attempt; Program still proceeds."""

        with self._engine.connect() as connection:
            return (
                connection.execute(
                    select(codex_runs.c.run_id)
                    .where(codex_runs.c.cycle_id == review_id)
                    .limit(1)
                ).scalar_one_or_none()
                is not None
            )
