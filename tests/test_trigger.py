from __future__ import annotations

from datetime import timedelta

import pytest

from quant_core.trigger import (
    AddWakeup,
    AnalysisEventRule,
    AnalysisTriggerType,
    DeleteEventRule,
    DeleteWakeup,
    ScheduledWakeup,
    SetAiPaused,
    SetHeartbeat,
    TriggerNow,
    TriggerPlanGate,
    UpdateWakeup,
    UpsertEventRule,
    build_initial_trigger_plan,
    build_trigger_plan_patch,
)


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
