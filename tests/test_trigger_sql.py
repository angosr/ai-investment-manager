from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine, func, select

from investment_manager.information.repository import SqlEventStore
from investment_manager.information.tables import normalized_events
from investment_manager.persistence import (
    analysis_call_admissions,
    analysis_trigger_batches,
    analysis_trigger_events,
    analysis_trigger_plans,
    trigger_outbox,
)
from investment_manager.schema import create_schema
from investment_manager.trigger import (
    AddWakeup,
    AnalysisTriggerType,
    ScheduledWakeup,
    TriggerNow,
    build_initial_trigger_plan,
    build_trigger_batch,
    build_trigger_event,
    build_trigger_plan_patch,
)
from investment_manager.trigger_sql import SqlTriggerRepository


def test_intelligence_insert_and_trigger_outbox_are_one_idempotent_fact(replay_input) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    store = SqlEventStore(engine, pipeline_id="pipeline-v1")
    event = replay_input.events[0]

    assert store.put(event)
    assert not store.put(event)

    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(normalized_events)) == 1
        assert connection.scalar(select(func.count()).select_from(analysis_trigger_events)) == len(
            event.symbols
        )
        assert connection.scalar(select(func.count()).select_from(trigger_outbox)) == len(
            event.symbols
        )


def test_plan_patch_plan_wakeups_and_trigger_now_commit_atomically(
    app_config, replay_input
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    repository = SqlTriggerRepository(engine, app_config.trigger)
    now = replay_input.market.as_of
    initial = build_initial_trigger_plan(
        symbol="BTCUSDT",
        pipeline_id="pipeline-v1",
        manifest_id="manifest-v1",
        updated_at=now,
        heartbeat_seconds=900,
    )
    assert repository.create_plan(initial) == initial
    wakeup = ScheduledWakeup(
        wakeup_id="wakeup-1",
        wake_at=now + timedelta(hours=1),
        expires_at=now + timedelta(hours=1, minutes=5),
        reason="计划复核",
    )
    patch = build_trigger_plan_patch(
        plan=initial,
        submitted_at=now,
        operations=(
            AddWakeup(wakeup=wakeup),
            TriggerNow(request_id="now-1", reason="立即复核"),
        ),
    )

    result = repository.apply_patch(
        patch,
        now=now,
        current_manifest_id="manifest-v1",
    )
    replayed = repository.apply_patch(
        patch,
        now=now,
        current_manifest_id="manifest-v1",
    )

    assert result.plan.revision == 2
    assert replayed.plan == result.plan
    assert replayed.emitted_triggers == ()
    assert repository.current_plan(initial.plan_id) == result.plan
    batch = build_trigger_batch(
        plan=result.plan,
        triggers=result.emitted_triggers,
        created_at=now,
        deadline=now + timedelta(minutes=5),
    )
    assert repository.record_batch(batch, analysis_submitted_at=now)
    assert not repository.record_batch(batch, analysis_submitted_at=now)
    pending = repository.pending_outbox(as_of=now)
    kinds = [item.message_kind.value for item in pending]
    assert kinds.count("PLAN_REVISED") == 2
    assert kinds.count("TRIGGER_CREATED") == 1
    repository.mark_delivered(pending[0].outbox_id, delivered_at=now)
    repository.mark_delivered(pending[0].outbox_id, delivered_at=now)
    with engine.connect() as connection:
        current_count = connection.scalar(
            select(func.count())
            .select_from(analysis_trigger_plans)
            .where(analysis_trigger_plans.c.is_current.is_(True))
        )
        batch_count = connection.scalar(select(func.count()).select_from(analysis_trigger_batches))
    assert current_count == 1
    assert batch_count == 1


def test_analysis_call_admission_is_global_idempotent_and_interval_only(
    app_config, replay_input
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    policy = app_config.trigger.model_copy(update={"minimum_call_interval_seconds": 15})
    repository = SqlTriggerRepository(engine, policy)
    now = replay_input.market.as_of

    def batch(symbol: str, sequence: int, at):
        plan = build_initial_trigger_plan(
            symbol=symbol,
            pipeline_id="pipeline-v1",
            manifest_id="manifest-v1",
            updated_at=now,
            heartbeat_seconds=None,
        )
        trigger = build_trigger_event(
            trigger_type=AnalysisTriggerType.AGENT_WAKEUP,
            symbol=symbol,
            pipeline_id="pipeline-v1",
            occurred_at=at,
            observed_at=at,
            priority=100,
            dedup_key=f"call-{sequence}",
        )
        return build_trigger_batch(
            plan=plan,
            triggers=(trigger,),
            created_at=at,
            deadline=at + timedelta(minutes=5),
        )

    first = batch("BTCUSDT", 1, now)
    second = batch("ETHUSDT", 2, now + timedelta(seconds=10))
    third = batch("BTCUSDT", 3, now + timedelta(seconds=30))

    admitted = repository.admit_analysis_call(first, requested_at=now)
    replayed = repository.admit_analysis_call(first, requested_at=now + timedelta(seconds=1))
    interval_limited = repository.admit_analysis_call(
        second, requested_at=now + timedelta(seconds=10)
    )
    admitted_second = repository.admit_analysis_call(
        second, requested_at=now + timedelta(seconds=15)
    )
    admitted_third = repository.admit_analysis_call(
        third, requested_at=now + timedelta(seconds=30)
    )

    assert admitted.admitted_at == now
    assert replayed.admitted_at == now
    assert interval_limited.retry_at == now + timedelta(seconds=15)
    assert admitted_second.admitted_at == now + timedelta(seconds=15)
    assert admitted_third.admitted_at == now + timedelta(seconds=30)
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(analysis_call_admissions)) == 3


def test_current_plans_for_symbols_returns_each_pipeline_current_revision(
    app_config, replay_input
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    repository = SqlTriggerRepository(engine, app_config.trigger)
    now = replay_input.market.as_of
    expected = tuple(
        build_initial_trigger_plan(
            symbol=symbol,
            pipeline_id=pipeline_id,
            manifest_id=f"manifest-{pipeline_id}",
            updated_at=now,
            heartbeat_seconds=None,
        )
        for symbol, pipeline_id in (
            ("BTCUSDT", "pipeline-v1"),
            ("BTCUSDT", "pipeline-v2"),
            ("ETHUSDT", "pipeline-v1"),
        )
    )
    for plan in expected:
        repository.create_plan(plan)

    assert repository.current_plans_for_symbols(("BTCUSDT",)) == expected[:2]
    assert repository.current_plans_for_symbols(()) == ()
