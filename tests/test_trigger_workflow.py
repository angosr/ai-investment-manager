from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Lock

import pytest
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from investment_manager.decision_cycle.trigger import (
    AnalysisCallDeferred,
    TriggerCoordinatorActivities,
)
from investment_manager.kernel.errors import PointInTimeInputUnavailable
from investment_manager.kernel.identity import stable_id
from investment_manager.scheduling.models import (
    AnalysisEventRule,
    AnalysisTriggerType,
    TriggerOutboxKind,
    build_initial_trigger_plan,
    build_trigger_batch,
    build_trigger_event,
    rebind_trigger_plan_manifest,
)
from investment_manager.scheduling.runtime import build_trigger_coordinator_input
from investment_manager.scheduling.workflows import (
    BUILD_TRIGGER_DISPATCHES_ACTIVITY,
    TRIGGER_SIGNAL,
    TriggerCoordinatorWorkflow,
    coordinator_workflow_id,
)
from investment_manager.state.decision.application import DecisionPacketPreparationError

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


@workflow.defn(name="AnalysisCycleWorkflow")
class AnalysisCycleWorkflow:
    """Test-only child: TriggerCoordinator owns dispatch, not child business semantics."""

    @workflow.run
    async def run(self, request):
        workflow_id = str(request.get("workflow_id") or workflow.info().workflow_id)
        return {
            "workflow_id": workflow_id,
            "status": "COMPLETED",
            "reason_code": "TEST_NO_ACTION",
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
                assert status["unresolved_failure"] is False
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


def test_forecast_slot_wakes_on_contract_boundary_without_heartbeat(app_config) -> None:
    async def scenario() -> None:
        batches: list[dict] = []

        @activity.defn(name=BUILD_TRIGGER_DISPATCHES_ACTIVITY)
        async def capture_forecast_slot(raw_batch):
            batches.append(raw_batch)
            return {"workflow_dispatches": []}

        async with await WorkflowEnvironment.start_time_skipping() as env:
            test_now = await env.get_current_time()
            context = app_config.capital.context_forecast
            assert context is not None
            capital = app_config.capital.model_copy(
                update={
                    "enabled": True,
                    "context_forecast": context.model_copy(
                        update={"enabled": True, "cadence_minutes": 1}
                    ),
                }
            )
            trigger_queue = "forecast-slot-boundary-test"
            temporal = app_config.temporal.model_copy(
                update={"trigger_task_queue": trigger_queue}
            )
            config = app_config.model_copy(
                update={"capital": capital, "temporal": temporal}
            )
            symbol = config.assessment.review_trigger_symbol
            plan = build_initial_trigger_plan(
                symbol=symbol,
                pipeline_id=config.pipeline.version,
                manifest_id="manifest-v1",
                updated_at=test_now,
                heartbeat_seconds=None,
            )
            next_slot = datetime.fromtimestamp(
                (int(test_now.timestamp()) // 60 + 1) * 60,
                tz=UTC,
            )
            coordinator_input = build_trigger_coordinator_input(
                plan,
                config,
                context_forecast_activation_at=test_now,
            )
            assert coordinator_input["settings"]["context_forecast_cadence_seconds"] == 60

            async with Worker(
                env.client,
                task_queue=trigger_queue,
                workflows=[TriggerCoordinatorWorkflow],
                activities=[capture_forecast_slot],
            ):
                handle = await env.client.start_workflow(
                    TriggerCoordinatorWorkflow.run,
                    coordinator_input,
                    id=coordinator_workflow_id(plan.symbol, plan.pipeline_id),
                    task_queue=trigger_queue,
                )
                await env.sleep(next_slot - test_now + timedelta(seconds=1))
                for _ in range(100):
                    if batches:
                        break
                    await asyncio.sleep(0.01)
                assert len(batches) == 1
                trigger = batches[0]["triggers"][0]
                assert trigger["trigger_type"] == AnalysisTriggerType.FORECAST_SLOT_DUE.value
                assert datetime.fromisoformat(trigger["occurred_at"]) == next_slot
                await handle.signal(TriggerCoordinatorWorkflow.stop)
                await handle.result()

    asyncio.run(scenario())


def test_trigger_created_before_first_workflow_task_is_not_lost(app_config) -> None:
    async def scenario() -> None:
        @activity.defn(name=BUILD_TRIGGER_DISPATCHES_ACTIVITY)
        async def build_no_dispatch(_raw_batch):
            return {"workflow_dispatches": []}

        async with await WorkflowEnvironment.start_time_skipping() as env:
            trigger_queue = "trigger-prestart-signal-test"
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
            )
            event = build_trigger_event(
                trigger_type=AnalysisTriggerType.AGENT_WAKEUP,
                symbol=plan.symbol,
                pipeline_id=plan.pipeline_id,
                occurred_at=NOW,
                observed_at=NOW,
                priority=100,
                dedup_key="prestart-immediate-review",
                review_reason="立即复核",
            )
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
            async with Worker(
                env.client,
                task_queue=trigger_queue,
                workflows=[TriggerCoordinatorWorkflow],
                activities=[build_no_dispatch],
            ):
                for _ in range(100):
                    status = await handle.query(TriggerCoordinatorWorkflow.status)
                    if status["completed_batches"] == 1:
                        break
                    await asyncio.sleep(0.01)
                status = await handle.query(TriggerCoordinatorWorkflow.status)
                assert status["completed_batches"] == 1, status
                assert status["pending_count"] == 0
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


def test_trigger_activity_marks_post_projection_failure_for_frozen_retry() -> None:
    class FailingBuilder:
        def build(self, _batch):
            raise DecisionPacketPreparationError("packet assembly failed")

    activity_handler = TriggerCoordinatorActivities(builder=FailingBuilder())
    plan = build_initial_trigger_plan(
        symbol="BTCUSDT",
        pipeline_id="pipeline-v1",
        manifest_id="manifest-v1",
        updated_at=NOW,
        heartbeat_seconds=None,
    )
    event = build_trigger_event(
        trigger_type=AnalysisTriggerType.AGENT_WAKEUP,
        review_reason="工作流立即复核",
        symbol=plan.symbol,
        pipeline_id=plan.pipeline_id,
        occurred_at=NOW,
        observed_at=NOW,
        priority=100,
        dedup_key="packet-failure",
    )
    batch = build_trigger_batch(
        plan=plan,
        triggers=(event,),
        created_at=NOW,
        deadline=NOW + timedelta(minutes=5),
    )

    assert activity_handler.build_analysis_dispatches(batch.model_dump(mode="json")) == {
        "retry_frozen_batch": True
    }

    class DeferredBuilder:
        def build(self, _batch):
            raise AnalysisCallDeferred(NOW + timedelta(seconds=10))

    deferred = TriggerCoordinatorActivities(builder=DeferredBuilder())
    assert deferred.build_analysis_dispatches(batch.model_dump(mode="json")) == {
        "deferred_until": (NOW + timedelta(seconds=10)).isoformat(),
        "retry_frozen_batch": True,
    }

    class MissingInputBuilder:
        def build(self, _batch):
            raise PointInTimeInputUnavailable("quote pending")

    missing_input = TriggerCoordinatorActivities(builder=MissingInputBuilder())
    assert missing_input.build_analysis_dispatches(batch.model_dump(mode="json")) == {
        "retry_frozen_batch": True,
    }

    class BrokenInvariantBuilder:
        def build(self, _batch):
            raise ValueError("broken invariant")

    broken = TriggerCoordinatorActivities(builder=BrokenInvariantBuilder())
    with pytest.raises(ApplicationError) as raised:
        broken.build_analysis_dispatches(batch.model_dump(mode="json"))
    assert raised.value.type == "PermanentDomainError"
    assert raised.value.non_retryable is True


def test_trigger_activity_serializes_single_portfolio_state_projection() -> None:
    class TrackingBuilder:
        def __init__(self) -> None:
            self.active = 0
            self.maximum_active = 0
            self.lock = Lock()

        def build(self, _batch):
            with self.lock:
                self.active += 1
                self.maximum_active = max(self.maximum_active, self.active)
            time.sleep(0.02)
            with self.lock:
                self.active -= 1
            return ()

    plan = build_initial_trigger_plan(
        symbol="BTCUSDT",
        pipeline_id="pipeline-v1",
        manifest_id="manifest-v1",
        updated_at=NOW,
        heartbeat_seconds=None,
    )
    event = build_trigger_event(
        trigger_type=AnalysisTriggerType.AGENT_WAKEUP,
        review_reason="并发组合复核",
        symbol=plan.symbol,
        pipeline_id=plan.pipeline_id,
        occurred_at=NOW,
        observed_at=NOW,
        priority=100,
        dedup_key="concurrent-build",
    )
    batch = build_trigger_batch(
        plan=plan,
        triggers=(event,),
        created_at=NOW,
        deadline=NOW + timedelta(minutes=5),
    ).model_dump(mode="json")
    builder = TrackingBuilder()
    activities = TriggerCoordinatorActivities(builder=builder)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(activities.build_analysis_dispatches, (batch, batch))
        )

    assert results == ({"workflow_dispatches": []},) * 2
    assert builder.maximum_active == 1


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

    coordinator.deliver(
        {
            "kind": TriggerOutboxKind.TRIGGER_CREATED.value,
            "trigger": event.model_dump(mode="json"),
        }
    )

    assert tuple(coordinator._prestart_triggers) == (event.trigger_id,)
    assert not coordinator._pending


def test_trigger_coordinator_keeps_event_when_input_is_temporarily_unavailable(
    app_config,
) -> None:
    async def scenario() -> None:
        attempts = 0
        batch_ids: list[str] = []

        @activity.defn(name=BUILD_TRIGGER_DISPATCHES_ACTIVITY)
        async def fail_once(raw_batch):
            nonlocal attempts
            attempts += 1
            batch_ids.append(raw_batch["batch_id"])
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
                    if attempts == 1:
                        break
                    await asyncio.sleep(0.01)
                await env.sleep(timedelta(seconds=31))
                for _ in range(100):
                    status = await handle.query(TriggerCoordinatorWorkflow.status)
                    if status["completed_batches"] == 1:
                        break
                    await asyncio.sleep(0.01)
                status = await handle.query(TriggerCoordinatorWorkflow.status)
                assert attempts == 2
                assert len(set(batch_ids)) == 2
                assert status["completed_batches"] == 1
                assert status["pending_count"] == 0
                await handle.signal(TriggerCoordinatorWorkflow.stop)
                await handle.result()

    asyncio.run(scenario())


def test_release_rebind_updates_plan_without_resetting_coordinator_timeline() -> None:
    initial = build_initial_trigger_plan(
        symbol="BTCUSDT",
        pipeline_id="pipeline-v1",
        manifest_id="manifest-v1",
        updated_at=NOW,
        heartbeat_seconds=900,
    )
    rebound = rebind_trigger_plan_manifest(
        initial,
        manifest_id="manifest-v2",
        updated_at=NOW + timedelta(minutes=5),
    )
    coordinator = TriggerCoordinatorWorkflow()
    coordinator._plan = initial.model_dump(mode="json")
    coordinator._last_analysis_at = NOW + timedelta(minutes=3)
    coordinator._completed_batches = 7

    coordinator.deliver(
        {
            "kind": TriggerOutboxKind.PLAN_REVISED.value,
            "plan": rebound.model_dump(mode="json"),
        }
    )

    assert coordinator._plan == rebound.model_dump(mode="json")
    assert coordinator._last_analysis_at == NOW + timedelta(minutes=3)
    assert coordinator._completed_batches == 7


def test_trigger_coordinator_terminally_records_permanent_builder_failure(
    app_config,
) -> None:
    async def scenario() -> None:
        attempts = 0

        @activity.defn(name=BUILD_TRIGGER_DISPATCHES_ACTIVITY)
        async def fail_permanently(_raw_batch):
            nonlocal attempts
            attempts += 1
            raise ApplicationError(
                "broken invariant",
                type="PermanentDomainError",
                non_retryable=True,
            )

        async with await WorkflowEnvironment.start_time_skipping() as env:
            trigger_queue = "trigger-permanent-failure-test"
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
                dedup_key="permanent-builder-failure",
            )
            async with Worker(
                env.client,
                task_queue=trigger_queue,
                workflows=[TriggerCoordinatorWorkflow],
                activities=[fail_permanently],
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
                    if status["failed_batches"] == 1:
                        break
                    await asyncio.sleep(0.01)
                status = await handle.query(TriggerCoordinatorWorkflow.status)
                assert attempts == 1
                assert status["failed_batches"] == 1
                assert status["unresolved_failure"] is True
                assert status["completed_batches"] == 0
                assert status["pending_count"] == 0
                await handle.signal(TriggerCoordinatorWorkflow.stop)
                await handle.result()

    asyncio.run(scenario())


def test_trigger_coordinator_retries_post_projection_failure_with_frozen_batch(
    app_config,
) -> None:
    async def scenario() -> None:
        attempts = 0
        batches: list[dict] = []

        @activity.defn(name=BUILD_TRIGGER_DISPATCHES_ACTIVITY)
        async def fail_packet_once(raw_batch):
            nonlocal attempts
            attempts += 1
            batches.append(raw_batch)
            if attempts == 1:
                return {"retry_frozen_batch": True}
            return await build_request(raw_batch)

        async with await WorkflowEnvironment.start_time_skipping() as env:
            trigger_queue = "trigger-frozen-retry-test"
            analysis_queue = "trigger-analysis-test"
            temporal = app_config.temporal.model_copy(
                update={
                    "trigger_task_queue": trigger_queue,
                    "task_queue": analysis_queue,
                    "retry_initial_seconds": 1,
                    "retry_maximum_seconds": 1,
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
                symbol=plan.symbol,
                pipeline_id=plan.pipeline_id,
                occurred_at=NOW,
                observed_at=NOW,
                priority=100,
                dedup_key="frozen-packet-retry",
            )
            async with (
                Worker(
                    env.client,
                    task_queue=trigger_queue,
                    workflows=[TriggerCoordinatorWorkflow],
                    activities=[fail_packet_once],
                ),
                Worker(
                    env.client,
                    task_queue=analysis_queue,
                    workflows=[AnalysisCycleWorkflow],
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
                assert batches[0] == batches[1]
                assert status["completed_batches"] == 1
                assert status["failed_batches"] == 0
                assert status["pending_count"] == 0
                assert status["active_batch_id"] is None
                await handle.signal(TriggerCoordinatorWorkflow.stop)
                await handle.result()

    asyncio.run(scenario())


def test_trigger_coordinator_fails_frozen_batch_at_analysis_deadline(app_config) -> None:
    async def scenario() -> None:
        attempts = 0

        @activity.defn(name=BUILD_TRIGGER_DISPATCHES_ACTIVITY)
        async def keep_failing(_raw_batch):
            nonlocal attempts
            attempts += 1
            return {"retry_frozen_batch": True}

        async with await WorkflowEnvironment.start_time_skipping() as env:
            trigger_queue = "trigger-frozen-deadline-test"
            temporal = app_config.temporal.model_copy(
                update={
                    "trigger_task_queue": trigger_queue,
                    "retry_initial_seconds": 1,
                    "retry_maximum_seconds": 10,
                }
            )
            shadow = app_config.shadow.model_copy(
                update={"analysis_deadline_seconds": 30}
            )
            config = app_config.model_copy(
                update={"temporal": temporal, "shadow": shadow}
            )
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
                dedup_key="frozen-deadline",
            )
            async with Worker(
                env.client,
                task_queue=trigger_queue,
                workflows=[TriggerCoordinatorWorkflow],
                activities=[keep_failing],
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
                await env.sleep(timedelta(seconds=31))
                for _ in range(100):
                    status = await handle.query(TriggerCoordinatorWorkflow.status)
                    if status["failed_batches"] == 1:
                        break
                    await asyncio.sleep(0.01)
                status = await handle.query(TriggerCoordinatorWorkflow.status)
                assert attempts >= 1
                assert status["failed_batches"] == 1
                assert status["completed_batches"] == 0
                assert status["pending_count"] == 0
                assert status["active_batch_id"] is None
                assert status["last_batch_id"] is not None
                await handle.signal(TriggerCoordinatorWorkflow.stop)
                await handle.result()

    asyncio.run(scenario())


def test_trigger_coordinator_keeps_event_until_global_admission_retry(
    app_config,
) -> None:
    async def scenario() -> None:
        attempts = 0
        batches: list[dict] = []

        @activity.defn(name=BUILD_TRIGGER_DISPATCHES_ACTIVITY)
        async def defer_once(raw_batch):
            nonlocal attempts
            attempts += 1
            batches.append(raw_batch)
            if attempts == 1:
                created_at = datetime.fromisoformat(raw_batch["created_at"])
                return {
                    "deferred_until": (created_at + timedelta(seconds=10)).isoformat(),
                    "retry_frozen_batch": True,
                }
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
                    if attempts == 1:
                        break
                    await asyncio.sleep(0.01)
                await env.sleep(timedelta(seconds=11))
                for _ in range(100):
                    status = await handle.query(TriggerCoordinatorWorkflow.status)
                    if status["completed_batches"] == 1:
                        break
                    await asyncio.sleep(0.01)
                status = await handle.query(TriggerCoordinatorWorkflow.status)
                assert attempts == 2
                assert batches[0] == batches[1]
                assert status["completed_batches"] == 1
                assert status["pending_count"] == 0
                await handle.signal(TriggerCoordinatorWorkflow.stop)
                await handle.result()

    asyncio.run(scenario())


def test_frozen_admission_batch_survives_trigger_expiry_until_deadline(
    app_config,
) -> None:
    async def scenario() -> None:
        attempts = 0
        batches: list[dict] = []

        async with await WorkflowEnvironment.start_time_skipping() as env:
            test_now = await env.get_current_time()

            @activity.defn(name=BUILD_TRIGGER_DISPATCHES_ACTIVITY)
            async def defer_beyond_expiry(raw_batch):
                nonlocal attempts
                attempts += 1
                batches.append(raw_batch)
                if attempts == 1:
                    return {
                        "deferred_until": (
                            test_now + timedelta(seconds=31)
                        ).isoformat(),
                        "retry_frozen_batch": True,
                    }
                return {"workflow_dispatches": []}

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
                await env.sleep(timedelta(seconds=32))
                for _ in range(100):
                    status = await handle.query(TriggerCoordinatorWorkflow.status)
                    if status["completed_batches"] == 1:
                        break
                    await asyncio.sleep(0.01)
                status = await handle.query(TriggerCoordinatorWorkflow.status)
                assert attempts == 2
                assert batches[0] == batches[1]
                assert status["pending_count"] == 0
                assert status["completed_batches"] == 1
                assert status["failed_batches"] == 0
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
