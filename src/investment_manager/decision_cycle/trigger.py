from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock
from typing import Any, Protocol

from pydantic import ValidationError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from investment_manager.forecast.context.analyst import assess_behavior_hash
from investment_manager.forecast.context.application import AssessmentCommand
from investment_manager.forecast.context.verification import verification_test_id
from investment_manager.forecast.context.workflow import (
    ASSESSMENT_WORKFLOW_NAME,
    AssessmentWorkflowRequest,
)
from investment_manager.forecast.models import (
    ContextAssessment,
    ContextMechanismObservation,
)
from investment_manager.governance.policy import DeploymentStage
from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.platform.orchestration import OrchestrationPolicySnapshot
from investment_manager.scheduling.models import (
    AnalysisCallAdmission,
    AnalysisDispatchRequest,
    AnalysisTriggerType,
    TriggerBatch,
)
from investment_manager.scheduling.workflows import BUILD_TRIGGER_DISPATCHES_ACTIVITY
from investment_manager.settings import AppConfig
from investment_manager.state.decision.application import (
    DecisionPacketPreparation,
    DecisionPacketPreparationError,
    PacketPreparationStatus,
)
from investment_manager.state.decision.packet import (
    PREVIOUS_CONTEXT_INVALIDATION_CHARACTERS,
    PREVIOUS_CONTEXT_TRANSMISSION_CHARACTERS,
    DecisionPacket,
    PacketPreviousCausalNode,
    PacketPreviousContext,
    PacketPreviousEventReference,
    PacketPreviousMechanism,
    PacketPreviousVerificationObservation,
    PacketPreviousVerificationPredicate,
    PacketPreviousVerificationTest,
    PacketReviewRequest,
    replace_packet_previous_context,
)
from investment_manager.state.panel import sanitize_external_text


class TriggerBatchRecorder(Protocol):
    def record_batch(self, batch: TriggerBatch, *, analysis_submitted_at: datetime) -> bool: ...

    def admit_analysis_call(
        self,
        batch: TriggerBatch,
        *,
        requested_at: datetime,
    ) -> AnalysisCallAdmission: ...


class ProgramForecastProducer(Protocol):
    def produce(self, *, as_of: datetime) -> object: ...


class ProgramBatchConsumer(Protocol):
    def consume(self, batch: TriggerBatch) -> object: ...


class AssessmentHistoryReader(Protocol):
    def latest_before(
        self,
        *,
        analysis_scope: str,
        as_of: datetime,
    ) -> ContextAssessment | None: ...

    def observe_mechanisms(
        self,
        *,
        assessment: ContextAssessment,
        packet: DecisionPacket,
    ) -> tuple[ContextMechanismObservation, ...]: ...

    def mechanism_observations(
        self,
        assessment_id: str,
    ) -> tuple[ContextMechanismObservation, ...]: ...


class AnalysisCallDeferred(Exception):
    def __init__(self, retry_at: datetime) -> None:
        self.retry_at = require_utc(retry_at)
        super().__init__(f"analysis call deferred until {self.retry_at.isoformat()}")


class TriggerDispatchBuilder:
    """Freeze every enabled decision consumer of one admitted trigger batch."""

    def __init__(
        self,
        *,
        config: AppConfig,
        packet_preparation: DecisionPacketPreparation | None = None,
        assessment_history: AssessmentHistoryReader | None = None,
        batch_recorder: TriggerBatchRecorder | None = None,
        program_forecast_producers: tuple[ProgramForecastProducer, ...] = (),
        program_batch_consumers: tuple[ProgramBatchConsumer, ...] = (),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if config.deployment.stage not in {DeploymentStage.SHADOW, DeploymentStage.TESTNET}:
            raise ValueError("Trigger 分析构建器只允许在 SHADOW 或 TESTNET 阶段启动")
        if config.assessment.enabled and packet_preparation is None:
            raise ValueError("启用 ContextAssessment 时必须装配 DecisionPacket preparation")
        if config.assessment.enabled and assessment_history is None:
            raise ValueError("启用 ContextAssessment 时必须装配上一轮认知读取器")
        self._config = config
        self._packet_preparation = packet_preparation
        self._assessment_history = assessment_history
        self._batch_recorder = batch_recorder
        self._program_forecast_producers = program_forecast_producers
        self._program_batch_consumers = program_batch_consumers
        self._clock = clock

    def build(self, batch: TriggerBatch) -> tuple[AnalysisDispatchRequest, ...]:
        as_of = batch.created_at
        for producer in self._program_forecast_producers:
            producer.produce(as_of=as_of)
        for consumer in self._program_batch_consumers:
            consumer.consume(batch)
        trigger_types = {item.trigger_type for item in batch.triggers}
        intelligence_evidence_ids = tuple(
            sorted(
                {
                    evidence
                    for item in batch.triggers
                    if item.trigger_type == AnalysisTriggerType.INTELLIGENCE_INSERTED
                    for evidence in item.evidence_ids
                }
            )
        )
        market_shock_symbols = (
            (batch.symbol,) if AnalysisTriggerType.MARKET_SHOCK in trigger_types else ()
        )
        reviews_by_id: dict[str, PacketReviewRequest] = {}
        for trigger in batch.triggers:
            if trigger.trigger_type != AnalysisTriggerType.AGENT_WAKEUP:
                continue
            if trigger.review_reason is None:
                # v11 以前的历史 payload 没有理由，允许读取但不能伪造评审授权。
                continue
            review = PacketReviewRequest.create(
                requested_at=trigger.occurred_at,
                reason=trigger.review_reason,
                evidence_ids=trigger.evidence_ids,
            )
            reviews_by_id[review.review_id] = review
        review_requests = tuple(reviews_by_id[item] for item in sorted(reviews_by_id))
        dispatches: list[AnalysisDispatchRequest] = []
        assessment_triggered = any(
            item != AnalysisTriggerType.WORLD_MODEL_UPDATED for item in trigger_types
        )
        if self._config.assessment.enabled and assessment_triggered:
            assert self._packet_preparation is not None
            assert self._assessment_history is not None
            previous = self._assessment_history.latest_before(
                analysis_scope=self._config.assessment.mandate.analysis_scope,
                as_of=as_of,
            )
            previous_observations = (
                self._assessment_history.mechanism_observations(previous.assessment_id)
                if previous is not None
                else ()
            )
            prepared = self._packet_preparation.prepare(
                analysis_id=stable_id("assessment_input", batch.batch_id),
                as_of=as_of,
                mandate=self._config.assessment.mandate,
                intelligence_evidence_ids=intelligence_evidence_ids,
                market_shock_symbols=market_shock_symbols,
                review_requests=review_requests,
                previous_context=_previous_context(
                    previous,
                    observations=previous_observations,
                ),
            )
            if prepared.status == PacketPreparationStatus.READY:
                assert prepared.packet is not None
                packet = prepared.packet
                if previous is not None:
                    recorded_observations = self._assessment_history.observe_mechanisms(
                        assessment=previous,
                        packet=packet,
                    )
                    if recorded_observations:
                        current_context = _previous_context(
                            previous,
                            observations=(
                                *previous_observations,
                                *recorded_observations,
                            ),
                        )
                        assert current_context is not None
                        packet = replace_packet_previous_context(
                            packet,
                            current_context,
                        )
                command = AssessmentCommand.create(
                    packet=packet,
                    analysis_behavior_hash=assess_behavior_hash(
                        self._config.codex_runtime,
                        packet,
                    ),
                )
                assessment_request = AssessmentWorkflowRequest.create(
                    command=command,
                    orchestration=OrchestrationPolicySnapshot.from_config(self._config.temporal),
                    created_at=as_of,
                    deadline=batch.deadline,
                )
                dispatches.append(
                    AnalysisDispatchRequest(
                        workflow_name=ASSESSMENT_WORKFLOW_NAME,
                        workflow_id=assessment_request.workflow_id,
                        task_queue=self._config.temporal.assessment_task_queue,
                        payload=assessment_request.model_dump(mode="json"),
                    )
                )
        if self._batch_recorder is not None:
            submitted_at = max(require_utc(self._clock()), as_of)
            if dispatches:
                admission = self._batch_recorder.admit_analysis_call(
                    batch,
                    requested_at=submitted_at,
                )
                if not admission.admitted:
                    if admission.retry_at is None:
                        raise RuntimeError("调用准入缺少 retry_at")
                    raise AnalysisCallDeferred(admission.retry_at)
            self._batch_recorder.record_batch(batch, analysis_submitted_at=submitted_at)
        return tuple(dispatches)


def _previous_context(
    assessment: ContextAssessment | None,
    *,
    observations: tuple[ContextMechanismObservation, ...] = (),
) -> PacketPreviousContext | None:
    if assessment is None:
        return None
    latest_observation_by_test = {
        item.test_id: item
        for item in sorted(
            observations,
            key=lambda value: (value.observed_at, value.observation_id),
        )
    }
    return PacketPreviousContext(
        assessment_id=assessment.assessment_id,
        analysis_scope=assessment.analysis_scope,
        mandate_version=assessment.mandate_version,
        analysis_behavior_hash=assessment.analysis_behavior_hash,
        decision_packet_hash=assessment.decision_packet_hash,
        as_of=assessment.as_of,
        available_at=assessment.available_at,
        synthesis=sanitize_external_text(
            assessment.synthesis,
            maximum_length=2_000,
        )[0],
        synthesis_horizon_hours=assessment.synthesis_horizon_hours,
        event_references=tuple(
            PacketPreviousEventReference(
                evidence_id=item.evidence_id,
                source=item.source,
                title=item.title,
                event_time=item.event_time,
                impact_state=item.impact_state.value,
                rationale=item.rationale,
                stale_at=item.stale_at,
            )
            for item in assessment.event_references
        ),
        mechanisms=tuple(
            PacketPreviousMechanism(
                mechanism_id=item.mechanism_id,
                continuity_ref=item.continuity_ref,
                relationship=item.relationship.value,
                claim=sanitize_external_text(item.claim, maximum_length=1_200)[0],
                horizon_hours=item.horizon_hours,
                causal_chain=tuple(
                    PacketPreviousCausalNode(
                        statement=sanitize_external_text(
                            node.statement,
                            maximum_length=PREVIOUS_CONTEXT_TRANSMISSION_CHARACTERS,
                        )[0],
                        evidence_ids=node.evidence_ids,
                    )
                    for node in item.causal_chain
                ),
                transmission_stage=item.transmission_stage.value,
                conflicting_evidence_ids=item.conflicting_evidence_ids,
                verification_tests=tuple(
                    PacketPreviousVerificationTest(
                        feature_selector=test.feature_selector,
                        evaluation_window_minutes=test.evaluation_window_minutes,
                        supports_predicate=PacketPreviousVerificationPredicate(
                            **test.supports_predicate.model_dump()
                        ),
                        contradicts_predicate=PacketPreviousVerificationPredicate(
                            **test.contradicts_predicate.model_dump()
                        ),
                        latest_observation=(
                            PacketPreviousVerificationObservation(
                                observed_at=observation.observed_at,
                                value=observation.value,
                                match=observation.match.value,
                                support_streak=observation.support_streak,
                                contradiction_streak=observation.contradiction_streak,
                                resolution=observation.resolution.value,
                            )
                            if (
                                observation := latest_observation_by_test.get(
                                    verification_test_id(
                                        assessment_id=assessment.assessment_id,
                                        mechanism_id=item.mechanism_id,
                                        test_index=index,
                                        test=test,
                                    )
                                )
                            )
                            is not None
                            else None
                        ),
                    )
                    for index, test in enumerate(item.verification_tests)
                ),
                invalidation_conditions=tuple(
                    sanitize_external_text(
                        condition,
                        maximum_length=PREVIOUS_CONTEXT_INVALIDATION_CHARACTERS,
                    )[0]
                    for condition in item.invalidation_conditions
                ),
                next_review_at=item.next_review_at,
            )
            for item in assessment.mechanisms
        ),
    )


@dataclass(slots=True)
class TriggerCoordinatorActivities:
    builder: TriggerDispatchBuilder
    _build_lock: Lock = field(default_factory=Lock, init=False, repr=False)

    @activity.defn(name=BUILD_TRIGGER_DISPATCHES_ACTIVITY)
    def build_analysis_dispatches(self, raw_batch: dict[str, Any]) -> dict[str, Any]:
        try:
            batch = TriggerBatch.model_validate(raw_batch)
            # Every batch projects the same portfolio State chain. Temporal may
            # execute synchronous activities on multiple threads, but parallel
            # builds would race the single previous_state_id and create no useful
            # latency advantage. Keep the read/project/admit boundary serial.
            with self._build_lock:
                dispatches = self.builder.build(batch)
        except ValidationError as exc:
            raise ApplicationError(
                "TriggerBatch 未通过契约校验",
                type="InvalidTriggerBatch",
                non_retryable=True,
            ) from exc
        except AnalysisCallDeferred as exc:
            # Admission is checked after all enabled consumers have frozen their
            # inputs.  Keep that exact batch so a persisted Delta cannot become
            # invisible while waiting for the global minimum interval.
            return {
                "deferred_until": exc.retry_at.isoformat(),
                "retry_frozen_batch": True,
            }
        except DecisionPacketPreparationError:
            # State/Delta are already durable.  Advancing batch.as_of would make
            # that delta background state and suppress the assessment permanently.
            return {"retry_frozen_batch": True}
        except ValueError as exc:
            raise ApplicationError(
                "TriggerBatch 的行情或账户输入暂不可用",
                type="TriggerInputUnavailable",
            ) from exc
        return {"workflow_dispatches": [item.model_dump(mode="json") for item in dispatches]}
