from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine, func, select

from quant_core.persistence import (
    SqlEventStore,
    analysis_trigger_batches,
    analysis_trigger_events,
    analysis_trigger_plans,
    create_schema,
    normalized_events,
    trigger_outbox,
)
from quant_core.trigger import (
    AddWakeup,
    ScheduledWakeup,
    TriggerNow,
    build_initial_trigger_plan,
    build_trigger_batch,
    build_trigger_plan_patch,
)
from quant_core.trigger_sql import SqlTriggerRepository


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
