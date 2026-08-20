from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import ValidationError
from temporalio import activity
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

from investment_manager.config import AppConfig, DeploymentStage, TemporalPolicy
from investment_manager.cycle import CycleInput
from investment_manager.ingestion import EventStore
from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.market.features import FeatureEngine
from investment_manager.market.repository import MarketDataStore
from investment_manager.portfolio_protection import PortfolioProtectionStore
from investment_manager.shadow import ShadowStateReader
from investment_manager.trigger import (
    AnalysisCallAdmission,
    AnalysisTriggerPlan,
    AnalysisTriggerType,
    TriggerBatch,
    TriggerDecision,
    TriggerReason,
)
from investment_manager.trigger_sql import SqlTriggerRepository, TriggerOutboxMessage
from investment_manager.trigger_workflows import (
    BUILD_TRIGGER_REQUEST_ACTIVITY,
    TRIGGER_SIGNAL,
    TriggerCoordinatorWorkflow,
    coordinator_workflow_id,
)
from investment_manager.workflow import WorkflowRequest, build_workflow_request

logger = logging.getLogger(__name__)


class TriggerBatchRecorder(Protocol):
    def record_batch(self, batch: TriggerBatch, *, analysis_submitted_at: datetime) -> bool: ...

    def admit_analysis_call(
        self,
        batch: TriggerBatch,
        *,
        requested_at: datetime,
    ) -> AnalysisCallAdmission: ...


class AnalysisCallDeferred(Exception):
    def __init__(self, retry_at: datetime) -> None:
        self.retry_at = require_utc(retry_at)
        super().__init__(f"analysis call deferred until {self.retry_at.isoformat()}")


class OutboxWakeup(Protocol):
    def wait(self, timeout_seconds: float) -> bool: ...


class TriggerAnalysisRequestBuilder:
    """把不可变 TriggerBatch 冻结为现有 AnalysisCycle 输入，不承担调度策略。"""

    def __init__(
        self,
        *,
        config: AppConfig,
        market_store: MarketDataStore,
        event_store: EventStore,
        state: ShadowStateReader,
        protection: PortfolioProtectionStore,
        batch_recorder: TriggerBatchRecorder | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if config.deployment.stage not in {DeploymentStage.SHADOW, DeploymentStage.TESTNET}:
            raise ValueError("Trigger 分析构建器只允许在 SHADOW 或 TESTNET 阶段启动")
        self._config = config
        self._market_store = market_store
        self._event_store = event_store
        self._state = state
        self._protection = protection
        self._batch_recorder = batch_recorder
        self._clock = clock
        self._features = FeatureEngine(config.feature)

    def build(self, batch: TriggerBatch) -> WorkflowRequest:
        as_of = batch.created_at
        cycle_id = stable_id("triggered_cycle", batch.batch_id)
        market = self._market_store.snapshot(
            cycle_id=cycle_id,
            symbol=batch.symbol,
            interval=self._config.market_data.interval,
            as_of=as_of,
            bar_window=self._config.market_data.bar_window,
            source=self._config.market_data.version,
        )
        self._features.compute(market)
        account = self._state.account_for_cycle(
            cycle_id=cycle_id,
            as_of=as_of,
            initial_quote_balance=self._config.shadow.initial_quote_balance,
        )
        marks = {market.symbol: market.last}
        for position in account.positions:
            if position.symbol in marks:
                continue
            trade = self._market_store.latest_trade(symbol=position.symbol, as_of=as_of)
            age_seconds = (as_of - trade.observed_at).total_seconds()
            if age_seconds > self._config.risk.maximum_market_age_seconds:
                raise ValueError(f"{position.symbol} 持仓盯市成交已过期")
            marks[position.symbol] = trade.price
        account = self._protection.observe(account, marks=marks, as_of=as_of)
        events = self._event_store.visible(symbol=batch.symbol, as_of=as_of)
        trigger_types = {item.trigger_type for item in batch.triggers}
        if AnalysisTriggerType.AGENT_WAKEUP in trigger_types:
            reason = TriggerReason.AGENT_WAKEUP
        elif AnalysisTriggerType.POSITION_RECHECK in trigger_types:
            reason = TriggerReason.POSITION_RECHECK
        elif trigger_types == {AnalysisTriggerType.HEARTBEAT}:
            reason = TriggerReason.HEARTBEAT
        else:
            reason = TriggerReason.EVENT_BATCH
        evidence_ids = tuple(
            sorted({evidence for item in batch.triggers for evidence in item.evidence_ids})
        )
        cycle_input = CycleInput(
            market=market,
            account=account,
            events=events,
            frequency_orders_today=self._state.entry_orders_today(as_of=as_of),
            frequency_last_entry_order_at=self._state.last_entry_order_at(
                symbol=batch.symbol, as_of=as_of
            ),
        )
        request = build_workflow_request(
            cycle_input=cycle_input,
            trigger=TriggerDecision(
                should_run=True,
                reason=reason,
                evidence_ids=evidence_ids,
            ),
            temporal_policy=self._config.temporal,
            created_at=as_of,
            deadline=batch.deadline,
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
        return request


@dataclass(slots=True)
class TriggerCoordinatorActivities:
    builder: TriggerAnalysisRequestBuilder

    @activity.defn(name=BUILD_TRIGGER_REQUEST_ACTIVITY)
    def build_analysis_request(self, raw_batch: dict[str, Any]) -> dict[str, Any]:
        try:
            batch = TriggerBatch.model_validate(raw_batch)
            request = self.builder.build(batch)
        except ValidationError as exc:
            raise ApplicationError(
                "TriggerBatch 未通过契约校验",
                type="InvalidTriggerBatch",
                non_retryable=True,
            ) from exc
        except AnalysisCallDeferred as exc:
            return {"deferred_until": exc.retry_at.isoformat()}
        except ValueError as exc:
            raise ApplicationError(
                "TriggerBatch 的行情或账户输入暂不可用",
                type="TriggerInputUnavailable",
            ) from exc
        return {"workflow_request": request.model_dump(mode="json")}


def build_trigger_coordinator_input(plan: AnalysisTriggerPlan, config: AppConfig) -> dict[str, Any]:
    return {
        "workflow_id": coordinator_workflow_id(plan.symbol, plan.pipeline_id),
        "plan": plan.model_dump(mode="json"),
        "settings": {
            "trigger_policy_version": config.trigger.version,
            "maximum_batch_size": config.trigger.maximum_batch_size,
            "maximum_pending_triggers": config.trigger.maximum_pending_triggers,
            "trigger_expiry_seconds": config.trigger.trigger_expiry_seconds,
            "analysis_deadline_seconds": config.shadow.analysis_deadline_seconds,
            "analysis_task_queue": config.temporal.task_queue,
            "activity_start_to_close_seconds": config.temporal.activity_start_to_close_seconds,
            "activity_schedule_to_close_seconds": (
                config.temporal.activity_schedule_to_close_seconds
            ),
            "retry_initial_seconds": config.temporal.retry_initial_seconds,
            "retry_maximum_seconds": config.temporal.retry_maximum_seconds,
            "retry_backoff_coefficient": config.temporal.retry_backoff_coefficient,
            "retry_maximum_attempts": config.temporal.retry_maximum_attempts,
        },
    }


@dataclass(slots=True)
class TemporalTriggerDispatcher:
    client: Client
    config: AppConfig
    plans: SqlTriggerRepository

    async def deliver(self, message: TriggerOutboxMessage) -> None:
        symbol, pipeline_id = message.aggregate_key.split(":", 1)
        if pipeline_id != self.config.pipeline.version:
            # Release cutover 后的历史 outbox 只作事实保留，不得复活旧 coordinator。
            return
        plan = self.plans.plan_for_scope(symbol=symbol, pipeline_id=pipeline_id)
        workflow_id = coordinator_workflow_id(symbol, pipeline_id)
        with suppress(WorkflowAlreadyStartedError):
            await self.client.start_workflow(
                TriggerCoordinatorWorkflow.run,
                build_trigger_coordinator_input(plan, self.config),
                id=workflow_id,
                task_queue=self.config.temporal.trigger_task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        handle = self.client.get_workflow_handle(workflow_id)
        await handle.signal(TRIGGER_SIGNAL, message.payload)


async def terminate_superseded_trigger_coordinators(
    *,
    client: Client,
    plans: tuple[AnalysisTriggerPlan, ...],
    active_pipeline_id: str,
) -> tuple[str, ...]:
    """在 release 切换时终止同一交易范围内的旧 pipeline coordinator。"""

    terminated: list[str] = []
    for plan in plans:
        if plan.pipeline_id == active_pipeline_id:
            continue
        workflow_id = coordinator_workflow_id(plan.symbol, plan.pipeline_id)
        handle = client.get_workflow_handle(workflow_id)
        try:
            description = await handle.describe()
            if description.status != WorkflowExecutionStatus.RUNNING:
                continue
            await handle.terminate(f"superseded by pipeline {active_pipeline_id}")
        except RPCError as exc:
            if exc.status not in {
                RPCStatusCode.NOT_FOUND,
                RPCStatusCode.FAILED_PRECONDITION,
            }:
                raise
            continue
        terminated.append(workflow_id)
    return tuple(terminated)


class TriggerTemporalWorker:
    def __init__(
        self,
        client: Client,
        policy: TemporalPolicy,
        activities: TriggerCoordinatorActivities,
    ) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="trigger-activity")
        self._worker = Worker(
            client,
            task_queue=policy.trigger_task_queue,
            workflows=[TriggerCoordinatorWorkflow],
            activities=[activities.build_analysis_request],
            activity_executor=self._executor,
            max_concurrent_activities=1,
        )

    async def __aenter__(self) -> TriggerTemporalWorker:
        await self._worker.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            await self._worker.__aexit__(exc_type, exc, tb)
        finally:
            self._executor.shutdown(wait=True, cancel_futures=True)


@dataclass(slots=True)
class TriggerOutboxDispatcherService:
    repository: SqlTriggerRepository
    dispatcher: TemporalTriggerDispatcher
    poll_seconds: float
    wakeup: OutboxWakeup | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    delivered_count: int = 0
    failure_count: int = 0
    last_error_class: str | None = None

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            messages = await asyncio.to_thread(
                self.repository.pending_outbox,
                as_of=self.clock(),
            )
            for message in messages:
                try:
                    await self.dispatcher.deliver(message)
                    await asyncio.to_thread(
                        self.repository.mark_delivered,
                        message.outbox_id,
                        delivered_at=self.clock(),
                    )
                    self.delivered_count += 1
                    self.last_error_class = None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.failure_count += 1
                    if self.last_error_class != type(exc).__name__:
                        logger.exception("trigger outbox delivery failed")
                    self.last_error_class = type(exc).__name__
                    await asyncio.to_thread(
                        self.repository.record_delivery_failure,
                        message.outbox_id,
                    )
                    break
            if self.wakeup is not None:
                await asyncio.to_thread(self.wakeup.wait, self.poll_seconds)
            else:
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
