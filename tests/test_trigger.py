from __future__ import annotations

import asyncio
from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace

import pytest

from investment_manager.scheduling.application import (
    ensure_trigger_plans,
    set_trigger_heartbeat,
    trigger_now,
)
from investment_manager.scheduling.fact_triggers import CanonicalFactTriggerPublisher
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
from investment_manager.scheduling.repository import (
    PostgresTriggerLeadership,
    TriggerOutboxMessage,
)
from investment_manager.scheduling.runtime import (
    TemporalTriggerDispatcher,
    terminate_inactive_trigger_coordinators,
)
from investment_manager.scheduling.workflows import coordinator_workflow_id
from investment_manager.state.models import CanonicalFactRevision, FactRevisionStatus

trigger_runtime = import_module("investment_manager.decision_cycle.service")


def test_trigger_leadership_connection_does_not_hold_an_idle_transaction() -> None:
    class Result:
        @staticmethod
        def scalar_one():
            return True

    class Connection:
        def __init__(self) -> None:
            self.isolation_level = None
            self.execute_count = 0
            self.closed = False

        def execution_options(self, *, isolation_level):
            self.isolation_level = isolation_level
            return self

        def execute(self, _statement):
            self.execute_count += 1
            return Result()

        def close(self):
            self.closed = True

    connection = Connection()
    engine = SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql"),
        connect=lambda: connection,
    )
    leadership = PostgresTriggerLeadership(engine, 1234)

    assert leadership.acquire()
    assert connection.isolation_level == "AUTOCOMMIT"
    leadership.release()

    assert connection.execute_count == 2
    assert connection.closed


def test_canonical_fact_trigger_publisher_is_idempotent_and_portfolio_wide(
    app_config,
    replay_input,
) -> None:
    now = replay_input.market.as_of
    fact = CanonicalFactRevision(
        fact_id="fed-release-1",
        revision_id="fed-release-revision-1",
        projection_version=app_config.decision_state.official_fact_policy.version,
        fact_type="FED_MONETARY_RELEASE",
        status=FactRevisionStatus.ACTIVE,
        event_time=now + timedelta(minutes=5),
        observed_at=now,
        headline="Federal Reserve monetary policy release",
        claim="Official statement published.",
        affected_assets=("ETH",),
        risk_factors=("US_MONETARY_POLICY",),
        source_observation_ids=("fed-observation-1",),
        revision_hash="a" * 64,
    )

    class Facts:
        def fact_revisions_observed_since(self, *, observed_since, as_of):
            assert observed_since < as_of
            return (fact,)

        def facts_as_of(self, *, as_of):
            return (fact,)

    class Triggers:
        def __init__(self):
            self.items = {}

        def record_trigger(self, trigger):
            inserted = trigger.trigger_id not in self.items
            self.items[trigger.trigger_id] = trigger
            return inserted

        def plan_for_scope(self, *, symbol, pipeline_id):
            raise KeyError((symbol, pipeline_id))

    triggers = Triggers()
    publisher = CanonicalFactTriggerPublisher(
        facts=Facts(),
        triggers=triggers,
        mandate=app_config.assessment.mandate,
        delta_policy=app_config.decision_state.delta_policy,
        pipeline_id=app_config.pipeline.version,
        trigger_expiry_seconds=app_config.trigger.trigger_expiry_seconds,
        required_freshness_seconds=app_config.risk.maximum_market_age_seconds,
        analysis_owner_symbol="BTCUSDT",
    )

    publisher.publish_recent(now)
    publisher.publish_recent(now)
    assert len(triggers.items) == 2
    assert {item.symbol for item in triggers.items.values()} == {
        "BTCUSDT",
        "ETHUSDT",
    }
    assert all(
        item.trigger_type == AnalysisTriggerType.CANONICAL_FACT_REVISED
        and item.occurred_at == now
        and item.evidence_ids == (fact.revision_id,)
        for item in triggers.items.values()
    )
    owner = next(item for item in triggers.items.values() if item.symbol == "BTCUSDT")
    assert owner.affected_symbols == ("ETHUSDT",)


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

    monkeypatch.setattr(trigger_runtime, "build_engine", lambda _database_url: object())
    monkeypatch.setattr(trigger_runtime, "require_current_schema", lambda _engine: None)
    monkeypatch.setattr(
        trigger_runtime,
        "SqlTriggerRepository",
        lambda _engine, _policy: object(),
    )
    monkeypatch.setattr(
        trigger_runtime,
        "PostgresTriggerLeadership",
        RejectingLeadership,
    )
    monkeypatch.setattr(
        trigger_runtime,
        "ensure_trigger_plans",
        lambda *_args, **_kwargs: events.append("plan-write"),
    )

    with pytest.raises(RuntimeError, match="已有 Trigger Dispatcher"):
        trigger_runtime.run_trigger_service(
            config=app_config,
            manifest=SimpleNamespace(manifest_id="release-v2"),
            database_url="postgresql://unused",
        )

    assert events == ["leadership"]


def test_trigger_plan_bootstrap_is_a_reusable_scheduling_use_case(
    app_config, replay_input,
) -> None:
    created = []

    class Repository:
        def current_plans_for_symbols(self, _symbols):
            return ()

        def plan_for_scope(self, *, symbol, pipeline_id):
            raise KeyError((symbol, pipeline_id))

        def create_plan(self, plan):
            created.append(plan)

    ensure_trigger_plans(
        repository=Repository(),
        symbols=("BTCUSDT",),
        pipeline_id="pipeline-v1",
        manifest_id="manifest-v1",
        heartbeat_seconds=900,
        high_impact_threshold=app_config.trigger.high_impact_threshold,
        debounce_seconds=30,
        now=replay_input.market.as_of,
    )

    assert len(created) == 1
    assert created[0].heartbeat_seconds == 900
    assert tuple(rule.rule_id for rule in created[0].event_rules) == (
        "canonical-fact-default",
        "intelligence-default",
        "market-shock-default",
        "position-recheck-default",
    )
    assert created[0].event_rules[0].minimum_priority == 0
    assert created[0].event_rules[1].minimum_priority == 80


def test_trigger_plan_bootstrap_adds_owned_wakeups_without_replacing_existing_plan(
    app_config, replay_input,
) -> None:
    now = replay_input.market.as_of
    official = ScheduledWakeup(
        wakeup_id="official-release",
        wake_at=now + timedelta(days=1),
        expires_at=now + timedelta(days=1, minutes=15),
        reason="官方数据发布时间",
    )
    candidate = ScheduledWakeup(
        wakeup_id="candidate-natural-window",
        wake_at=now + timedelta(days=2),
        expires_at=now + timedelta(days=2, minutes=30),
        reason="候选自然信号窗口",
    )
    initial = build_initial_trigger_plan(
        symbol="BTCUSDT",
        pipeline_id="pipeline-v1",
        manifest_id="manifest-v1",
        updated_at=now - timedelta(hours=1),
        heartbeat_seconds=900,
    ).model_copy(update={"scheduled_wakeups": (official,)})

    class Repository:
        def __init__(self):
            self.plan = initial
            self.patch_count = 0

        def current_plans_for_symbols(self, _symbols):
            return (self.plan,)

        def plan_for_scope(self, *, symbol, pipeline_id):
            assert (symbol, pipeline_id) == ("BTCUSDT", "pipeline-v1")
            return self.plan

        def create_plan(self, _plan):
            raise AssertionError("existing plan must be reused")

        def apply_patch(self, patch, *, now, current_manifest_id):
            self.patch_count += 1
            result = TriggerPlanGate(app_config.trigger).apply(
                self.plan,
                patch,
                now=now,
                current_manifest_id=current_manifest_id,
            )
            self.plan = result.plan
            return result

    repository = Repository()
    kwargs = {
        "repository": repository,
        "symbols": ("BTCUSDT",),
        "pipeline_id": "pipeline-v1",
        "manifest_id": "manifest-v1",
        "heartbeat_seconds": 900,
        "high_impact_threshold": app_config.trigger.high_impact_threshold,
        "debounce_seconds": 30,
        "now": now,
        "scheduled_wakeups_by_symbol": {"BTCUSDT": (candidate,)},
    }

    ensure_trigger_plans(**kwargs)
    ensure_trigger_plans(**kwargs)

    assert repository.patch_count == 1
    assert repository.plan.scheduled_wakeups == (official, candidate)


def test_immediate_trigger_use_case_applies_the_authoritative_plan_gate(
    app_config, replay_input
) -> None:
    now = replay_input.market.as_of
    plan = build_initial_trigger_plan(
        symbol="BTCUSDT",
        pipeline_id="pipeline-v1",
        manifest_id="manifest-v1",
        updated_at=now,
        heartbeat_seconds=None,
    )

    class Repository:
        def plan_for_scope(self, *, symbol, pipeline_id):
            assert (symbol, pipeline_id) == (plan.symbol, plan.pipeline_id)
            return plan

        def apply_patch(self, patch, *, now, current_manifest_id):
            return TriggerPlanGate(app_config.trigger).apply(
                plan,
                patch,
                now=now,
                current_manifest_id=current_manifest_id,
            )

    result = trigger_now(
        repository=Repository(),
        symbol=plan.symbol,
        pipeline_id=plan.pipeline_id,
        manifest_id=plan.manifest_id,
        request_id="manual-1",
        reason="risk review",
        now=now,
    )

    assert result.plan.revision == 2
    assert len(result.emitted_triggers) == 1
    assert result.emitted_triggers[0].trigger_type == AnalysisTriggerType.AGENT_WAKEUP
    assert result.emitted_triggers[0].review_reason == "risk review"


def test_set_trigger_heartbeat_updates_plan_without_emitting_call(
    app_config,
    replay_input,
) -> None:
    now = replay_input.market.as_of
    plan = build_initial_trigger_plan(
        symbol="BTCUSDT",
        pipeline_id="pipeline-v1",
        manifest_id="manifest-v1",
        updated_at=now,
        heartbeat_seconds=3600,
    )

    class Repository:
        def plan_for_scope(self, *, symbol, pipeline_id):
            assert (symbol, pipeline_id) == (plan.symbol, plan.pipeline_id)
            return plan

        def apply_patch(self, patch, *, now, current_manifest_id):
            return TriggerPlanGate(app_config.trigger).apply(
                plan,
                patch,
                now=now,
                current_manifest_id=current_manifest_id,
            )

    result = set_trigger_heartbeat(
        repository=Repository(),
        symbol=plan.symbol,
        pipeline_id=plan.pipeline_id,
        manifest_id=plan.manifest_id,
        heartbeat_seconds=900,
        now=now,
    )

    assert result.plan.revision == 2
    assert result.plan.heartbeat_seconds == 900
    assert result.emitted_triggers == ()


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


def test_plan_patch_prunes_expired_wakeup_without_blocking_future_changes(
    app_config,
    replay_input,
) -> None:
    now = replay_input.market.as_of
    plan = build_initial_trigger_plan(
        symbol="BTCUSDT",
        pipeline_id=app_config.pipeline.version,
        manifest_id="manifest-v1",
        updated_at=now - timedelta(hours=1),
        heartbeat_seconds=900,
    ).model_copy(
        update={
            "scheduled_wakeups": (
                ScheduledWakeup(
                    wakeup_id="expired-official-event",
                    wake_at=now - timedelta(minutes=10),
                    expires_at=now - timedelta(minutes=5),
                    reason="already expired",
                ),
            )
        }
    )
    patch = build_trigger_plan_patch(
        plan=plan,
        submitted_at=now,
        operations=(SetHeartbeat(heartbeat_seconds=None),),
    )

    revised = TriggerPlanGate(app_config.trigger).apply(
        plan,
        patch,
        now=now,
        current_manifest_id=plan.manifest_id,
    )

    assert revised.plan.scheduled_wakeups == ()
    assert revised.plan.heartbeat_seconds is None


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


def test_release_cutover_terminates_every_inactive_coordinator(app_config) -> None:
    class FakeHandle:
        def __init__(self) -> None:
            self.reason = None

        async def terminate(self, reason):
            self.reason = reason

    active_id = coordinator_workflow_id("BTCUSDT", app_config.pipeline.version)
    old_id = coordinator_workflow_id("BTCUSDT", "pipeline-old")
    orphan_id = "trigger_coordinator_orphaned_from_database"
    handles = {old_id: FakeHandle(), orphan_id: FakeHandle()}

    class FakeClient:
        async def _executions(self):
            for workflow_id in (active_id, orphan_id, old_id):
                yield SimpleNamespace(id=workflow_id)

        def list_workflows(self, query):
            assert query == (
                'WorkflowType="TriggerCoordinatorWorkflow" '
                'AND ExecutionStatus="Running"'
            )
            return self._executions()

        def get_workflow_handle(self, workflow_id):
            return handles[workflow_id]

    terminated = asyncio.run(
        terminate_inactive_trigger_coordinators(
            client=FakeClient(),
            active_symbols=("BTCUSDT",),
            active_pipeline_id=app_config.pipeline.version,
        )
    )

    assert terminated == tuple(sorted((old_id, orphan_id)))
    assert all(
        handle.reason == f"superseded by pipeline {app_config.pipeline.version}"
        for handle in handles.values()
    )


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
