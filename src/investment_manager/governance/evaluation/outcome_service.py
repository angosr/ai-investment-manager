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
from sqlalchemy.engine import Engine
from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

from investment_manager.forecast.context.producer import context_spot_forecast_contract
from investment_manager.forecast.contracts import ForecastContract
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.forecast.settlement import ForecastOutcomeSettler
from investment_manager.governance.evaluation.outcome_store import SqlOutcomeWindowRepository
from investment_manager.governance.evaluation.outcome_workflow import (
    OUTCOME_EVALUATION_ACTIVITY_NAME,
    OutcomeEvaluationWorkflow,
)
from investment_manager.governance.evaluation.performance import (
    OutcomeWindowEvaluator,
    OutcomeWindowReport,
)
from investment_manager.governance.evaluation.world_model_ablation import (
    SqlWorldModelAblationRepository,
    WorldModelAblationRunner,
    assemble_world_model_ablation_analyst,
    ensure_world_model_ablation_plan,
)
from investment_manager.governance.models import EvaluationPlan, ReleaseManifest
from investment_manager.governance.policy import OutcomeEvaluationPolicy
from investment_manager.governance.repository import SqlGovernanceRepository
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.legacy.candidate_evaluation import (
    CandidateOutcomeSettler,
    SqlCandidateOutcomeStore,
)
from investment_manager.legacy.forecast_evaluation import (
    AnalysisForecastOutcomeSettler,
    SqlAnalysisForecastOutcomeStore,
)
from investment_manager.market.models import InstrumentProduct
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.platform.database import build_engine
from investment_manager.platform.orchestration import OrchestrationPolicySnapshot
from investment_manager.platform.temporal import SingleActivityWorker
from investment_manager.scheduling.policy import TemporalPolicy
from investment_manager.settings import AppConfig

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

    _utc_window_start = field_validator("window_start")(require_utc)
    _utc_window_end = field_validator("window_end")(require_utc)

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
    window_start = require_utc(window_start)
    window_end = require_utc(window_end)
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
    target_forecast_settled: int = 0
    target_forecast_outcome_unavailable: int = 0
    target_forecast_pending: int = 0
    world_model_ablation_assignments: int = 0
    world_model_ablation_settled_pairs: int = 0
    world_model_ablation_failed_controls: int = 0
    last_workflow_id: str | None = None
    last_error_class: str | None = None
    last_candidate_error_class: str | None = None
    last_forecast_error_class: str | None = None
    last_target_forecast_error_class: str | None = None
    last_world_model_ablation_error_class: str | None = None


@dataclass(slots=True)
class OutcomeEvaluationSupervisor:
    coordinator: OutcomeEvaluationTemporalCoordinator
    config: AppConfig
    candidate_settler: CandidateOutcomeSettler
    forecast_settler: AnalysisForecastOutcomeSettler
    target_forecast_settler: ForecastOutcomeSettler
    world_model_ablation_runner: WorldModelAblationRunner | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    health: OutcomeEvaluationSupervisorHealth = field(
        default_factory=OutcomeEvaluationSupervisorHealth
    )

    async def run(self, stop: asyncio.Event) -> None:
        policy = self.config.outcome_evaluation
        window = timedelta(hours=policy.window_hours)
        while not stop.is_set():
            now = require_utc(self.clock())
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
                forecast_result = await asyncio.to_thread(self.forecast_settler.settle, as_of=now)
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
            try:
                target_result = await asyncio.to_thread(
                    self.target_forecast_settler.settle,
                    as_of=now,
                )
                self.health.target_forecast_settled += target_result.settled
                self.health.target_forecast_outcome_unavailable += (
                    target_result.outcome_unavailable
                )
                self.health.target_forecast_pending += target_result.pending
                self.health.last_target_forecast_error_class = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.health.last_target_forecast_error_class != type(exc).__name__:
                    logger.exception("target forecast settlement failed")
                self.health.last_target_forecast_error_class = type(exc).__name__
            if self.world_model_ablation_runner is not None:
                try:
                    report = await asyncio.to_thread(
                        self.world_model_ablation_runner.reconcile,
                        as_of=now,
                    )
                    self.health.world_model_ablation_assignments = report.assignments
                    self.health.world_model_ablation_settled_pairs = report.settled_pairs
                    self.health.world_model_ablation_failed_controls = report.failed_controls
                    self.health.last_world_model_ablation_error_class = None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if self.health.last_world_model_ablation_error_class != type(exc).__name__:
                        logger.exception("world model ablation evaluation failed")
                    self.health.last_world_model_ablation_error_class = type(exc).__name__
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
                require_utc(self.clock()),
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
    *,
    release: ReleaseManifest | None = None,
) -> tuple[OutcomeEvaluationTemporalWorker, OutcomeEvaluationSupervisor]:
    engine = build_engine(database_url)
    repository = SqlOutcomeWindowRepository(engine)
    coordinator = OutcomeEvaluationTemporalCoordinator(client, config.temporal)
    ablation_runner = _assemble_world_model_ablation(
        config=config,
        engine=engine,
        release=release,
    )
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
            target_forecast_settler=ForecastOutcomeSettler(
                market=SqlMarketDataStore(engine),
                store=SqlForecastStore(engine),
                evaluation_version=(config.outcome_evaluation.target_forecast_version),
                maximum_spot_age_seconds=config.risk.maximum_market_age_seconds,
                maximum_perpetual_age_seconds=(config.market_data.perpetual_poll_seconds * 3),
                maximum_funding_gap_hours=(config.outcome_evaluation.maximum_funding_gap_hours),
                settlement_grace_minutes=(config.outcome_evaluation.settlement_grace_minutes),
            ),
            world_model_ablation_runner=ablation_runner,
        ),
    )


def _assemble_world_model_ablation(
    *,
    config: AppConfig,
    engine,
    release: ReleaseManifest | None,
) -> WorldModelAblationRunner | None:
    policy = config.outcome_evaluation.world_model_ablation
    if policy is None or not policy.enabled:
        return None
    if release is None:
        raise ValueError("启用 WorldModel control 必须绑定 ReleaseManifest")
    contract = configured_world_model_ablation_contract(config)
    context = config.capital.context_forecast
    assert context is not None
    plan = ensure_world_model_ablation_plan(
        governance=SqlGovernanceRepository(engine),
        config=config,
        contract=contract,
        release=release,
        registered_at=datetime.now(UTC),
    )
    return WorldModelAblationRunner(
        policy=policy,
        plan=plan,
        formal_producer_behavior_id=context.producer_behavior_id,
        evaluation_version=config.outcome_evaluation.target_forecast_version,
        repository=SqlWorldModelAblationRepository(engine),
        analyst=assemble_world_model_ablation_analyst(config, engine=engine),
    )


def configured_world_model_ablation_contract(config: AppConfig) -> ForecastContract:
    """Build the one formal contract shared by registration and runtime assembly."""

    context = config.capital.context_forecast
    if context is None or not context.enabled:
        raise ValueError("启用 WorldModel control 必须绑定 Context Forecast")
    instrument = next(
        (
            item.instrument
            for item in config.capital.execution_specs
            if item.instrument.key == context.target_instrument_key
            and item.instrument.product == InstrumentProduct.SPOT
        ),
        None,
    )
    if instrument is None:
        raise ValueError("WorldModel control 合同品种不在 Capital Spot 范围")
    return context_spot_forecast_contract(
        policy=context,
        instrument=instrument,
        cost_semantics_version=config.capital.decision.cost_model_version,
    )


def preregister_world_model_ablation_plan(
    *,
    config: AppConfig,
    engine: Engine,
    release: ReleaseManifest,
    registered_at: datetime,
) -> EvaluationPlan:
    """Persist the candidate plan before release preflight or either model call."""

    return ensure_world_model_ablation_plan(
        governance=SqlGovernanceRepository(engine),
        config=config,
        contract=configured_world_model_ablation_contract(config),
        release=release,
        registered_at=registered_at,
    )
