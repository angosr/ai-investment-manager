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
from investment_manager.forecast.context.posterior_preparation import (
    ContextPosteriorPreparation,
    PriorResult,
)
from investment_manager.forecast.context.posterior_workflow import (
    POSTERIOR_WORKFLOW_NAME,
    PosteriorWorkflowRequest,
)
from investment_manager.forecast.context.verification import verification_test_id
from investment_manager.forecast.context.workflow import (
    ASSESSMENT_WORKFLOW_NAME,
    AssessmentWorkflowRequest,
)
from investment_manager.forecast.models import (
    MAX_WORLD_MECHANISM_CLAIM_CHARACTERS,
    MAX_WORLD_VERIFICATION_TESTS,
    ContextAssessment,
    ContextMechanism,
    ContextMechanismObservation,
)
from investment_manager.governance.policy import DeploymentStage
from investment_manager.information.text import sanitize_external_text
from investment_manager.kernel.errors import PointInTimeInputUnavailable
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
    PermanentDecisionPacketPreparationError,
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


class TriggerBatchRecorder(Protocol):
    def record_batch(self, batch: TriggerBatch, *, analysis_submitted_at: datetime) -> bool: ...

    def admit_analysis_call(
        self,
        batch: TriggerBatch,
        *,
        requested_at: datetime,
    ) -> AnalysisCallAdmission: ...


class ProgramForecastProducer(Protocol):
    def produce(self, *, as_of: datetime) -> tuple[PriorResult, ...]: ...


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
        posterior_preparation: ContextPosteriorPreparation | None = None,
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
        self._posterior_preparation = posterior_preparation
        self._program_batch_consumers = program_batch_consumers
        self._clock = clock

    def build(self, batch: TriggerBatch) -> tuple[AnalysisDispatchRequest, ...]:
        as_of = batch.created_at
        prior_results: list[PriorResult] = []
        for producer in self._program_forecast_producers:
            prior_results.extend(producer.produce(as_of=as_of))
        for consumer in self._program_batch_consumers:
            consumer.consume(batch)
        trigger_types = {item.trigger_type for item in batch.triggers}
        dispatches: list[AnalysisDispatchRequest] = []
        assessment_triggered = bool(
            trigger_types
            & {
                AnalysisTriggerType.CANONICAL_FACT_REVISED,
                AnalysisTriggerType.INTELLIGENCE_INSERTED,
                AnalysisTriggerType.MARKET_SHOCK,
                AnalysisTriggerType.AGENT_WAKEUP,
            }
        )
        owns_portfolio_assessment = batch.symbol == self._config.assessment.review_trigger_symbol
        posterior_seed = None
        if self._posterior_preparation is not None and owns_portfolio_assessment:
            posterior_seed = self._posterior_preparation.reserve(
                tuple(prior_results),
                as_of=as_of,
            )
            if posterior_seed is not None:
                command = self._assessment_command(
                    batch,
                    as_of=posterior_seed.information_cutoff_at,
                    analysis_identity=posterior_seed.seed_id,
                )
                if command is not None:
                    posterior_request = PosteriorWorkflowRequest.create(
                        seed=posterior_seed,
                        assessment_command=command,
                        producer_behavior_id=(
                            self._posterior_preparation.producer_behavior_id
                        ),
                        orchestration=OrchestrationPolicySnapshot.from_config(
                            self._config.temporal
                        ),
                        created_at=as_of,
                    )
                    dispatches.append(
                        AnalysisDispatchRequest(
                            workflow_name=POSTERIOR_WORKFLOW_NAME,
                            workflow_id=posterior_request.workflow_id,
                            task_queue=self._config.temporal.assessment_task_queue,
                            payload=posterior_request.model_dump(mode="json"),
                        )
                    )
        later_material_trigger = posterior_seed is not None and any(
            item.trigger_type
            in {
                AnalysisTriggerType.CANONICAL_FACT_REVISED,
                AnalysisTriggerType.INTELLIGENCE_INSERTED,
                AnalysisTriggerType.MARKET_SHOCK,
                AnalysisTriggerType.AGENT_WAKEUP,
            }
            and item.observed_at > posterior_seed.information_cutoff_at
            for item in batch.triggers
        )
        if (
            self._config.assessment.enabled
            and assessment_triggered
            and owns_portfolio_assessment
            and (posterior_seed is None or later_material_trigger)
        ):
            command = self._assessment_command(
                batch,
                as_of=as_of,
                analysis_identity=batch.batch_id,
            )
            if command is not None:
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

    def _assessment_command(
        self,
        batch: TriggerBatch,
        *,
        as_of: datetime,
        analysis_identity: str,
    ) -> AssessmentCommand | None:
        assert self._packet_preparation is not None
        assert self._assessment_history is not None
        visible_triggers = tuple(item for item in batch.triggers if item.observed_at <= as_of)
        intelligence_evidence_ids = tuple(
            sorted(
                {
                    evidence
                    for item in visible_triggers
                    if item.trigger_type == AnalysisTriggerType.INTELLIGENCE_INSERTED
                    for evidence in item.evidence_ids
                }
            )
        )
        market_shock_symbols = tuple(
            sorted(
                {
                    symbol
                    for item in visible_triggers
                    if item.trigger_type == AnalysisTriggerType.MARKET_SHOCK
                    for symbol in (item.affected_symbols or (item.symbol,))
                }
            )
        )
        reviews_by_id: dict[str, PacketReviewRequest] = {}
        for trigger in visible_triggers:
            if (
                trigger.trigger_type != AnalysisTriggerType.AGENT_WAKEUP
                or trigger.review_reason is None
                or trigger.occurred_at > as_of
            ):
                continue
            review = PacketReviewRequest.create(
                requested_at=trigger.occurred_at,
                reason=trigger.review_reason,
                evidence_ids=trigger.evidence_ids,
            )
            reviews_by_id[review.review_id] = review
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
            analysis_id=stable_id("assessment_input", analysis_identity),
            as_of=as_of,
            mandate=self._config.assessment.mandate,
            intelligence_evidence_ids=intelligence_evidence_ids,
            market_shock_symbols=market_shock_symbols,
            review_requests=tuple(reviews_by_id[item] for item in sorted(reviews_by_id)),
            previous_context=_previous_context(
                previous,
                observations=previous_observations,
            ),
        )
        if prepared.status != PacketPreparationStatus.READY:
            return None
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
                    observations=(*previous_observations, *recorded_observations),
                )
                assert current_context is not None
                packet = replace_packet_previous_context(
                    packet,
                    current_context,
                    maximum_analysis_characters=(
                        self._config.decision_state.packet_policy.maximum_packet_characters
                    ),
                )
        return AssessmentCommand.create(
            packet=packet,
            analysis_behavior_hash=assess_behavior_hash(
                self._config.codex_runtime,
                packet,
            ),
        )


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
                claim=sanitize_external_text(
                    item.claim,
                    maximum_length=MAX_WORLD_MECHANISM_CLAIM_CHARACTERS,
                )[0],
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
                verification_tests=_previous_verification_tests(
                    assessment_id=assessment.assessment_id,
                    mechanism=item,
                    latest_observation_by_test=latest_observation_by_test,
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


def _previous_verification_tests(
    *,
    assessment_id: str,
    mechanism: ContextMechanism,
    latest_observation_by_test: dict[str, ContextMechanismObservation],
) -> tuple[PacketPreviousVerificationTest, ...]:
    """Carry a decision-dense subset while retaining the full historical contract."""

    candidates = []
    for index, test in enumerate(mechanism.verification_tests):
        observation = latest_observation_by_test.get(
            verification_test_id(
                assessment_id=assessment_id,
                mechanism_id=mechanism.mechanism_id,
                test_index=index,
                test=test,
            )
        )
        candidates.append((index, test, observation))
    ordered = sorted(
        candidates,
        key=lambda item: (_verification_observation_rank(item[2]), item[0]),
    )
    selected = []
    represented_families: set[str] = set()
    for candidate in ordered:
        family = candidate[1].feature_selector.partition(":")[0]
        if family in represented_families:
            continue
        selected.append(candidate)
        represented_families.add(family)
        if len(selected) == MAX_WORLD_VERIFICATION_TESTS:
            break
    if len(selected) < MAX_WORLD_VERIFICATION_TESTS:
        selected_indices = {item[0] for item in selected}
        selected.extend(item for item in ordered if item[0] not in selected_indices)
    selected = sorted(selected[:MAX_WORLD_VERIFICATION_TESTS], key=lambda item: item[0])
    return tuple(
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
                if observation is not None
                else None
            ),
        )
        for _index, test, observation in selected
    )


def _verification_observation_rank(
    observation: ContextMechanismObservation | None,
) -> int:
    if observation is None:
        return 7
    resolution_rank = {
        "CONTRADICTED": 0,
        "AMBIGUOUS": 1,
        "SUPPORTED": 2,
    }.get(observation.resolution.value)
    if resolution_rank is not None:
        return resolution_rank
    return {
        "CONTRADICTS": 3,
        "SUPPORTS": 4,
        "AMBIGUOUS": 5,
        "NEITHER": 6,
    }[observation.match.value]


@dataclass(slots=True)
class TriggerCoordinatorActivities:
    builder: TriggerDispatchBuilder
    _build_lock: Lock = field(default_factory=Lock, init=False, repr=False)

    @activity.defn(name=BUILD_TRIGGER_DISPATCHES_ACTIVITY)
    def build_analysis_dispatches(self, raw_batch: dict[str, Any]) -> dict[str, Any]:
        try:
            batch = TriggerBatch.model_validate(raw_batch)
        except ValidationError as exc:
            raise ApplicationError(
                "TriggerBatch 未通过契约校验",
                type="InvalidTriggerBatch",
                non_retryable=True,
            ) from exc
        try:
            # Every batch projects the same portfolio State chain. Temporal may
            # execute synchronous activities on multiple threads, but parallel
            # builds would race the single previous_state_id and create no useful
            # latency advantage. Keep the read/project/admit boundary serial.
            with self._build_lock:
                dispatches = self.builder.build(batch)
        except AnalysisCallDeferred as exc:
            # Admission is checked after all enabled consumers have frozen their
            # inputs.  Keep that exact batch so a persisted Delta cannot become
            # invisible while waiting for the global minimum interval.
            return {
                "deferred_until": exc.retry_at.isoformat(),
                "retry_frozen_batch": True,
            }
        except PermanentDecisionPacketPreparationError as exc:
            raise ApplicationError(
                str(exc),
                type="PermanentDomainError",
                non_retryable=True,
            ) from exc
        except DecisionPacketPreparationError:
            # State/Delta are already durable.  Advancing batch.as_of would make
            # that delta background state and suppress the assessment permanently.
            return {"retry_frozen_batch": True}
        except PointInTimeInputUnavailable:
            # Preserve this exact point-in-time batch. A later quote/account
            # observation may make the frozen decision input complete.
            return {"retry_frozen_batch": True}
        except ValueError as exc:
            raise ApplicationError(
                str(exc),
                type="PermanentDomainError",
                non_retryable=True,
            ) from exc
        return {"workflow_dispatches": [item.model_dump(mode="json") for item in dispatches]}
