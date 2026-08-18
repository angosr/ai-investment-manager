from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, ValidationError, model_validator
from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.worker import Worker

from quant_core.binance_testnet import assemble_binance_testnet
from quant_core.config import AppConfig, DeploymentStage, TemporalPolicy
from quant_core.domain import FrozenModel, PositionLifecycle, _require_utc
from quant_core.ids import content_hash, stable_id
from quant_core.lifecycle import (
    OpenLifecycleRecord,
    OpenLifecycleRepository,
    PositionLifecycleManager,
)
from quant_core.lifecycle_workflows import (
    LIFECYCLE_ACTIVITY_NAME,
    PositionLifecycleWorkflow,
)
from quant_core.market_data import MarketDataStore
from quant_core.market_data_sql import SqlMarketDataStore
from quant_core.mock_exchange_sql import SqlMockExchange
from quant_core.persistence import (
    SqlLifecycleLedger,
    SqlOpenLifecycleRepository,
    SqlRiskBudgetStore,
    build_engine,
)
from quant_core.reconciliation import MockReconciler
from quant_core.shadow import ShadowStateReader, SqlShadowStateReader
from quant_core.workflow import OrchestrationPolicySnapshot

logger = logging.getLogger(__name__)


class LifecycleWorkflowRequest(FrozenModel):
    workflow_id: str
    initial_lifecycle: PositionLifecycle
    lifecycle: PositionLifecycle
    pipeline_version: str
    poll_seconds: int = Field(ge=1, le=300)
    orchestration: OrchestrationPolicySnapshot
    input_hash: str

    @model_validator(mode="after")
    def identity_must_match(self):
        if (
            self.lifecycle.position_id != self.initial_lifecycle.position_id
            or self.lifecycle.cycle_id != self.initial_lifecycle.cycle_id
        ):
            raise ValueError("Lifecycle 当前状态与初始身份不一致")
        expected_hash = content_hash(_request_identity(self))
        if self.input_hash != expected_hash:
            raise ValueError("Lifecycle Workflow input_hash 不一致")
        expected_id = stable_id("position_lifecycle_workflow", self.initial_lifecycle.position_id)
        if self.workflow_id != expected_id:
            raise ValueError("Lifecycle Workflow workflow_id 不一致")
        return self


class LifecycleWorkflowStatus(StrEnum):
    CLOSED = "CLOSED"
    FAILED = "FAILED"


class LifecycleWorkflowExecution(FrozenModel):
    workflow_id: str
    status: LifecycleWorkflowStatus
    reason_code: str
    lifecycle: PositionLifecycle | None = None


def _request_identity(request: LifecycleWorkflowRequest) -> dict[str, Any]:
    return {
        "initial_lifecycle": request.initial_lifecycle.model_dump(mode="json"),
        "pipeline_version": request.pipeline_version,
        "poll_seconds": request.poll_seconds,
        "orchestration": request.orchestration.model_dump(mode="json"),
    }


def build_lifecycle_workflow_request(
    record: OpenLifecycleRecord,
    *,
    temporal_policy: TemporalPolicy,
    poll_seconds: int,
) -> LifecycleWorkflowRequest:
    workflow_id = stable_id("position_lifecycle_workflow", record.lifecycle.position_id)
    orchestration = OrchestrationPolicySnapshot.from_config(temporal_policy)
    payload = {
        "initial_lifecycle": record.lifecycle.model_dump(mode="json"),
        "pipeline_version": record.pipeline_version,
        "poll_seconds": poll_seconds,
        "orchestration": orchestration.model_dump(mode="json"),
    }
    return LifecycleWorkflowRequest(
        workflow_id=workflow_id,
        initial_lifecycle=record.lifecycle,
        lifecycle=record.lifecycle,
        pipeline_version=record.pipeline_version,
        poll_seconds=poll_seconds,
        orchestration=orchestration,
        input_hash=content_hash(payload),
    )


@dataclass(slots=True)
class PositionLifecycleActivities:
    config: AppConfig
    market_store: MarketDataStore
    state: ShadowStateReader
    manager: PositionLifecycleManager
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    @activity.defn(name=LIFECYCLE_ACTIVITY_NAME)
    def evaluate(self, raw_request: dict[str, Any]) -> dict[str, Any]:
        try:
            request = LifecycleWorkflowRequest.model_validate(raw_request)
        except ValidationError as exc:
            raise ApplicationError(
                "持仓 Workflow 输入未通过契约校验",
                type="InvalidLifecycleInput",
                non_retryable=True,
            ) from exc
        now = _require_utc(self.clock())
        lifecycle = request.lifecycle
        market = self.market_store.snapshot(
            cycle_id=lifecycle.cycle_id,
            symbol=lifecycle.symbol,
            interval=self.config.market_data.interval,
            as_of=now,
            bar_window=self.config.market_data.bar_window,
            source=self.config.market_data.version,
        )
        market_age = (now - market.observed_at).total_seconds()
        if market_age > self.config.risk.maximum_market_age_seconds:
            raise ApplicationError("行情已过期", type="StaleMarketData")
        account = self.state.account_for_cycle(
            cycle_id=lifecycle.cycle_id,
            as_of=now,
            initial_quote_balance=self.config.shadow.initial_quote_balance,
        )
        result = self.manager.evaluate(
            lifecycle=lifecycle,
            market=market,
            account=account,
            pipeline_version=request.pipeline_version,
        )
        return {
            "closed": result.lifecycle.status.value == "CLOSED",
            "reason_code": (
                result.outcome.exit_reason.value if result.outcome is not None else "POSITION_OPEN"
            ),
            "lifecycle": result.lifecycle.model_dump(mode="json"),
        }


@dataclass(slots=True)
class LifecycleTemporalCoordinator:
    client: Client
    policy: TemporalPolicy

    async def ensure(self, request: LifecycleWorkflowRequest) -> str:
        payload = request.model_dump(mode="json")
        try:
            await self.client.start_workflow(
                PositionLifecycleWorkflow.run,
                payload,
                id=request.workflow_id,
                task_queue=self.policy.lifecycle_task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            handle = self.client.get_workflow_handle(request.workflow_id)
            existing_hash = await handle.query(PositionLifecycleWorkflow.input_hash)
            if existing_hash != request.input_hash:
                raise ValueError("相同 position_id 的 Lifecycle 冻结输入不同") from None
        return request.workflow_id


class LifecycleTemporalWorker:
    def __init__(
        self,
        client: Client,
        policy: TemporalPolicy,
        activities: PositionLifecycleActivities,
    ) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lifecycle-activity")
        self._worker = Worker(
            client,
            task_queue=policy.lifecycle_task_queue,
            workflows=[PositionLifecycleWorkflow],
            activities=[activities.evaluate],
            activity_executor=self._executor,
            max_concurrent_activities=1,
        )

    async def run(self) -> None:
        try:
            await self._worker.run()
        finally:
            self._executor.shutdown(wait=True, cancel_futures=True)

    async def __aenter__(self) -> LifecycleTemporalWorker:
        await self._worker.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            await self._worker.__aexit__(exc_type, exc, tb)
        finally:
            self._executor.shutdown(wait=True, cancel_futures=True)


@dataclass(slots=True)
class LifecycleSupervisorHealth:
    scans: int = 0
    ensure_calls: int = 0
    last_error_class: str | None = None
    active_workflow_ids: set[str] = field(default_factory=set)


class LifecycleSupervisor:
    def __init__(
        self,
        repository: OpenLifecycleRepository,
        coordinator: LifecycleTemporalCoordinator,
        policy: TemporalPolicy,
        *,
        poll_seconds: int,
    ) -> None:
        self._repository = repository
        self._coordinator = coordinator
        self._policy = policy
        self._poll_seconds = poll_seconds
        self.health = LifecycleSupervisorHealth()

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            self.health.scans += 1
            try:
                records = self._repository.list_open()
                active: set[str] = set()
                for record in records:
                    request = build_lifecycle_workflow_request(
                        record,
                        temporal_policy=self._policy,
                        poll_seconds=self._poll_seconds,
                    )
                    active.add(await self._coordinator.ensure(request))
                    self.health.ensure_calls += 1
                self.health.active_workflow_ids = active
                self.health.last_error_class = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.health.last_error_class != type(exc).__name__:
                    logger.exception("lifecycle supervisor failed")
                self.health.last_error_class = type(exc).__name__
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._poll_seconds)


def assemble_lifecycle_activities(
    config: AppConfig,
    database_url: str,
) -> PositionLifecycleActivities:
    if config.deployment.stage not in {
        DeploymentStage.MOCK,
        DeploymentStage.SHADOW,
        DeploymentStage.TESTNET,
    }:
        raise ValueError("不支持的持仓生命周期阶段")
    engine = build_engine(database_url)
    exchange = (
        assemble_binance_testnet(config)[1]
        if config.deployment.stage == DeploymentStage.TESTNET
        else SqlMockExchange(engine, config.execution)
    )
    return PositionLifecycleActivities(
        config=config,
        market_store=SqlMarketDataStore(engine),
        state=SqlShadowStateReader(
            engine,
            maximum_reconciliation_age_seconds=(config.reconciliation.maximum_report_age_seconds),
        ),
        manager=PositionLifecycleManager(
            exchange=exchange,
            reconciler=MockReconciler(),
            risk_budget=SqlRiskBudgetStore(engine),
            lifecycle_ledger=SqlLifecycleLedger(engine),
        ),
    )


def assemble_lifecycle_supervisor(
    config: AppConfig,
    database_url: str,
    client: Client,
) -> LifecycleSupervisor:
    engine = build_engine(database_url)
    return LifecycleSupervisor(
        SqlOpenLifecycleRepository(engine),
        LifecycleTemporalCoordinator(client, config.temporal),
        config.temporal,
        poll_seconds=config.shadow.lifecycle_poll_seconds,
    )
