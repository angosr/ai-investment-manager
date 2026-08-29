from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

from investment_manager.forecast.codex.repository import (
    SqlAccountLeaseStore,
    SqlCodexAuditStore,
)
from investment_manager.forecast.context.analyst import assemble_codex_context_analyst
from investment_manager.forecast.context.application import (
    AssessmentApplication,
    AssessmentWorkflowExecution,
)
from investment_manager.forecast.context.executor import ContextAssessmentExecutor
from investment_manager.forecast.context.posterior_analyst import (
    assemble_codex_context_posterior_analyst,
)
from investment_manager.forecast.context.posterior_execution import (
    ContextPosteriorApplication,
)
from investment_manager.forecast.context.posterior_workflow import (
    POSTERIOR_ACTIVITY_NAME,
    ContextPosteriorWorkflow,
    PosteriorWorkflowRequest,
)
from investment_manager.forecast.context.repository import SqlContextAssessmentStore
from investment_manager.forecast.context.workflow import (
    ASSESSMENT_ACTIVITY_NAME,
    AssessmentWorkflowRequest,
    ContextAssessmentWorkflow,
)
from investment_manager.forecast.contract_repository import SqlForecastContractStore
from investment_manager.forecast.models import ContextAssessment
from investment_manager.forecast.program.baseline import load_forecast_baseline
from investment_manager.forecast.program.prior import build_prior_targets
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.governance.models import ReleaseManifest, resolve_manifest_artifact
from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.platform.database import build_engine, require_current_schema
from investment_manager.platform.temporal import SingleActivityWorker
from investment_manager.scheduling.models import (
    AddWakeup,
    AnalysisTriggerType,
    DeleteWakeup,
    ScheduledWakeup,
    build_trigger_event,
    build_trigger_plan_patch,
)
from investment_manager.scheduling.policy import TemporalPolicy
from investment_manager.scheduling.repository import SqlTriggerRepository
from investment_manager.settings import AppConfig

logger = logging.getLogger(__name__)

WORLD_MODEL_REVIEW_MARKER = "world_model_review:"


@dataclass(frozen=True, slots=True)
class WorldModelReviewScheduler:
    """Keep one durable review wakeup for the latest portfolio WorldModel."""

    assessments: SqlContextAssessmentStore
    triggers: SqlTriggerRepository
    symbol: str
    pipeline_id: str
    manifest_id: str
    minimum_call_interval_seconds: int
    trigger_expiry_seconds: int
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def reconcile_latest(self, analysis_scope: str) -> bool:
        """Restore the latest model's wakeup after a release cutover or restart."""

        latest = self.assessments.latest_before(
            analysis_scope=analysis_scope,
            as_of=require_utc(self.clock()),
        )
        if latest is None:
            return True
        try:
            self.schedule(latest)
        except KeyError:
            # Trigger service owns plan creation and may start just after this
            # worker. New assessments remain valid and schedule their own review
            # once the current plan exists.
            logger.warning(
                "world model review wakeup deferred until trigger plan is ready",
                extra={"pipeline_id": self.pipeline_id, "symbol": self.symbol},
            )
            return False
        return True

    def publish_update(self, assessment: ContextAssessment) -> None:
        """Publish one durable downstream trigger after the WorldModel is persisted."""

        plan = self.triggers.plan_for_scope(
            symbol=self.symbol,
            pipeline_id=self.pipeline_id,
        )
        trigger = build_trigger_event(
            trigger_type=AnalysisTriggerType.WORLD_MODEL_UPDATED,
            symbol=self.symbol,
            pipeline_id=self.pipeline_id,
            occurred_at=assessment.available_at,
            observed_at=assessment.available_at,
            priority=100,
            dedup_key=stable_id("world_model_updated", assessment.assessment_id),
            evidence_ids=(assessment.assessment_id,),
            plan_revision=plan.revision,
        )
        if self.triggers.trigger(trigger.trigger_id) is not None:
            return
        try:
            self.triggers.record_trigger(trigger)
        except ValueError:
            if self.triggers.trigger(trigger.trigger_id) is None:
                raise

    def schedule(self, assessment: ContextAssessment) -> None:
        now = max(require_utc(self.clock()), assessment.available_at)
        latest = self.assessments.latest_before(
            analysis_scope=assessment.analysis_scope,
            as_of=now,
        )
        if latest is None or latest.assessment_id != assessment.assessment_id:
            return
        review_at = min(item.next_review_at for item in assessment.mechanisms)
        marker = f"{WORLD_MODEL_REVIEW_MARKER}{assessment.assessment_id}"
        reason = f"世界模型机制到期复核：{assessment.assessment_id}"
        for _attempt in range(3):
            plan = self.triggers.plan_for_scope(
                symbol=self.symbol,
                pipeline_id=self.pipeline_id,
            )
            obsolete = tuple(
                item
                for item in plan.scheduled_wakeups
                if item.hypothesis.startswith(WORLD_MODEL_REVIEW_MARKER)
                and item.hypothesis != marker
            )
            operations = [DeleteWakeup(wakeup_id=item.wakeup_id) for item in obsolete]
            if review_at > now:
                desired = ScheduledWakeup(
                    wakeup_id=stable_id(
                        "world_model_review_wakeup",
                        assessment.assessment_id,
                        review_at,
                    ),
                    wake_at=review_at,
                    expires_at=review_at + timedelta(seconds=self.trigger_expiry_seconds),
                    reason=reason,
                    hypothesis=marker,
                )
                current = next(
                    (
                        item
                        for item in plan.scheduled_wakeups
                        if item.wakeup_id == desired.wakeup_id
                    ),
                    None,
                )
                if current is not None and current != desired:
                    raise ValueError("世界模型复核唤醒身份绑定了不同内容")
                conflicts = any(
                    item.wakeup_id != desired.wakeup_id
                    and not item.hypothesis.startswith(WORLD_MODEL_REVIEW_MARKER)
                    and abs((item.wake_at - review_at).total_seconds())
                    < self.minimum_call_interval_seconds
                    for item in plan.scheduled_wakeups
                )
                if current is None and not conflicts:
                    operations.append(AddWakeup(wakeup=desired))
            active_plan = plan
            if operations:
                try:
                    result = self.triggers.apply_patch(
                        build_trigger_plan_patch(
                            plan=plan,
                            submitted_at=now,
                            operations=tuple(operations),
                        ),
                        now=now,
                        current_manifest_id=self.manifest_id,
                    )
                    active_plan = result.plan
                except ValueError as exc:
                    if "revision" in str(exc) or "并发" in str(exc):
                        continue
                    raise
            if review_at <= now:
                self.triggers.record_trigger(
                    build_trigger_event(
                        trigger_type=AnalysisTriggerType.AGENT_WAKEUP,
                        symbol=self.symbol,
                        pipeline_id=self.pipeline_id,
                        occurred_at=review_at,
                        observed_at=now,
                        priority=90,
                        dedup_key=stable_id(
                            "world_model_review_due",
                            assessment.assessment_id,
                            review_at,
                        ),
                        review_reason=reason,
                        expires_at=now + timedelta(seconds=self.trigger_expiry_seconds),
                        plan_revision=active_plan.revision,
                    )
                )
            return
        raise ValueError("世界模型复核计划并发更新失败")


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
class PosteriorActivities:
    application: ContextPosteriorApplication

    @activity.defn(name=POSTERIOR_ACTIVITY_NAME)
    def execute_context_posterior(self, raw_request: dict[str, Any]) -> dict[str, Any]:
        try:
            request = PosteriorWorkflowRequest.model_validate(raw_request)
        except ValidationError as exc:
            raise ApplicationError(
                "ContextPosterior Workflow 输入未通过契约校验",
                type="InvalidWorkflowInput",
                non_retryable=True,
            ) from exc
        try:
            execution = self.application.execute(
                request.frozen_input,
                expected_behavior_hash=request.producer_behavior_id,
            )
        except ValueError as exc:
            raise ApplicationError(
                "ContextPosterior 冻结输入与权威事实冲突",
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
                raise ValueError("相同 ContextAssessment workflow_id 的冻结输入不同") from None
            raw_result = await handle.result()
        return AssessmentWorkflowExecution.model_validate(raw_result)


class AssessmentTemporalWorker(SingleActivityWorker):
    def __init__(
        self,
        client: Client,
        policy: TemporalPolicy,
        application: AssessmentApplication,
        *,
        posterior_application: ContextPosteriorApplication | None = None,
        worker_threads: int,
    ) -> None:
        activities = [AssessmentActivities(application).execute_context_assessment]
        workflows = [ContextAssessmentWorkflow]
        if posterior_application is not None:
            workflows.append(ContextPosteriorWorkflow)
            activities.append(PosteriorActivities(posterior_application).execute_context_posterior)
        super().__init__(
            client,
            task_queue=policy.assessment_task_queue,
            workflows=workflows,
            activities=activities,
            thread_name_prefix="assessment-activity",
            max_concurrent_activities=worker_threads,
        )


@dataclass(frozen=True, slots=True)
class AssessmentServiceAssembly:
    """Purely constructed worker graph; activation may update durable wakeups."""

    application: AssessmentApplication
    posterior_application: ContextPosteriorApplication | None
    scheduler: WorldModelReviewScheduler
    analysis_scope: str

    def activate(self) -> None:
        self.scheduler.reconcile_latest(self.analysis_scope)


def assemble_assessment_service(
    config: AppConfig,
    database_url: str,
    *,
    manifest: ReleaseManifest,
    repository_root: Path,
) -> AssessmentServiceAssembly:
    engine = build_engine(database_url)
    require_current_schema(engine)
    leases = SqlAccountLeaseStore(engine)
    audit = SqlCodexAuditStore(engine)
    analyst = assemble_codex_context_analyst(
        config,
        bundle_root=config.codex_runtime.bundle_root,
        code_version=manifest.code_version,
        leases=leases,
        audit=audit,
    )
    config.codex_runtime.bundle_root.mkdir(parents=True, exist_ok=True)
    assessments = SqlContextAssessmentStore(engine)
    review_symbol = config.assessment.review_trigger_symbol
    if review_symbol is None:
        raise ValueError("ContextAssessment 缺少复核协调 symbol")
    scheduler = WorldModelReviewScheduler(
        assessments=assessments,
        triggers=SqlTriggerRepository(engine, config.trigger),
        symbol=review_symbol,
        pipeline_id=config.pipeline.version,
        manifest_id=manifest.manifest_id,
        minimum_call_interval_seconds=config.trigger.minimum_call_interval_seconds,
        trigger_expiry_seconds=config.trigger.trigger_expiry_seconds,
    )

    def complete_world_model(assessment: ContextAssessment) -> None:
        scheduler.schedule(assessment)
        scheduler.publish_update(assessment)

    posterior_application = None
    prior_policy = config.outcome_evaluation.forecast_prior
    if prior_policy.enabled:
        if prior_policy.artifact_id is None:
            raise ValueError("启用 Forecast prior 时缺少 Release 制品 ID")
        artifact = load_forecast_baseline(
            resolve_manifest_artifact(
                manifest,
                prior_policy.artifact_id,
                repository_root=repository_root,
            )
        )
        posterior_contracts = tuple(
            sorted(
                (item.contract for item in build_prior_targets(artifact)),
                key=lambda item: item.contract_id,
            )
        )
        posterior_application = ContextPosteriorApplication(
            analyst=assemble_codex_context_posterior_analyst(
                config,
                bundle_root=config.codex_runtime.bundle_root,
                contracts=posterior_contracts,
                code_version=manifest.code_version,
                leases=leases,
                audit=audit,
            ),
            contracts=SqlForecastContractStore(engine),
            forecasts=SqlForecastStore(engine),
            market=SqlMarketDataStore(engine),
            maximum_quote_age_seconds=config.capital.risk.maximum_quote_age_seconds,
        )
    return AssessmentServiceAssembly(
        application=AssessmentApplication(
            ContextAssessmentExecutor(
                assessments,
                analyst,
                on_success=complete_world_model,
            )
        ),
        posterior_application=posterior_application,
        scheduler=scheduler,
        analysis_scope=config.assessment.mandate.analysis_scope,
    )


async def run_assessment_worker_forever(
    *,
    config: AppConfig,
    application: AssessmentApplication,
    posterior_application: ContextPosteriorApplication | None = None,
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
        posterior_application=posterior_application,
        worker_threads=worker_threads,
    ):
        await asyncio.Event().wait()


def run_assessment_worker_process(
    *,
    config: AppConfig,
    application: AssessmentApplication,
    posterior_application: ContextPosteriorApplication | None = None,
) -> None:
    asyncio.run(
        run_assessment_worker_forever(
            config=config,
            application=application,
            posterior_application=posterior_application,
        )
    )
