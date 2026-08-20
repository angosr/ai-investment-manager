from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import ValidationError, model_validator
from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

from investment_manager.binance_testnet import BinanceTradingStateSource, assemble_binance_testnet
from investment_manager.config import (
    AppConfig,
    DeploymentStage,
    ReconciliationPolicy,
    TemporalPolicy,
)
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.platform.database import build_engine
from investment_manager.reconciliation import (
    ReconciliationEngine,
    ReconciliationReport,
    ReconciliationReportStore,
    TradingStateSource,
)
from investment_manager.reconciliation_sql import (
    SqlLocalTradingStateSource,
    SqlMockExchangeTruthSource,
    SqlReconciliationReportStore,
)
from investment_manager.reconciliation_workflows import (
    RECONCILIATION_ACTIVITY_NAME,
    ReconciliationWorkflow,
)
from investment_manager.temporal_worker import SingleActivityWorker
from investment_manager.workflow import OrchestrationPolicySnapshot

logger = logging.getLogger(__name__)


class ReconciliationWorkflowRequest(FrozenModel):
    workflow_id: str
    as_of: datetime
    policy: ReconciliationPolicy
    orchestration: OrchestrationPolicySnapshot
    input_hash: str

    @model_validator(mode="after")
    def identity_must_match(self):
        as_of = require_utc(self.as_of)
        expected_hash = content_hash(_request_identity(self))
        if self.input_hash != expected_hash:
            raise ValueError("Reconciliation Workflow input_hash 不一致")
        expected_id = stable_id("reconciliation_workflow", as_of.isoformat(), self.policy.version)
        if self.workflow_id != expected_id:
            raise ValueError("Reconciliation Workflow workflow_id 不一致")
        return self


class ReconciliationWorkflowStatus(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ReconciliationWorkflowExecution(FrozenModel):
    workflow_id: str
    status: ReconciliationWorkflowStatus
    reason_code: str
    attempt: int
    report: ReconciliationReport | None = None


def _request_identity(request: ReconciliationWorkflowRequest) -> dict[str, Any]:
    return {
        "as_of": request.as_of.isoformat(),
        "policy": request.policy.model_dump(mode="json"),
        "orchestration": request.orchestration.model_dump(mode="json"),
    }


def build_reconciliation_workflow_request(
    *,
    as_of: datetime,
    reconciliation_policy: ReconciliationPolicy,
    temporal_policy: TemporalPolicy,
) -> ReconciliationWorkflowRequest:
    as_of = require_utc(as_of)
    orchestration = OrchestrationPolicySnapshot.from_config(temporal_policy)
    payload = {
        "as_of": as_of.isoformat(),
        "policy": reconciliation_policy.model_dump(mode="json"),
        "orchestration": orchestration.model_dump(mode="json"),
    }
    return ReconciliationWorkflowRequest(
        workflow_id=stable_id(
            "reconciliation_workflow", as_of.isoformat(), reconciliation_policy.version
        ),
        as_of=as_of,
        policy=reconciliation_policy,
        orchestration=orchestration,
        input_hash=content_hash(payload),
    )


@dataclass(slots=True)
class ReconciliationActivities:
    local: TradingStateSource
    remote: TradingStateSource
    reports: ReconciliationReportStore

    @activity.defn(name=RECONCILIATION_ACTIVITY_NAME)
    def reconcile(self, raw_request: dict[str, Any]) -> dict[str, Any]:
        try:
            request = ReconciliationWorkflowRequest.model_validate(raw_request)
        except ValidationError as exc:
            raise ApplicationError(
                "对账 Workflow 输入未通过契约校验",
                type="InvalidReconciliationInput",
                non_retryable=True,
            ) from exc
        engine = ReconciliationEngine(request.policy)
        local = self.local.snapshot(as_of=request.as_of)
        try:
            remote = self.remote.snapshot(as_of=request.as_of)
        except Exception as exc:
            report = engine.unknown(
                local,
                as_of=request.as_of,
                reason_code=type(exc).__name__,
            )
        else:
            report = engine.compare(local, remote, as_of=request.as_of)
        self.reports.record(report)
        return {
            "attempt": activity.info().attempt,
            "report": report.model_dump(mode="json"),
        }


@dataclass(slots=True)
class ReconciliationTemporalCoordinator:
    client: Client
    policy: TemporalPolicy

    async def execute(
        self, request: ReconciliationWorkflowRequest
    ) -> ReconciliationWorkflowExecution:
        payload = request.model_dump(mode="json")
        try:
            raw = await self.client.execute_workflow(
                ReconciliationWorkflow.run,
                payload,
                id=request.workflow_id,
                task_queue=self.policy.reconciliation_task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            handle = self.client.get_workflow_handle(request.workflow_id)
            existing_hash = await handle.query(ReconciliationWorkflow.input_hash)
            if existing_hash != request.input_hash:
                raise ValueError("相同对账 Workflow ID 的冻结输入不同") from None
            raw = await handle.result()
        return ReconciliationWorkflowExecution.model_validate(raw)


class ReconciliationTemporalWorker(SingleActivityWorker):
    def __init__(
        self,
        client: Client,
        policy: TemporalPolicy,
        activities: ReconciliationActivities,
    ) -> None:
        super().__init__(
            client,
            task_queue=policy.reconciliation_task_queue,
            workflows=[ReconciliationWorkflow],
            activities=[activities.reconcile],
            thread_name_prefix="reconciliation-activity",
        )


@dataclass(slots=True)
class ReconciliationSupervisorHealth:
    runs: int = 0
    matched: int = 0
    frozen: int = 0
    last_report_id: str | None = None
    last_error_class: str | None = None


@dataclass(slots=True)
class ReconciliationSupervisor:
    coordinator: ReconciliationTemporalCoordinator
    reconciliation_policy: ReconciliationPolicy
    temporal_policy: TemporalPolicy
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    health: ReconciliationSupervisorHealth = field(default_factory=ReconciliationSupervisorHealth)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            now = require_utc(self.clock())
            bucket_seconds = self.reconciliation_policy.poll_seconds
            bucket = datetime.fromtimestamp(
                int(now.timestamp()) // bucket_seconds * bucket_seconds,
                tz=UTC,
            )
            try:
                request = build_reconciliation_workflow_request(
                    as_of=bucket,
                    reconciliation_policy=self.reconciliation_policy,
                    temporal_policy=self.temporal_policy,
                )
                result = await self.coordinator.execute(request)
                self.health.runs += 1
                if result.report is not None:
                    self.health.last_report_id = result.report.report_id
                    if result.report.freeze_new_risk:
                        self.health.frozen += 1
                    else:
                        self.health.matched += 1
                self.health.last_error_class = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.health.last_error_class != type(exc).__name__:
                    logger.exception("reconciliation supervisor failed")
                self.health.last_error_class = type(exc).__name__
            delay = _seconds_until_next_bucket(
                require_utc(self.clock()),
                bucket_seconds=bucket_seconds,
            )
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)


def _seconds_until_next_bucket(now: datetime, *, bucket_seconds: int) -> float:
    """按绝对 UTC 桶调度，避免每轮执行耗时累积为对账漂移。"""

    elapsed = now.timestamp()
    next_boundary = (int(elapsed) // bucket_seconds + 1) * bucket_seconds
    return max(0.001, next_boundary - elapsed)


def assemble_reconciliation(
    config: AppConfig,
    database_url: str,
    client: Client,
) -> tuple[ReconciliationTemporalWorker, ReconciliationSupervisor]:
    if config.deployment.stage not in {
        DeploymentStage.MOCK,
        DeploymentStage.SHADOW,
        DeploymentStage.TESTNET,
    }:
        raise ValueError("不支持的对账阶段")
    engine = build_engine(database_url)
    local = SqlLocalTradingStateSource(
        engine,
        initial_quote_balance=config.shadow.initial_quote_balance,
        bootstrap_from_reconciliation=(config.deployment.stage == DeploymentStage.TESTNET),
    )
    if config.deployment.stage == DeploymentStage.TESTNET:
        binance_client, binance_exchange = assemble_binance_testnet(config)
        remote: TradingStateSource = BinanceTradingStateSource(
            binance_client,
            binance_exchange,
            local,
            symbols=config.market_data.symbols,
            quote_asset=config.binance_testnet.quote_asset,
        )
    else:
        remote = SqlMockExchangeTruthSource(
            engine,
            initial_quote_balance=config.shadow.initial_quote_balance,
        )
    activities = ReconciliationActivities(
        local=local,
        remote=remote,
        reports=SqlReconciliationReportStore(engine),
    )
    coordinator = ReconciliationTemporalCoordinator(client, config.temporal)
    return (
        ReconciliationTemporalWorker(client, config.temporal, activities),
        ReconciliationSupervisor(
            coordinator=coordinator,
            reconciliation_policy=config.reconciliation,
            temporal_policy=config.temporal,
        ),
    )
