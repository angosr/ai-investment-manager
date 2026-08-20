from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.worker import Worker

from investment_manager.analyst import assemble_codex_analyst
from investment_manager.cycle import AnalysisCycle
from investment_manager.execution.binance import assemble_binance_testnet
from investment_manager.execution.contracts import ExecutionRequest
from investment_manager.execution.mock_repository import SqlMockExchange
from investment_manager.forecast.codex_repository import (
    SqlAccountLeaseStore,
    SqlCodexAuditStore,
)
from investment_manager.forecast.policy import AiMode
from investment_manager.governance.policy import DeploymentStage
from investment_manager.legacy.repository import SqlFactLedger
from investment_manager.platform.database import build_engine
from investment_manager.risk.budget import SqlRiskBudgetStore
from investment_manager.scheduling.policy import TemporalPolicy
from investment_manager.settings import AppConfig
from investment_manager.temporal_workflows import (
    EXECUTION_ACTIVITY_NAME,
    PREPARE_ACTIVITY_NAME,
    AnalysisCycleWorkflow,
    ExecutionWorkflow,
)
from investment_manager.workflow import WorkflowExecution, WorkflowRequest


@dataclass(slots=True)
class AnalysisCycleActivities:
    cycle: AnalysisCycle
    execution_clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    @activity.defn(name=PREPARE_ACTIVITY_NAME)
    def prepare_analysis_decision(self, raw_request: dict[str, Any]) -> dict[str, Any]:
        try:
            request = WorkflowRequest.model_validate(raw_request)
        except ValidationError as exc:
            raise ApplicationError(
                "冻结的 Workflow 输入未通过契约校验",
                type="InvalidWorkflowInput",
                non_retryable=True,
            ) from exc
        try:
            result = self.cycle.prepare(request.cycle_input, trigger=request.trigger)
        except ValueError as exc:
            raise ApplicationError(
                "周期输入与既有业务事实冲突",
                type="PermanentDomainError",
                non_retryable=True,
            ) from exc
        return {
            "attempt": activity.info().attempt,
            "cycle_result": result.model_dump(mode="json"),
        }

    @activity.defn(name=EXECUTION_ACTIVITY_NAME)
    def execute_trade_request(self, raw_request: dict[str, Any]) -> dict[str, Any]:
        try:
            request = ExecutionRequest.model_validate(raw_request)
        except ValidationError as exc:
            raise ApplicationError(
                "ExecutionRequest 未通过契约校验",
                type="InvalidWorkflowInput",
                non_retryable=True,
            ) from exc
        try:
            result = self.cycle.execute(request, observed_at=self.execution_clock())
        except ValueError as exc:
            raise ApplicationError(
                "ExecutionRequest 与冻结事实冲突",
                type="PermanentDomainError",
                non_retryable=True,
            ) from exc
        return {
            "attempt": activity.info().attempt,
            "cycle_result": result.model_dump(mode="json"),
        }


@dataclass(slots=True)
class TemporalAnalysisCoordinator:
    client: Client
    policy: TemporalPolicy

    @classmethod
    async def connect(cls, policy: TemporalPolicy) -> TemporalAnalysisCoordinator:
        client = await Client.connect(policy.address, namespace=policy.namespace)
        return cls(client=client, policy=policy)

    async def execute(self, request: WorkflowRequest) -> WorkflowExecution:
        payload = request.model_dump(mode="json")
        try:
            raw_result = await self.client.execute_workflow(
                AnalysisCycleWorkflow.run,
                payload,
                id=request.workflow_id,
                task_queue=self.policy.task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            handle = self.client.get_workflow_handle(request.workflow_id)
            existing_hash = await handle.query(AnalysisCycleWorkflow.input_hash)
            if existing_hash != request.input_hash:
                raise ValueError("相同 cycle_id 的 Temporal Workflow 冻结输入不同") from None
            raw_result = await handle.result()
        return WorkflowExecution.model_validate(raw_result)


class AnalysisTemporalWorker:
    """一个受监督的 Worker 角色；线程池只承载同步、可幂等 Activity。"""

    def __init__(
        self,
        client: Client,
        policy: TemporalPolicy,
        cycle: AnalysisCycle,
        *,
        execution_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=policy.worker_threads,
            thread_name_prefix="analysis-activity",
        )
        activities = AnalysisCycleActivities(cycle, execution_clock)
        self._worker = Worker(
            client,
            task_queue=policy.task_queue,
            workflows=[AnalysisCycleWorkflow, ExecutionWorkflow],
            activities=[
                activities.prepare_analysis_decision,
                activities.execute_trade_request,
            ],
            activity_executor=self._executor,
            max_concurrent_activities=policy.worker_threads,
        )

    async def run(self) -> None:
        try:
            await self._worker.run()
        finally:
            self._executor.shutdown(wait=True, cancel_futures=True)

    async def __aenter__(self) -> AnalysisTemporalWorker:
        await self._worker.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            await self._worker.__aexit__(exc_type, exc, tb)
        finally:
            self._executor.shutdown(wait=True, cancel_futures=True)


async def run_worker_forever(policy: TemporalPolicy, cycle: AnalysisCycle) -> None:
    coordinator = await TemporalAnalysisCoordinator.connect(policy)
    worker = AnalysisTemporalWorker(coordinator.client, policy, cycle)
    await worker.run()


def run_worker_process(policy: TemporalPolicy, cycle: AnalysisCycle) -> None:
    asyncio.run(run_worker_forever(policy, cycle))


def assemble_simulated_analysis_cycle(
    config: AppConfig,
    database_url: str,
    *,
    code_version: str,
) -> AnalysisCycle:
    """装配持久化模拟执行；MOCK/SHADOW 永不接触真实订单接口。"""

    if config.deployment.stage not in {DeploymentStage.MOCK, DeploymentStage.SHADOW}:
        raise ValueError("模拟 Temporal Worker 只允许 MOCK 或 SHADOW 阶段")
    engine = build_engine(database_url)
    analyst = None
    if config.pipeline.ai_mode == AiMode.PROPOSE:
        analyst = assemble_codex_analyst(
            config,
            bundle_root=config.codex_runtime.bundle_root,
            code_version=code_version,
            leases=SqlAccountLeaseStore(engine),
            audit=SqlCodexAuditStore(engine),
        )
        config.codex_runtime.bundle_root.mkdir(parents=True, exist_ok=True)
    return AnalysisCycle.with_adapters(
        config,
        ledger=SqlFactLedger(engine),
        exchange=SqlMockExchange(engine, config.execution),
        risk_budget=SqlRiskBudgetStore(engine),
        analyst=analyst,
    )


def assemble_analysis_cycle(
    config: AppConfig,
    database_url: str,
    *,
    code_version: str,
) -> AnalysisCycle:
    """按部署阶段装配唯一执行边界；LIVE 始终被配置层拒绝。"""

    if config.deployment.stage in {DeploymentStage.MOCK, DeploymentStage.SHADOW}:
        return assemble_simulated_analysis_cycle(
            config,
            database_url,
            code_version=code_version,
        )
    if config.deployment.stage != DeploymentStage.TESTNET:
        raise ValueError("不支持的执行阶段")
    engine = build_engine(database_url)
    analyst = None
    if config.pipeline.ai_mode == AiMode.PROPOSE:
        analyst = assemble_codex_analyst(
            config,
            bundle_root=config.codex_runtime.bundle_root,
            code_version=code_version,
            leases=SqlAccountLeaseStore(engine),
            audit=SqlCodexAuditStore(engine),
        )
        config.codex_runtime.bundle_root.mkdir(parents=True, exist_ok=True)
    _, exchange = assemble_binance_testnet(config)
    return AnalysisCycle.with_adapters(
        config,
        ledger=SqlFactLedger(engine),
        exchange=exchange,
        risk_budget=SqlRiskBudgetStore(engine),
        analyst=analyst,
    )
