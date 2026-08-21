from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import ValidationError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from investment_manager.forecast.context.analyst import assess_behavior_hash
from investment_manager.forecast.context.application import AssessmentCommand
from investment_manager.forecast.context.workflow import (
    ASSESSMENT_WORKFLOW_NAME,
    AssessmentWorkflowRequest,
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
from investment_manager.state.decision.packet import PacketReviewRequest


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
        batch_recorder: TriggerBatchRecorder | None = None,
        program_forecast_producers: tuple[ProgramForecastProducer, ...] = (),
        program_batch_consumers: tuple[ProgramBatchConsumer, ...] = (),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if config.deployment.stage not in {DeploymentStage.SHADOW, DeploymentStage.TESTNET}:
            raise ValueError("Trigger 分析构建器只允许在 SHADOW 或 TESTNET 阶段启动")
        if config.assessment.enabled and packet_preparation is None:
            raise ValueError("启用 ContextAssessment 时必须装配 DecisionPacket preparation")
        self._config = config
        self._packet_preparation = packet_preparation
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
        if self._config.assessment.enabled:
            assert self._packet_preparation is not None
            prepared = self._packet_preparation.prepare(
                analysis_id=stable_id("assessment_input", batch.batch_id),
                as_of=as_of,
                mandate=self._config.assessment.mandate,
                intelligence_evidence_ids=intelligence_evidence_ids,
                market_shock_symbols=market_shock_symbols,
                review_requests=review_requests,
            )
            if prepared.status == PacketPreparationStatus.READY:
                assert prepared.packet is not None
                command = AssessmentCommand.create(
                    packet=prepared.packet,
                    analysis_behavior_hash=assess_behavior_hash(
                        self._config.codex_runtime,
                        prepared.packet,
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


@dataclass(slots=True)
class TriggerCoordinatorActivities:
    builder: TriggerDispatchBuilder

    @activity.defn(name=BUILD_TRIGGER_DISPATCHES_ACTIVITY)
    def build_analysis_dispatches(self, raw_batch: dict[str, Any]) -> dict[str, Any]:
        try:
            batch = TriggerBatch.model_validate(raw_batch)
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
