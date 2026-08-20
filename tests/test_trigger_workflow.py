from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from investment_manager.kernel.identity import stable_id
from investment_manager.legacy.workflows import PREPARE_ACTIVITY_NAME, AnalysisCycleWorkflow
from investment_manager.scheduling.models import (
    AnalysisEventRule,
    AnalysisTriggerType,
    TriggerOutboxKind,
    build_initial_trigger_plan,
    build_trigger_event,
)
from investment_manager.scheduling.runtime import build_trigger_coordinator_input
from investment_manager.scheduling.workflows import (
    BUILD_TRIGGER_DISPATCHES_ACTIVITY,
    TRIGGER_SIGNAL,
    TriggerCoordinatorWorkflow,
    coordinator_workflow_id,
)

NOW = datetime(2026, 8, 18, 12, tzinfo=UTC)


@activity.defn(name=BUILD_TRIGGER_DISPATCHES_ACTIVITY)
async def build_request(raw_batch):
    workflow_id = stable_id("analysis_workflow", raw_batch["batch_id"])
    return {
        "workflow_dispatches": [
            {
                "workflow_name": "AnalysisCycleWorkflow",
                "workflow_id": workflow_id,
                "task_queue": "trigger-analysis-test",
                "payload": {
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
                },
            },
        ]
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
                assert status["active_batch_id"] is None
                await handle.signal(TriggerCoordinatorWorkflow.stop)
                result = await handle.result()
                assert result["completed_batches"] == 1

    asyncio.run(scenario())


def test_trigger_coordinator_consumes_valid_batch_with_no_enabled_consumer(
    app_config,
) -> None:
    async def scenario() -> None:
        @activity.defn(name=BUILD_TRIGGER_DISPATCHES_ACTIVITY)
        async def build_no_dispatch(_raw_batch):
            return {"workflow_dispatches": []}

        async with await WorkflowEnvironment.start_time_skipping() as env:
            trigger_queue = "trigger-no-consumer-test"
            temporal = app_config.temporal.model_copy(
                update={"trigger_task_queue": trigger_queue}
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
                symbol=plan.symbol,
                pipeline_id=plan.pipeline_id,
                occurred_at=NOW,
                observed_at=NOW,
                priority=100,
                dedup_key="no-consumer",
            )
            async with Worker(
                env.client,
                task_queue=trigger_queue,
                workflows=[TriggerCoordinatorWorkflow],
                activities=[build_no_dispatch],
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
                assert status["completed_batches"] == 1
                assert status["pending_count"] == 0
                assert status["failed_batches"] == 0
                await handle.signal(TriggerCoordinatorWorkflow.stop)
                await handle.result()

    asyncio.run(scenario())


def test_trigger_coordinator_uses_most_specific_matching_event_rule() -> None:
    coordinator = TriggerCoordinatorWorkflow()
    coordinator._plan = {
        "event_rules": [
            {
                "rule_id": "news-default",
                "trigger_type": AnalysisTriggerType.INTELLIGENCE_INSERTED.value,
                "enabled": True,
                "minimum_priority": 80,
                "coalesce_seconds": 15,
            },
            {
                "rule_id": "news-urgent",
                "trigger_type": AnalysisTriggerType.INTELLIGENCE_INSERTED.value,
                "enabled": True,
                "minimum_priority": 95,
                "coalesce_seconds": 0,
            },
        ]
    }

    ordinary = {
        "trigger_type": AnalysisTriggerType.INTELLIGENCE_INSERTED.value,
        "priority": 90,
    }
    urgent = {**ordinary, "priority": 98}

    assert coordinator._rule_value(ordinary, "coalesce_seconds") == 15
    assert coordinator._rule_value(urgent, "coalesce_seconds") == 0


def test_trigger_signal_can_arrive_before_workflow_run_initializes_settings(
    app_config,
) -> None:
    plan = build_initial_trigger_plan(
        symbol="BTCUSDT",
        pipeline_id=app_config.pipeline.version,
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
        symbol=plan.symbol,
        pipeline_id=plan.pipeline_id,
        occurred_at=NOW,
        observed_at=NOW,
        priority=100,
        dedup_key="signal-before-run",
    )
    coordinator = TriggerCoordinatorWorkflow()
    coordinator._plan = plan.model_dump(mode="json")

    coordinator.deliver(
        {
            "kind": TriggerOutboxKind.TRIGGER_CREATED.value,
            "trigger": event.model_dump(mode="json"),
        }
    )

    assert tuple(coordinator._pending) == (event.trigger_id,)
    coordinator._settings = {"maximum_pending_triggers": 1}
    coordinator._trim_pending()
    assert tuple(coordinator._pending) == (event.trigger_id,)


def test_trigger_coordinator_keeps_event_when_input_is_temporarily_unavailable(
    app_config,
) -> None:
    async def scenario() -> None:
        attempts = 0

        @activity.defn(name=BUILD_TRIGGER_DISPATCHES_ACTIVITY)
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
            analysis_queue = "trigger-analysis-test"
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


def test_trigger_coordinator_keeps_event_until_global_admission_retry(
    app_config,
) -> None:
    async def scenario() -> None:
        attempts = 0

        @activity.defn(name=BUILD_TRIGGER_DISPATCHES_ACTIVITY)
        async def defer_once(raw_batch):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return {"deferred_until": (NOW + timedelta(seconds=10)).isoformat()}
            return await build_request(raw_batch)

        async with await WorkflowEnvironment.start_time_skipping() as env:
            trigger_queue = "trigger-admission-delay-test"
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
                dedup_key="admission-delay-evidence",
            )
            async with (
                Worker(
                    env.client,
                    task_queue=trigger_queue,
                    workflows=[TriggerCoordinatorWorkflow],
                    activities=[defer_once],
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


def test_trigger_coordinator_discards_event_at_expiry_before_admission_retry(
    app_config,
) -> None:
    async def scenario() -> None:
        attempts = 0

        async with await WorkflowEnvironment.start_time_skipping() as env:
            test_now = await env.get_current_time()

            @activity.defn(name=BUILD_TRIGGER_DISPATCHES_ACTIVITY)
            async def defer_beyond_expiry(raw_batch):
                nonlocal attempts
                attempts += 1
                return {
                    "deferred_until": (test_now + timedelta(seconds=31)).isoformat()
                }

            trigger_queue = "trigger-expiry-before-admission-test"
            temporal = app_config.temporal.model_copy(
                update={"trigger_task_queue": trigger_queue}
            )
            config = app_config.model_copy(update={"temporal": temporal})
            plan = build_initial_trigger_plan(
                symbol="BTCUSDT",
                pipeline_id=config.pipeline.version,
                manifest_id="manifest-v1",
                updated_at=test_now,
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
                occurred_at=test_now,
                observed_at=test_now,
                priority=100,
                dedup_key="expires-before-admission",
                expires_at=test_now + timedelta(seconds=10),
            )
            async with Worker(
                env.client,
                task_queue=trigger_queue,
                workflows=[TriggerCoordinatorWorkflow],
                activities=[defer_beyond_expiry],
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
                    if attempts == 1:
                        break
                    await asyncio.sleep(0.01)
                assert attempts == 1
                await env.sleep(timedelta(seconds=20))
                status = await handle.query(TriggerCoordinatorWorkflow.status)
                assert attempts == 1
                assert status["pending_count"] == 0
                assert status["completed_batches"] == 0
                await handle.signal(TriggerCoordinatorWorkflow.stop)
                await handle.result()

    asyncio.run(scenario())


def test_heartbeat_remains_pending_past_generic_trigger_expiry(app_config) -> None:
    async def scenario() -> None:
        attempts = 0
        observed_expiries: list[str | None] = []

        @activity.defn(name=BUILD_TRIGGER_DISPATCHES_ACTIVITY)
        async def defer_past_expiry(raw_batch):
            nonlocal attempts
            attempts += 1
            observed_expiries.append(raw_batch["triggers"][0]["expires_at"])
            if attempts == 1:
                created_at = datetime.fromisoformat(raw_batch["created_at"])
                return {"deferred_until": (created_at + timedelta(seconds=31)).isoformat()}
            return await build_request(raw_batch)

        async with await WorkflowEnvironment.start_time_skipping() as env:
            trigger_queue = "durable-heartbeat-test"
            analysis_queue = "trigger-analysis-test"
            temporal = app_config.temporal.model_copy(
                update={"trigger_task_queue": trigger_queue, "task_queue": analysis_queue}
            )
            trigger = app_config.trigger.model_copy(update={"trigger_expiry_seconds": 30})
            config = app_config.model_copy(
                update={"temporal": temporal, "trigger": trigger}
            )
            plan = build_initial_trigger_plan(
                symbol="BTCUSDT",
                pipeline_id=config.pipeline.version,
                manifest_id="manifest-v1",
                updated_at=NOW,
                heartbeat_seconds=1,
                event_rules=(),
            )
            async with (
                Worker(
                    env.client,
                    task_queue=trigger_queue,
                    workflows=[TriggerCoordinatorWorkflow],
                    activities=[defer_past_expiry],
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
                await env.sleep(timedelta(seconds=40))
                for _ in range(200):
                    status = await handle.query(TriggerCoordinatorWorkflow.status)
                    if status["completed_batches"] == 1:
                        break
                    await asyncio.sleep(0.01)
                status = await handle.query(TriggerCoordinatorWorkflow.status)
                assert attempts >= 2
                assert observed_expiries[:2] == [None, None]
                assert status["completed_batches"] >= 1
                await handle.signal(TriggerCoordinatorWorkflow.stop)
                await handle.result()

    asyncio.run(scenario())
