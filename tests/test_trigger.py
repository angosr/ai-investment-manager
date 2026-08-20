from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from temporalio.client import WorkflowExecutionStatus

from investment_manager import cli
from investment_manager.scheduling.models import (
    AddWakeup,
    AnalysisEventRule,
    AnalysisTriggerType,
    DeleteEventRule,
    DeleteWakeup,
    ScheduledWakeup,
    SetAiPaused,
    SetHeartbeat,
    TriggerNow,
    TriggerOutboxKind,
    TriggerPlanGate,
    UpdateWakeup,
    UpsertEventRule,
    build_initial_trigger_plan,
    build_trigger_event,
    build_trigger_plan_patch,
    carry_forward_trigger_plan,
    decide_analysis_call_admission,
    trigger_plan_accepts,
    trigger_reconsideration,
    trigger_rule_value,
)
from investment_manager.scheduling.repository import TriggerOutboxMessage
from investment_manager.scheduling.runtime import (
    TemporalTriggerDispatcher,
    terminate_superseded_trigger_coordinators,
)
from investment_manager.scheduling.workflows import coordinator_workflow_id


def test_trigger_service_acquires_leadership_before_durable_release_setup(
    monkeypatch, app_config
) -> None:
    events: list[str] = []

    class RejectingLeadership:
        def __init__(self, _engine, _lock_key):
            pass

        def __enter__(self):
            events.append("leadership")
            raise RuntimeError("已有 Trigger Dispatcher 持有领导锁")

        def __exit__(self, exc_type, exc, traceback):
            return None

    monkeypatch.setattr(
        cli,
        "_load_runtime_release",
        lambda _config, _manifest: (app_config, SimpleNamespace(manifest_id="release-v2")),
    )
    monkeypatch.setattr(cli, "_runtime_engine", lambda _database_url: object())
    monkeypatch.setattr(cli, "SqlTriggerRepository", lambda _engine, _policy: object())
    monkeypatch.setattr(cli, "PostgresTriggerLeadership", RejectingLeadership)
    monkeypatch.setattr(
        cli,
        "SqlGovernanceRepository",
        lambda _engine: events.append("release-write"),
    )
    monkeypatch.setattr(
        cli,
        "_ensure_trigger_plans",
        lambda *_args, **_kwargs: events.append("plan-write"),
    )

    with pytest.raises(RuntimeError, match="已有 Trigger Dispatcher"):
        cli.trigger_service(Path("config.yaml"), "postgresql://unused", Path("manifest.yaml"))

    assert events == ["leadership"]


def test_shared_trigger_timing_preserves_specific_rules_cooldown_and_expiry(
    app_config, replay_input
) -> None:
    now = replay_input.market.as_of
    plan = build_initial_trigger_plan(
        symbol="BTCUSDT",
        pipeline_id=app_config.pipeline.version,
        manifest_id="manifest-v1",
        updated_at=now,
        heartbeat_seconds=None,
        event_rules=(
            AnalysisEventRule(
                rule_id="news-default",
                trigger_type=AnalysisTriggerType.INTELLIGENCE_INSERTED,
                minimum_priority=80,
                coalesce_seconds=120,
                ordinary_cooldown_seconds=900,
            ),
            AnalysisEventRule(
                rule_id="news-urgent",
                trigger_type=AnalysisTriggerType.INTELLIGENCE_INSERTED,
                minimum_priority=95,
                coalesce_seconds=15,
                ordinary_cooldown_seconds=300,
            ),
        ),
    )
    ordinary = build_trigger_event(
        trigger_type=AnalysisTriggerType.INTELLIGENCE_INSERTED,
        symbol=plan.symbol,
        pipeline_id=plan.pipeline_id,
        occurred_at=now,
        observed_at=now,
        priority=84,
        dedup_key="ordinary",
        expires_at=now + timedelta(minutes=15),
    ).model_dump(mode="json")
    urgent = build_trigger_event(
        trigger_type=AnalysisTriggerType.INTELLIGENCE_INSERTED,
        symbol=plan.symbol,
        pipeline_id=plan.pipeline_id,
        occurred_at=now,
        observed_at=now,
        priority=98,
        dedup_key="urgent",
        expires_at=now + timedelta(minutes=15),
    ).model_dump(mode="json")
    raw_plan = plan.model_dump(mode="json")

    assert trigger_plan_accepts(raw_plan, ordinary)
    assert trigger_rule_value(raw_plan, ordinary, "coalesce_seconds") == 120
    assert trigger_rule_value(raw_plan, urgent, "coalesce_seconds") == 15
    timing = trigger_reconsideration(
        plan=raw_plan,
        pending=(ordinary,),
        now=now,
        last_analysis_at=now - timedelta(minutes=5),
        input_retry_not_before=None,
        wake_at_expiry=True,
    )
    assert timing.reconsider_at == now + timedelta(minutes=10)

    expiring = {**ordinary, "expires_at": (now + timedelta(seconds=30)).isoformat()}
    expiry_wakeup = trigger_reconsideration(
        plan=raw_plan,
        pending=(expiring,),
        now=now,
        last_analysis_at=now,
        input_retry_not_before=None,
        wake_at_expiry=True,
    )
    assert expiry_wakeup.reconsider_at == now + timedelta(seconds=30)


def test_shared_global_admission_only_enforces_minimum_interval(replay_input) -> None:
    now = replay_input.market.as_of

    interval = decide_analysis_call_admission(
        requested_at=now,
        last_admitted_at=now - timedelta(seconds=5),
        minimum_call_interval_seconds=15,
    )
    assert not interval.admitted
    assert interval.retry_at == now + timedelta(seconds=10)

    admitted = decide_analysis_call_admission(
        requested_at=now,
        last_admitted_at=now - timedelta(seconds=15),
        minimum_call_interval_seconds=15,
    )
    assert admitted.admitted_at == now

    no_history = decide_analysis_call_admission(
        requested_at=now,
        last_admitted_at=None,
        minimum_call_interval_seconds=15,
    )
    assert no_history.admitted_at == now


def test_shared_trigger_timing_has_no_hourly_budget(app_config, replay_input) -> None:
    now = replay_input.market.as_of
    plan = build_initial_trigger_plan(
        symbol="BTCUSDT",
        pipeline_id=app_config.pipeline.version,
        manifest_id="manifest-v1",
        updated_at=now,
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
        occurred_at=now,
        observed_at=now,
        priority=100,
        dedup_key="urgent-event",
        expires_at=now + timedelta(hours=2),
    ).model_dump(mode="json")
    timing = trigger_reconsideration(
        plan=plan.model_dump(mode="json"),
        pending=(event,),
        now=now,
        last_analysis_at=now,
        input_retry_not_before=None,
        wake_at_expiry=True,
    )

    assert timing.reconsider_at == now


def test_trigger_plan_patch_has_full_bounded_scheduling_authority(app_config, replay_input) -> None:
    now = replay_input.market.as_of
    plan = build_initial_trigger_plan(
        symbol="BTCUSDT",
        pipeline_id=app_config.pipeline.version,
        manifest_id="manifest-v1",
        updated_at=now,
        heartbeat_seconds=900,
        event_rules=(
            AnalysisEventRule(
                rule_id="news",
                trigger_type=AnalysisTriggerType.INTELLIGENCE_INSERTED,
                minimum_priority=70,
                coalesce_seconds=10,
            ),
        ),
    )
    wakeup = ScheduledWakeup(
        wakeup_id="wakeup-fed",
        wake_at=now + timedelta(hours=1),
        expires_at=now + timedelta(hours=1, minutes=5),
        reason="宏观事件后复核",
        evidence_ids=("calendar-fed",),
    )
    patch = build_trigger_plan_patch(
        plan=plan,
        submitted_at=now,
        evidence_ids=("calendar-fed",),
        operations=(
            AddWakeup(wakeup=wakeup),
            SetHeartbeat(heartbeat_seconds=None),
            TriggerNow(request_id="governor-check-1", reason="立即复核"),
        ),
    )

    result = TriggerPlanGate(app_config.trigger).apply(
        plan,
        patch,
        now=now,
        current_manifest_id="manifest-v1",
    )

    assert result.plan.revision == 2
    assert result.plan.heartbeat_seconds is None
    assert result.plan.scheduled_wakeups == (wakeup,)
    assert result.emitted_triggers[0].trigger_type == AnalysisTriggerType.AGENT_WAKEUP
    assert result.emitted_triggers[0].priority == 100

    with pytest.raises(ValueError, match="revision 已过期"):
        TriggerPlanGate(app_config.trigger).apply(
            result.plan,
            patch,
            now=now,
            current_manifest_id="manifest-v1",
        )


def test_release_cutover_carries_agent_plan_and_drops_expired_wakeups(replay_input) -> None:
    now = replay_input.market.as_of
    live = ScheduledWakeup(
        wakeup_id="live",
        wake_at=now + timedelta(minutes=5),
        expires_at=now + timedelta(minutes=10),
        reason="继续复核",
    )
    expired = ScheduledWakeup(
        wakeup_id="expired",
        wake_at=now - timedelta(minutes=10),
        expires_at=now - timedelta(minutes=5),
        reason="已经过期",
    )
    previous = build_initial_trigger_plan(
        symbol="BTCUSDT",
        pipeline_id="pipeline-v1",
        manifest_id="manifest-v1",
        updated_at=now - timedelta(hours=1),
        heartbeat_seconds=1800,
        event_rules=(
            AnalysisEventRule(
                rule_id="news",
                trigger_type=AnalysisTriggerType.INTELLIGENCE_INSERTED,
                minimum_priority=80,
                coalesce_seconds=15,
                ordinary_cooldown_seconds=120,
            ),
        ),
    ).model_copy(
        update={
            "ai_paused": True,
            "scheduled_wakeups": (expired, live),
        }
    )

    carried = carry_forward_trigger_plan(
        previous,
        pipeline_id="pipeline-v2",
        manifest_id="manifest-v2",
        updated_at=now,
    )

    assert carried.revision == 1
    assert carried.pipeline_id == "pipeline-v2"
    assert carried.manifest_id == "manifest-v2"
    assert carried.ai_paused
    assert carried.heartbeat_seconds == 1800
    assert carried.event_rules == previous.event_rules
    assert carried.scheduled_wakeups == (live,)
    assert carried.applied_patch_id is None


def test_trigger_plan_gate_rejects_past_wakeup(app_config, replay_input) -> None:
    now = replay_input.market.as_of
    plan = build_initial_trigger_plan(
        symbol="BTCUSDT",
        pipeline_id="pipeline-v1",
        manifest_id="manifest-v1",
        updated_at=now,
        heartbeat_seconds=900,
    )
    past = ScheduledWakeup(
        wakeup_id="past",
        wake_at=now - timedelta(minutes=2),
        expires_at=now - timedelta(minutes=1),
        reason="非法过去时间",
    )
    patch = build_trigger_plan_patch(
        plan=plan,
        submitted_at=now,
        operations=(AddWakeup(wakeup=past),),
    )

    with pytest.raises(ValueError, match="必须位于未来"):
        TriggerPlanGate(app_config.trigger).apply(
            plan,
            patch,
            now=now,
            current_manifest_id="manifest-v1",
        )


def test_trigger_plan_rejects_ambiguous_enabled_rule_tiers(replay_input) -> None:
    now = replay_input.market.as_of

    with pytest.raises(ValueError, match="minimum_priority 不得重复"):
        build_initial_trigger_plan(
            symbol="BTCUSDT",
            pipeline_id="pipeline-v1",
            manifest_id="manifest-v1",
            updated_at=now,
            heartbeat_seconds=900,
            event_rules=(
                AnalysisEventRule(
                    rule_id="news-default",
                    trigger_type=AnalysisTriggerType.INTELLIGENCE_INSERTED,
                    minimum_priority=80,
                ),
                AnalysisEventRule(
                    rule_id="news-duplicate",
                    trigger_type=AnalysisTriggerType.INTELLIGENCE_INSERTED,
                    minimum_priority=80,
                ),
            ),
        )


def test_trigger_plan_can_update_delete_and_pause_without_hidden_defaults(
    app_config, replay_input
) -> None:
    now = replay_input.market.as_of
    rule = AnalysisEventRule(
        rule_id="news",
        trigger_type=AnalysisTriggerType.INTELLIGENCE_INSERTED,
        minimum_priority=80,
    )
    wakeup = ScheduledWakeup(
        wakeup_id="wakeup-1",
        wake_at=now + timedelta(hours=2),
        expires_at=now + timedelta(hours=2, minutes=5),
        reason="初始时间",
    )
    plan = build_initial_trigger_plan(
        symbol="BTCUSDT",
        pipeline_id="pipeline-v1",
        manifest_id="manifest-v1",
        updated_at=now,
        heartbeat_seconds=900,
        event_rules=(rule,),
    ).model_copy(update={"scheduled_wakeups": (wakeup,)})
    updated = wakeup.model_copy(
        update={
            "wake_at": now + timedelta(hours=3),
            "expires_at": now + timedelta(hours=3, minutes=5),
        }
    )
    temporary_rule = AnalysisEventRule(
        rule_id="market",
        trigger_type=AnalysisTriggerType.MARKET_SHOCK,
    )
    patch = build_trigger_plan_patch(
        plan=plan,
        submitted_at=now,
        operations=(
            UpdateWakeup(wakeup=updated),
            DeleteWakeup(wakeup_id="wakeup-1"),
            UpsertEventRule(rule=temporary_rule),
            DeleteEventRule(rule_id="news"),
            SetAiPaused(paused=True),
            SetHeartbeat(heartbeat_seconds=None),
        ),
    )

    result = TriggerPlanGate(app_config.trigger).apply(
        plan,
        patch,
        now=now,
        current_manifest_id="manifest-v1",
    )

    assert result.plan.ai_paused
    assert result.plan.heartbeat_seconds is None
    assert result.plan.scheduled_wakeups == ()
    assert result.plan.event_rules == (temporary_rule,)


def test_release_cutover_terminates_only_superseded_pipeline(app_config, replay_input) -> None:
    active = build_initial_trigger_plan(
        symbol="BTCUSDT",
        pipeline_id=app_config.pipeline.version,
        manifest_id="manifest-active",
        updated_at=replay_input.market.as_of,
        heartbeat_seconds=None,
    )
    superseded = build_initial_trigger_plan(
        symbol="BTCUSDT",
        pipeline_id="pipeline-old",
        manifest_id="manifest-old",
        updated_at=replay_input.market.as_of,
        heartbeat_seconds=None,
    )

    class FakeHandle:
        def __init__(self) -> None:
            self.reason = None

        async def describe(self):
            return SimpleNamespace(status=WorkflowExecutionStatus.RUNNING)

        async def terminate(self, reason):
            self.reason = reason

    old_handle = FakeHandle()

    class FakeClient:
        def get_workflow_handle(self, workflow_id):
            assert workflow_id == coordinator_workflow_id(superseded.symbol, superseded.pipeline_id)
            return old_handle

    terminated = asyncio.run(
        terminate_superseded_trigger_coordinators(
            client=FakeClient(),
            plans=(active, superseded),
            active_pipeline_id=active.pipeline_id,
        )
    )

    assert terminated == (coordinator_workflow_id("BTCUSDT", "pipeline-old"),)
    assert old_handle.reason == f"superseded by pipeline {active.pipeline_id}"


def test_dispatcher_acknowledges_superseded_outbox_without_reviving_it(
    app_config, replay_input
) -> None:
    class FailIfUsed:
        def __getattr__(self, name):
            raise AssertionError(f"不应访问旧 pipeline 依赖：{name}")

    message = TriggerOutboxMessage(
        outbox_id="outbox-old",
        aggregate_key="BTCUSDT:pipeline-old",
        message_kind=TriggerOutboxKind.PLAN_REVISED,
        created_at=replay_input.market.as_of,
        available_at=replay_input.market.as_of,
        attempt_count=0,
        payload={"kind": TriggerOutboxKind.PLAN_REVISED.value},
    )
    dispatcher = TemporalTriggerDispatcher(
        client=FailIfUsed(),
        config=app_config,
        plans=FailIfUsed(),
    )

    asyncio.run(dispatcher.deliver(message))
