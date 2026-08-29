from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

from investment_manager.scheduling.models import (
    AnalysisTriggerPlan,
)
from investment_manager.scheduling.policy import TemporalPolicy
from investment_manager.scheduling.repository import SqlTriggerRepository, TriggerOutboxMessage
from investment_manager.scheduling.workflows import (
    TRIGGER_SIGNAL,
    TriggerCoordinatorWorkflow,
    coordinator_workflow_id,
)
from investment_manager.settings import AppConfig

logger = logging.getLogger(__name__)


class OutboxWakeup(Protocol):
    def wait(self, timeout_seconds: float) -> bool: ...


def build_trigger_coordinator_input(
    plan: AnalysisTriggerPlan,
    config: AppConfig,
) -> dict[str, Any]:
    return {
        "workflow_id": coordinator_workflow_id(plan.symbol, plan.pipeline_id),
        "plan": plan.model_dump(mode="json"),
        "settings": {
            "trigger_policy_version": config.trigger.version,
            "maximum_batch_size": config.trigger.maximum_batch_size,
            "maximum_pending_triggers": config.trigger.maximum_pending_triggers,
            "trigger_expiry_seconds": config.trigger.trigger_expiry_seconds,
            "analysis_deadline_seconds": config.shadow.analysis_deadline_seconds,
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
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            )
        handle = self.client.get_workflow_handle(workflow_id)
        await handle.signal(TRIGGER_SIGNAL, message.payload)


async def terminate_inactive_trigger_coordinators(
    *,
    client: Client,
    active_symbols: tuple[str, ...],
    active_pipeline_id: str,
) -> tuple[str, ...]:
    """Keep only this release's coordinators in its dedicated namespace.

    Discovering workflows from Temporal, rather than reconstructing their ids from
    the current fact database, also removes coordinators orphaned by a database
    migration or an interrupted release cutover.
    """

    active_ids = {
        coordinator_workflow_id(symbol, active_pipeline_id) for symbol in active_symbols
    }
    terminated: list[str] = []
    executions = client.list_workflows(
        'WorkflowType="TriggerCoordinatorWorkflow" AND ExecutionStatus="Running"'
    )
    async for execution in executions:
        if execution.id in active_ids:
            continue
        handle = client.get_workflow_handle(execution.id)
        try:
            await handle.terminate(f"superseded by pipeline {active_pipeline_id}")
        except RPCError as exc:
            if exc.status not in {
                RPCStatusCode.NOT_FOUND,
                RPCStatusCode.FAILED_PRECONDITION,
            }:
                raise
            continue
        terminated.append(execution.id)
    return tuple(sorted(terminated))


class TriggerTemporalWorker:
    def __init__(
        self,
        client: Client,
        policy: TemporalPolicy,
        activities: Any,
    ) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="trigger-activity")
        self._worker = Worker(
            client,
            task_queue=policy.trigger_task_queue,
            workflows=[TriggerCoordinatorWorkflow],
            activities=[activities.build_analysis_dispatches],
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
