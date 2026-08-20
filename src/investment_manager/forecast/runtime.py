from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

from investment_manager.forecast.analyst import assemble_codex_context_analyst
from investment_manager.forecast.application import (
    AssessmentApplication,
    AssessmentWorkflowExecution,
)
from investment_manager.forecast.codex_repository import (
    SqlAccountLeaseStore,
    SqlCodexAuditStore,
)
from investment_manager.forecast.execution import ContextAssessmentExecutor
from investment_manager.forecast.repository import SqlContextAssessmentStore
from investment_manager.forecast.workflows import (
    ASSESSMENT_ACTIVITY_NAME,
    AssessmentWorkflowRequest,
    ContextAssessmentWorkflow,
)
from investment_manager.platform.database import build_engine, require_current_schema
from investment_manager.platform.temporal import SingleActivityWorker
from investment_manager.scheduling.policy import TemporalPolicy
from investment_manager.settings import AppConfig


@dataclass(slots=True)
class AssessmentActivities:
    application: AssessmentApplication

    @activity.defn(name=ASSESSMENT_ACTIVITY_NAME)
    def execute_context_assessment(self, raw_request: dict[str, Any]) -> dict[str, Any]:
        try:
            request = AssessmentWorkflowRequest.model_validate(raw_request)
        except ValidationError as exc:
            raise ApplicationError(
                "ContextAssessment Workflow 输入未通过契约校验",
                type="InvalidWorkflowInput",
                non_retryable=True,
            ) from exc
        try:
            execution = self.application.execute(request.command)
        except ValueError as exc:
            raise ApplicationError(
                "ContextAssessment command 与权威事实冲突",
                type="PermanentDomainError",
                non_retryable=True,
            ) from exc
        return {
            "attempt": activity.info().attempt,
            "execution": execution.model_dump(mode="json"),
        }


@dataclass(slots=True)
class AssessmentTemporalCoordinator:
    client: Client
    policy: TemporalPolicy

    @classmethod
    async def connect(cls, policy: TemporalPolicy) -> AssessmentTemporalCoordinator:
        client = await Client.connect(policy.address, namespace=policy.namespace)
        return cls(client=client, policy=policy)

    async def execute(
        self,
        request: AssessmentWorkflowRequest,
    ) -> AssessmentWorkflowExecution:
        payload = request.model_dump(mode="json")
        try:
            raw_result = await self.client.execute_workflow(
                ContextAssessmentWorkflow.run,
                payload,
                id=request.workflow_id,
                task_queue=self.policy.assessment_task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            handle = self.client.get_workflow_handle(request.workflow_id)
            existing_hash = await handle.query(ContextAssessmentWorkflow.input_hash)
            if existing_hash != request.input_hash:
                raise ValueError(
                    "相同 ContextAssessment workflow_id 的冻结输入不同"
                ) from None
            raw_result = await handle.result()
        return AssessmentWorkflowExecution.model_validate(raw_result)


class AssessmentTemporalWorker(SingleActivityWorker):
    def __init__(
        self,
        client: Client,
        policy: TemporalPolicy,
        application: AssessmentApplication,
        *,
        worker_threads: int,
    ) -> None:
        super().__init__(
            client,
            task_queue=policy.assessment_task_queue,
            workflows=[ContextAssessmentWorkflow],
            activities=[
                AssessmentActivities(application).execute_context_assessment,
            ],
            thread_name_prefix="assessment-activity",
            max_concurrent_activities=worker_threads,
        )


def assemble_assessment_application(
    config: AppConfig,
    database_url: str,
    *,
    code_version: str,
) -> AssessmentApplication:
    engine = build_engine(database_url)
    require_current_schema(engine)
    analyst = assemble_codex_context_analyst(
        config,
        bundle_root=config.codex_runtime.bundle_root,
        code_version=code_version,
        leases=SqlAccountLeaseStore(engine),
        audit=SqlCodexAuditStore(engine),
    )
    config.codex_runtime.bundle_root.mkdir(parents=True, exist_ok=True)
    return AssessmentApplication(
        ContextAssessmentExecutor(
            SqlContextAssessmentStore(engine),
            analyst,
        )
    )


async def run_assessment_worker_forever(
    *,
    config: AppConfig,
    application: AssessmentApplication,
) -> None:
    coordinator = await AssessmentTemporalCoordinator.connect(config.temporal)
    enabled_accounts = sum(account.enabled for account in config.codex_accounts.accounts)
    if not config.codex_runtime.enabled or enabled_accounts < 1:
        raise ValueError("ContextAssessment Worker 需要已验证且启用的 Codex 账号")
    worker_threads = min(config.temporal.worker_threads, enabled_accounts)
    async with AssessmentTemporalWorker(
        coordinator.client,
        config.temporal,
        application,
        worker_threads=worker_threads,
    ):
        await asyncio.Event().wait()


def run_assessment_worker_process(
    *,
    config: AppConfig,
    application: AssessmentApplication,
) -> None:
    asyncio.run(
        run_assessment_worker_forever(
            config=config,
            application=application,
        )
    )
