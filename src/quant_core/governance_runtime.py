from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import ValidationError, field_validator, model_validator
from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.worker import Worker

from quant_core.analyst import assemble_codex_router
from quant_core.config import AppConfig, TemporalPolicy
from quant_core.domain import FrozenModel, _require_utc
from quant_core.governance import GovernanceSnapshot
from quant_core.governance_agent import (
    GOVERNOR_OUTPUT_ADAPTER,
    CodexGovernor,
    Governor,
    GovernorBundleBuilder,
    GovernorDecision,
    SqlGovernorDecisionStore,
)
from quant_core.governance_context import GovernanceSnapshotAssembler
from quant_core.governance_workflows import (
    BUILD_GOVERNANCE_SNAPSHOT_ACTIVITY,
    RUN_GOVERNOR_ACTIVITY,
    GovernanceCycleWorkflow,
)
from quant_core.ids import content_hash, stable_id
from quant_core.persistence import (
    SqlAccountLeaseStore,
    SqlCodexAuditStore,
    SqlGovernanceRepository,
    build_engine,
)
from quant_core.trigger import AnalysisTriggerPlan, TriggerPlanPatch
from quant_core.trigger_sql import SqlTriggerRepository
from quant_core.workflow import OrchestrationPolicySnapshot

logger = logging.getLogger(__name__)


class GovernanceWorkflowRequest(FrozenModel):
    workflow_id: str
    as_of: datetime
    governance_policy_version: str
    expected_champion_manifest_id: str
    orchestration: OrchestrationPolicySnapshot
    input_hash: str

    _utc_as_of = field_validator("as_of")(_require_utc)

    @model_validator(mode="after")
    def identity_must_match(self):
        if self.input_hash != content_hash(_request_identity(self)):
            raise ValueError("Governance Workflow input_hash 不一致")
        expected_id = stable_id(
            "governance_workflow",
            self.as_of.isoformat(),
            self.governance_policy_version,
            self.expected_champion_manifest_id,
        )
        if self.workflow_id != expected_id:
            raise ValueError("Governance Workflow workflow_id 不一致")
        return self


class GovernanceWorkflowStatus(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class GovernanceWorkflowExecution(FrozenModel):
    workflow_id: str
    status: GovernanceWorkflowStatus
    reason_code: str
    snapshot_id: str | None = None
    decision: GovernorDecision | None = None
    trigger_plan_patch: TriggerPlanPatch | None = None
    applied_trigger_plan: AnalysisTriggerPlan | None = None


def _request_identity(request: GovernanceWorkflowRequest) -> dict[str, Any]:
    return {
        "as_of": request.as_of.isoformat(),
        "governance_policy_version": request.governance_policy_version,
        "expected_champion_manifest_id": request.expected_champion_manifest_id,
        "orchestration": request.orchestration.model_dump(mode="json"),
    }


def build_governance_workflow_request(
    *,
    as_of: datetime,
    config: AppConfig,
    expected_champion_manifest_id: str,
) -> GovernanceWorkflowRequest:
    as_of = _require_utc(as_of)
    orchestration = OrchestrationPolicySnapshot.from_config(config.temporal)
    payload = {
        "as_of": as_of.isoformat(),
        "governance_policy_version": config.governance.version,
        "expected_champion_manifest_id": expected_champion_manifest_id,
        "orchestration": orchestration.model_dump(mode="json"),
    }
    return GovernanceWorkflowRequest(
        workflow_id=stable_id(
            "governance_workflow",
            as_of.isoformat(),
            config.governance.version,
            expected_champion_manifest_id,
        ),
        as_of=as_of,
        governance_policy_version=config.governance.version,
        expected_champion_manifest_id=expected_champion_manifest_id,
        orchestration=orchestration,
        input_hash=content_hash(payload),
    )


@dataclass(slots=True)
class GovernanceActivities:
    snapshots: GovernanceSnapshotAssembler
    governor: Governor

    @activity.defn(name=BUILD_GOVERNANCE_SNAPSHOT_ACTIVITY)
    def build_snapshot(self, raw_request: dict[str, Any]) -> dict[str, Any]:
        try:
            request = GovernanceWorkflowRequest.model_validate(raw_request)
        except ValidationError as exc:
            raise ApplicationError(
                "治理 Workflow 输入未通过契约校验",
                type="InvalidGovernanceInput",
                non_retryable=True,
            ) from exc
        snapshot = self.snapshots.build(as_of=request.as_of)
        if snapshot.champion.manifest_id != request.expected_champion_manifest_id:
            raise ApplicationError(
                "治理周期冻结时 Champion 已变化",
                type="InvalidGovernanceInput",
                non_retryable=True,
            )
        return {"snapshot": snapshot.model_dump(mode="json")}

    @activity.defn(name=RUN_GOVERNOR_ACTIVITY)
    def run_governor(self, raw_snapshot: dict[str, Any]) -> dict[str, Any]:
        try:
            snapshot = GovernanceSnapshot.model_validate(raw_snapshot)
        except ValidationError as exc:
            raise ApplicationError(
                "GovernanceSnapshot 未通过契约校验",
                type="InvalidGovernanceInput",
                non_retryable=True,
            ) from exc
        result = self.governor.govern(snapshot)
        if not result.success or result.decision is None:
            raise ApplicationError(result.reason_code, type="GovernorUnavailable")
        return {
            "reason_code": result.reason_code,
            "decision": result.decision.model_dump(mode="json"),
            "trigger_plan_patch": (
                result.trigger_plan_patch.model_dump(mode="json")
                if result.trigger_plan_patch is not None
                else None
            ),
            "applied_trigger_plan": (
                result.applied_trigger_plan.model_dump(mode="json")
                if result.applied_trigger_plan is not None
                else None
            ),
        }


@dataclass(slots=True)
class GovernanceTemporalCoordinator:
    client: Client
    policy: TemporalPolicy

    async def ensure(self, request: GovernanceWorkflowRequest) -> str:
        try:
            await self.client.start_workflow(
                GovernanceCycleWorkflow.run,
                request.model_dump(mode="json"),
                id=request.workflow_id,
                task_queue=self.policy.governance_task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            handle = self.client.get_workflow_handle(request.workflow_id)
            existing_hash = await handle.query(GovernanceCycleWorkflow.input_hash)
            if existing_hash != request.input_hash:
                raise ValueError("相同治理周期的冻结输入不同") from None
        return request.workflow_id

    async def execute(self, request: GovernanceWorkflowRequest) -> GovernanceWorkflowExecution:
        await self.ensure(request)
        raw = await self.client.get_workflow_handle(request.workflow_id).result()
        return GovernanceWorkflowExecution.model_validate(raw)


class GovernanceTemporalWorker:
    def __init__(
        self,
        client: Client,
        policy: TemporalPolicy,
        activities: GovernanceActivities,
    ) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="governance-activity",
        )
        self._worker = Worker(
            client,
            task_queue=policy.governance_task_queue,
            workflows=[GovernanceCycleWorkflow],
            activities=[activities.build_snapshot, activities.run_governor],
            activity_executor=self._executor,
            max_concurrent_activities=1,
        )

    async def __aenter__(self) -> GovernanceTemporalWorker:
        await self._worker.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            await self._worker.__aexit__(exc_type, exc, tb)
        finally:
            self._executor.shutdown(wait=True, cancel_futures=True)


@dataclass(slots=True)
class GovernanceSupervisor:
    coordinator: GovernanceTemporalCoordinator
    config: AppConfig
    repository: SqlGovernanceRepository
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    last_workflow_id: str | None = None
    last_error_class: str | None = None

    async def run(self, stop: asyncio.Event) -> None:
        interval_seconds = self.config.governance.cycle_interval_hours * 3600
        while not stop.is_set():
            now = _require_utc(self.clock())
            bucket = datetime.fromtimestamp(
                int(now.timestamp()) // interval_seconds * interval_seconds,
                tz=UTC,
            )
            try:
                request = build_governance_workflow_request(
                    as_of=bucket,
                    config=self.config,
                    expected_champion_manifest_id=(self.repository.get_champion().manifest_id),
                )
                self.last_workflow_id = await self.coordinator.ensure(request)
                self.last_error_class = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.last_error_class != type(exc).__name__:
                    logger.exception("governance supervisor failed")
                self.last_error_class = type(exc).__name__
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)


def assemble_governance(
    config: AppConfig,
    database_url: str,
    client: Client,
    *,
    project_root: Path,
) -> tuple[GovernanceTemporalWorker, GovernanceSupervisor]:
    engine = build_engine(database_url)
    router = assemble_codex_router(
        config,
        leases=SqlAccountLeaseStore(engine),
        audit=SqlCodexAuditStore(engine),
        output_adapter=GOVERNOR_OUTPUT_ADAPTER,
    )
    config.codex_runtime.bundle_root.mkdir(parents=True, exist_ok=True)
    governor = CodexGovernor(
        bundle_root=config.codex_runtime.bundle_root,
        bundle_builder=GovernorBundleBuilder(
            config.codex_runtime,
            prompt_path=project_root / "config" / "governor_prompt.md",
        ),
        router=router,
        decisions=SqlGovernorDecisionStore(engine),
        trigger_plans=SqlTriggerRepository(engine, config.trigger),
    )
    activities = GovernanceActivities(
        snapshots=GovernanceSnapshotAssembler(
            engine,
            config,
            project_root=project_root,
        ),
        governor=governor,
    )
    coordinator = GovernanceTemporalCoordinator(client, config.temporal)
    return (
        GovernanceTemporalWorker(client, config.temporal, activities),
        GovernanceSupervisor(
            coordinator=coordinator,
            config=config,
            repository=SqlGovernanceRepository(engine),
        ),
    )
