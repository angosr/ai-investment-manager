from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import Field, ValidationError, field_validator, model_validator
from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

from quant_core.candidate_evaluation import CandidateOutcomeSettler, SqlCandidateOutcomeStore
from quant_core.config import AppConfig, OutcomeEvaluationPolicy, TemporalPolicy
from quant_core.domain import FrozenModel, _require_utc
from quant_core.evaluation import OutcomeWindowEvaluator, OutcomeWindowReport
from quant_core.forecast_evaluation import (
    AnalysisForecastOutcomeSettler,
    SqlAnalysisForecastOutcomeStore,
)
from quant_core.ids import content_hash, stable_id
from quant_core.outcome_evaluation_sql import SqlOutcomeWindowRepository
from quant_core.outcome_evaluation_workflows import (
    OUTCOME_EVALUATION_ACTIVITY_NAME,
    OutcomeEvaluationWorkflow,
)
from quant_core.platform.database import build_engine
from quant_core.temporal_worker import SingleActivityWorker
from quant_core.workflow import OrchestrationPolicySnapshot

logger = logging.getLogger(__name__)


class OutcomeEvaluationWorkflowRequest(FrozenModel):
    workflow_id: str
    pipeline_version: str
    window_start: datetime
    window_end: datetime
    evaluation_version: str
    poll_seconds: int = Field(ge=1, le=3600)
    orchestration: OrchestrationPolicySnapshot
    input_hash: str

    _utc_window_start = field_validator("window_start")(_require_utc)
    _utc_window_end = field_validator("window_end")(_require_utc)

    @model_validator(mode="after")
    def identity_must_match(self):
        if self.window_end <= self.window_start:
            raise ValueError("结果窗口结束时间必须晚于开始时间")
        if self.input_hash != content_hash(_request_identity(self)):
            raise ValueError("Outcome Evaluation Workflow input_hash 不一致")
        expected_id = stable_id(
            "outcome_evaluation_workflow",
            self.pipeline_version,
            self.window_start.isoformat(),
            self.window_end.isoformat(),
            self.evaluation_version,
        )
        if self.workflow_id != expected_id:
            raise ValueError("Outcome Evaluation Workflow workflow_id 不一致")
        return self


class OutcomeEvaluationWorkflowStatus(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class OutcomeEvaluationWorkflowExecution(FrozenModel):
    workflow_id: str
    status: OutcomeEvaluationWorkflowStatus
    reason_code: str
    attempt: int = Field(ge=0)
    report: OutcomeWindowReport | None = None


def _request_identity(request: OutcomeEvaluationWorkflowRequest) -> dict[str, Any]:
    return {
        "pipeline_version": request.pipeline_version,
        "window_start": request.window_start.isoformat(),
        "window_end": request.window_end.isoformat(),
        "evaluation_version": request.evaluation_version,
        "poll_seconds": request.poll_seconds,
        "orchestration": request.orchestration.model_dump(mode="json"),
    }


def build_outcome_evaluation_workflow_request(
    *,
    pipeline_version: str,
    window_start: datetime,
    window_end: datetime,
    policy: OutcomeEvaluationPolicy,
    temporal_policy: TemporalPolicy,
) -> OutcomeEvaluationWorkflowRequest:
    window_start = _require_utc(window_start)
    window_end = _require_utc(window_end)
    orchestration = OrchestrationPolicySnapshot.from_config(temporal_policy)
    payload = {
        "pipeline_version": pipeline_version,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "evaluation_version": policy.version,
        "poll_seconds": policy.poll_seconds,
        "orchestration": orchestration.model_dump(mode="json"),
    }
    return OutcomeEvaluationWorkflowRequest(
        workflow_id=stable_id(
            "outcome_evaluation_workflow",
            pipeline_version,
            window_start.isoformat(),
            window_end.isoformat(),
            policy.version,
        ),
        pipeline_version=pipeline_version,
        window_start=window_start,
        window_end=window_end,
        evaluation_version=policy.version,
        poll_seconds=policy.poll_seconds,
        orchestration=orchestration,
        input_hash=content_hash(payload),
    )


@dataclass(slots=True)
class OutcomeEvaluationActivities:
    repository: SqlOutcomeWindowRepository

    @activity.defn(name=OUTCOME_EVALUATION_ACTIVITY_NAME)
    def evaluate(self, raw_request: dict[str, Any]) -> dict[str, Any]:
        try:
            request = OutcomeEvaluationWorkflowRequest.model_validate(raw_request)
        except ValidationError as exc:
            raise ApplicationError(
                "结果评估 Workflow 输入未通过契约校验",
                type="InvalidOutcomeEvaluationInput",
                non_retryable=True,
            ) from exc
        facts = self.repository.load(
            pipeline_version=request.pipeline_version,
            window_start=request.window_start,
            window_end=request.window_end,
        )
        report = OutcomeWindowEvaluator(version=request.evaluation_version).evaluate(
            pipeline_version=request.pipeline_version,
            window_start=request.window_start,
            window_end=request.window_end,
            cycles=facts.cycles,
            outcomes=facts.outcomes,
            unresolved_cycle_ids=facts.unresolved_cycle_ids,
        )
        self.repository.record(report)
        return {
            "attempt": activity.info().attempt,
            "report": report.model_dump(mode="json"),
        }


@dataclass(slots=True)
class OutcomeEvaluationTemporalCoordinator:
    client: Client
    policy: TemporalPolicy

    async def ensure(self, request: OutcomeEvaluationWorkflowRequest) -> str:
        payload = request.model_dump(mode="json")
        try:
            await self.client.start_workflow(
                OutcomeEvaluationWorkflow.run,
                payload,
                id=request.workflow_id,
                task_queue=self.policy.outcome_evaluation_task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            handle = self.client.get_workflow_handle(request.workflow_id)
            existing_hash = await handle.query(OutcomeEvaluationWorkflow.input_hash)
            if existing_hash != request.input_hash:
                raise ValueError("相同结果窗口 Workflow 的冻结输入不同") from None
        return request.workflow_id

    async def execute(
        self, request: OutcomeEvaluationWorkflowRequest
    ) -> OutcomeEvaluationWorkflowExecution:
        await self.ensure(request)
        raw = await self.client.get_workflow_handle(request.workflow_id).result()
        return OutcomeEvaluationWorkflowExecution.model_validate(raw)


class OutcomeEvaluationTemporalWorker(SingleActivityWorker):
    def __init__(
        self,
        client: Client,
        policy: TemporalPolicy,
        activities: OutcomeEvaluationActivities,
    ) -> None:
        super().__init__(
            client,
            task_queue=policy.outcome_evaluation_task_queue,
            workflows=[OutcomeEvaluationWorkflow],
            activities=[activities.evaluate],
            thread_name_prefix="outcome-evaluation-activity",
        )


@dataclass(slots=True)
class OutcomeEvaluationSupervisorHealth:
    ensure_calls: int = 0
    candidate_settled: int = 0
    candidate_unscorable: int = 0
    forecast_settled: int = 0
    forecast_abstained: int = 0
    forecast_unscorable: int = 0
    last_workflow_id: str | None = None
    last_error_class: str | None = None
    last_candidate_error_class: str | None = None
    last_forecast_error_class: str | None = None


@dataclass(slots=True)
class OutcomeEvaluationSupervisor:
    coordinator: OutcomeEvaluationTemporalCoordinator
    config: AppConfig
    candidate_settler: CandidateOutcomeSettler
    forecast_settler: AnalysisForecastOutcomeSettler
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    health: OutcomeEvaluationSupervisorHealth = field(
        default_factory=OutcomeEvaluationSupervisorHealth
    )

    async def run(self, stop: asyncio.Event) -> None:
        policy = self.config.outcome_evaluation
        window = timedelta(hours=policy.window_hours)
        while not stop.is_set():
            now = _require_utc(self.clock())
            try:
                result = await asyncio.to_thread(self.candidate_settler.settle, as_of=now)
                self.health.candidate_settled += result.settled
                self.health.candidate_unscorable += result.unscorable
                self.health.last_candidate_error_class = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.health.last_candidate_error_class != type(exc).__name__:
                    logger.exception("candidate outcome settlement failed")
                self.health.last_candidate_error_class = type(exc).__name__
            try:
                forecast_result = await asyncio.to_thread(
                    self.forecast_settler.settle, as_of=now
                )
                self.health.forecast_settled += forecast_result.settled
                self.health.forecast_abstained += forecast_result.abstained
                self.health.forecast_unscorable += forecast_result.unscorable
                self.health.last_forecast_error_class = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.health.last_forecast_error_class != type(exc).__name__:
                    logger.exception("analysis forecast settlement failed")
                self.health.last_forecast_error_class = type(exc).__name__
            eligible = now - timedelta(minutes=policy.settlement_grace_minutes)
            window_seconds = int(window.total_seconds())
            window_end = datetime.fromtimestamp(
                int(eligible.timestamp()) // window_seconds * window_seconds,
                tz=UTC,
            )
            window_start = window_end - window
            try:
                request = build_outcome_evaluation_workflow_request(
                    pipeline_version=self.config.pipeline.version,
                    window_start=window_start,
                    window_end=window_end,
                    policy=policy,
                    temporal_policy=self.config.temporal,
                )
                self.health.last_workflow_id = await self.coordinator.ensure(request)
                self.health.ensure_calls += 1
                self.health.last_error_class = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.health.last_error_class != type(exc).__name__:
                    logger.exception("outcome evaluation supervisor failed")
                self.health.last_error_class = type(exc).__name__
            delay = _seconds_until_next_poll(
                _require_utc(self.clock()),
                poll_seconds=policy.poll_seconds,
            )
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)


def _seconds_until_next_poll(now: datetime, *, poll_seconds: int) -> float:
    """按绝对 UTC 时间桶调度，避免候选结算耗时累积为评价漂移。"""

    elapsed = now.timestamp()
    next_boundary = (int(elapsed) // poll_seconds + 1) * poll_seconds
    return max(0.001, next_boundary - elapsed)


def assemble_outcome_evaluation(
    config: AppConfig,
    database_url: str,
    client: Client,
) -> tuple[OutcomeEvaluationTemporalWorker, OutcomeEvaluationSupervisor]:
    engine = build_engine(database_url)
    repository = SqlOutcomeWindowRepository(engine)
    coordinator = OutcomeEvaluationTemporalCoordinator(client, config.temporal)
    return (
        OutcomeEvaluationTemporalWorker(
            client,
            config.temporal,
            OutcomeEvaluationActivities(repository),
        ),
        OutcomeEvaluationSupervisor(
            coordinator=coordinator,
            config=config,
            candidate_settler=CandidateOutcomeSettler(
                store=SqlCandidateOutcomeStore(engine),
                evaluation_version=config.outcome_evaluation.version,
                maximum_market_age_seconds=config.risk.maximum_market_age_seconds,
                settlement_grace_minutes=(config.outcome_evaluation.settlement_grace_minutes),
            ),
            forecast_settler=AnalysisForecastOutcomeSettler(
                engine=engine,
                store=SqlAnalysisForecastOutcomeStore(engine),
                evaluation_version=config.outcome_evaluation.forecast_version,
                maximum_market_age_seconds=config.risk.maximum_market_age_seconds,
                settlement_grace_minutes=(config.outcome_evaluation.settlement_grace_minutes),
            ),
        ),
    )
