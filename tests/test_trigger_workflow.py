from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from quant_core.ids import stable_id
from quant_core.temporal_workflows import PREPARE_ACTIVITY_NAME, AnalysisCycleWorkflow
from quant_core.trigger import (
    AnalysisEventRule,
    AnalysisTriggerType,
    TriggerOutboxKind,
    build_initial_trigger_plan,
    build_trigger_event,
)
from quant_core.trigger_runtime import build_trigger_coordinator_input
from quant_core.trigger_workflows import (
    BUILD_TRIGGER_REQUEST_ACTIVITY,
    TRIGGER_SIGNAL,
    TriggerCoordinatorWorkflow,
    coordinator_workflow_id,
)

NOW = datetime(2026, 8, 18, 12, tzinfo=UTC)


@activity.defn(name=BUILD_TRIGGER_REQUEST_ACTIVITY)
async def build_request(raw_batch):
    workflow_id = stable_id("analysis_workflow", raw_batch["batch_id"])
    return {
        "workflow_request": {
            "workflow_id": workflow_id,
            "input_hash": "test-input-hash",
            "deadline": raw_batch["deadline"],
            "orchestration": {
                "activity_start_to_close_seconds": 10,
                "activity_schedule_to_close_seconds": 30,
                "retry_initial_seconds": 1,
                "retry_maximum_seconds": 2,
                "retry_backoff_coefficient": 2,
                "retry_maximum_attempts": 1,
            },
        }
    }


@activity.defn(name=PREPARE_ACTIVITY_NAME)
async def prepare_no_action(_request):
    return {
        "attempt": 1,
        "cycle_result": {"outcome": "NO_ACTION", "reason_code": "TEST_NO_ACTION"},
    }


def test_trigger_coordinator_deduplicates_and_runs_one_event_batch(app_config) -> None:
    async def scenario() -> None:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            trigger_queue = "trigger-coordinator-test"
            analysis_queue = "trigger-analysis-test"
            temporal = app_config.temporal.model_copy(
                update={"trigger_task_queue": trigger_queue, "task_queue": analysis_queue}
            )
            config = app_config.model_copy(update={"temporal": temporal})
            plan = build_initial_trigger_plan(
                symbol="BTCUSDT",
                pipeline_id=config.pipeline.version,
                manifest_id="manifest-v1",
                updated_at=NOW,
                heartbeat_seconds=None,
                event_rules=(
                    AnalysisEventRule(
                        rule_id="news",
                        trigger_type=AnalysisTriggerType.INTELLIGENCE_INSERTED,
                        minimum_priority=0,
                    ),
                ),
            )
            event = build_trigger_event(
                trigger_type=AnalysisTriggerType.INTELLIGENCE_INSERTED,
                symbol="BTCUSDT",
                pipeline_id=config.pipeline.version,
                occurred_at=NOW,
                observed_at=NOW,
                priority=100,
                dedup_key="evidence-1",
                evidence_ids=("evidence-1",),
            )
            workflow_id = coordinator_workflow_id(plan.symbol, plan.pipeline_id)
            async with (
                Worker(
                    env.client,
                    task_queue=trigger_queue,
                    workflows=[TriggerCoordinatorWorkflow],
                    activities=[build_request],
                ),
                Worker(
                    env.client,
                    task_queue=analysis_queue,
                    workflows=[AnalysisCycleWorkflow],
                    activities=[prepare_no_action],
                ),
            ):
                handle = await env.client.start_workflow(
                    TriggerCoordinatorWorkflow.run,
                    build_trigger_coordinator_input(plan, config),
                    id=workflow_id,
                    task_queue=trigger_queue,
                )
                payload = {
                    "kind": TriggerOutboxKind.TRIGGER_CREATED.value,
                    "trigger": event.model_dump(mode="json"),
                }
                await handle.signal(TRIGGER_SIGNAL, payload)
                await handle.signal(TRIGGER_SIGNAL, payload)
                for _ in range(100):
                    status = await handle.query(TriggerCoordinatorWorkflow.status)
                    if status["completed_batches"] == 1:
                        break
                    await asyncio.sleep(0.01)
                status = await handle.query(TriggerCoordinatorWorkflow.status)
                assert status["completed_batches"] == 1, status
                assert status["pending_count"] == 0
                await handle.signal(TriggerCoordinatorWorkflow.stop)
                result = await handle.result()
                assert result["completed_batches"] == 1

    asyncio.run(scenario())


def test_trigger_coordinator_keeps_event_when_input_is_temporarily_unavailable(
    app_config,
) -> None:
    async def scenario() -> None:
        attempts = 0

        @activity.defn(name=BUILD_TRIGGER_REQUEST_ACTIVITY)
        async def fail_once(raw_batch):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ApplicationError(
                    "market bootstrap pending",
                    type="TriggerInputUnavailable",
                )
            return await build_request(raw_batch)

        async with await WorkflowEnvironment.start_time_skipping() as env:
            trigger_queue = "trigger-retry-test"
            analysis_queue = "trigger-retry-analysis-test"
            temporal = app_config.temporal.model_copy(
                update={
                    "trigger_task_queue": trigger_queue,
                    "task_queue": analysis_queue,
                    "retry_initial_seconds": 1,
                    "retry_maximum_seconds": 1,
                    "retry_maximum_attempts": 1,
                }
            )
            config = app_config.model_copy(update={"temporal": temporal})
            plan = build_initial_trigger_plan(
                symbol="BTCUSDT",
                pipeline_id=config.pipeline.version,
                manifest_id="manifest-v1",
                updated_at=NOW,
                heartbeat_seconds=None,
                event_rules=(
                    AnalysisEventRule(
                        rule_id="news",
                        trigger_type=AnalysisTriggerType.INTELLIGENCE_INSERTED,
                    ),
                ),
            )
            event = build_trigger_event(
                trigger_type=AnalysisTriggerType.INTELLIGENCE_INSERTED,
                symbol="BTCUSDT",
                pipeline_id=config.pipeline.version,
                occurred_at=NOW,
                observed_at=NOW,
                priority=100,
                dedup_key="retry-evidence",
            )
            async with (
                Worker(
                    env.client,
                    task_queue=trigger_queue,
                    workflows=[TriggerCoordinatorWorkflow],
                    activities=[fail_once],
                ),
                Worker(
                    env.client,
                    task_queue=analysis_queue,
                    workflows=[AnalysisCycleWorkflow],
                    activities=[prepare_no_action],
                ),
            ):
                handle = await env.client.start_workflow(
                    TriggerCoordinatorWorkflow.run,
                    build_trigger_coordinator_input(plan, config),
                    id=coordinator_workflow_id(plan.symbol, plan.pipeline_id),
                    task_queue=trigger_queue,
                )
                await handle.signal(
                    TRIGGER_SIGNAL,
                    {
                        "kind": TriggerOutboxKind.TRIGGER_CREATED.value,
                        "trigger": event.model_dump(mode="json"),
                    },
                )
                for _ in range(100):
                    status = await handle.query(TriggerCoordinatorWorkflow.status)
                    if status["completed_batches"] == 1:
                        break
                    await asyncio.sleep(0.01)
                status = await handle.query(TriggerCoordinatorWorkflow.status)
                assert attempts == 2
                assert status["completed_batches"] == 1
                assert status["pending_count"] == 0
                await handle.signal(TriggerCoordinatorWorkflow.stop)
                await handle.result()

    asyncio.run(scenario())
