from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from investment_manager.domain import IntelligenceEvent
from investment_manager.research.dataset import freeze_historical_events
from investment_manager.research.trigger_replay import (
    ExternalTriggerReplaySpec,
    run_external_trigger_replay,
)
from investment_manager.trigger import (
    AnalysisEventRule,
    AnalysisTriggerType,
    build_initial_trigger_plan,
)


def _event(
    *,
    observed_at: datetime,
    evidence_id: str,
    impact: Decimal,
    symbols: tuple[str, ...] = ("BTCUSDT",),
) -> IntelligenceEvent:
    return IntelligenceEvent(
        evidence_id=evidence_id,
        normalizer_version="test-normalizer-v1",
        acquisition_route="test-archive-v1",
        event_time=observed_at - timedelta(seconds=5),
        observed_at=observed_at,
        source="test-source",
        title=evidence_id,
        body=evidence_id,
        symbols=symbols,
        relevance=Decimal("1"),
        impact=impact,
        source_reliability=Decimal("0.8"),
        novelty=Decimal("1"),
    )


def _plan(
    app_config,
    start: datetime,
    *,
    symbol: str = "BTCUSDT",
    ordinary_cooldown_seconds: int = 900,
):
    return build_initial_trigger_plan(
        symbol=symbol,
        pipeline_id=app_config.pipeline.version,
        manifest_id="manifest-v1",
        updated_at=start,
        heartbeat_seconds=None,
        event_rules=(
            AnalysisEventRule(
                rule_id="news-default",
                trigger_type=AnalysisTriggerType.INTELLIGENCE_INSERTED,
                minimum_priority=80,
                coalesce_seconds=120,
                ordinary_cooldown_seconds=ordinary_cooldown_seconds,
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


def test_external_trigger_replay_matches_coalesce_specific_cooldown_and_latency(
    app_config,
) -> None:
    start = datetime(2026, 8, 19, 12, tzinfo=UTC)
    end = start + timedelta(hours=1)
    events = (
        _event(
            observed_at=start + timedelta(minutes=1),
            evidence_id="ordinary-1",
            impact=Decimal("0.84"),
        ),
        _event(
            observed_at=start + timedelta(minutes=1, seconds=30),
            evidence_id="ordinary-2",
            impact=Decimal("0.83"),
        ),
        _event(
            observed_at=start + timedelta(minutes=4),
            evidence_id="urgent-1",
            impact=Decimal("0.98"),
        ),
        _event(
            observed_at=start + timedelta(minutes=5),
            evidence_id="below-threshold",
            impact=Decimal("0.70"),
        ),
    )
    dataset = freeze_historical_events(
        events=events,
        source="test-archive",
        requested_start=start,
        requested_end=end,
        collected_at=end,
    )
    spec = ExternalTriggerReplaySpec.freeze(
        plans=(_plan(app_config, start),),
        config=app_config,
        analysis_duration_seconds=10,
    )

    replay = run_external_trigger_replay(
        event_dataset=dataset,
        spec=spec,
        replay_start=start,
        replay_end=end,
    )

    scope = replay.scopes[0]
    assert scope.source_event_count == 4
    assert scope.accepted_trigger_count == 3
    assert scope.rejected_trigger_count == 1
    assert len(replay.batches) == 2
    first, second = replay.batches
    assert first.batch.created_at == start + timedelta(minutes=3)
    assert first.analysis_completed_at == start + timedelta(minutes=3, seconds=10)
    assert {item.dedup_key for item in first.batch.triggers} == {
        "ordinary-1",
        "ordinary-2",
    }
    assert second.batch.created_at == start + timedelta(minutes=8, seconds=10)
    assert tuple(item.dedup_key for item in second.batch.triggers) == ("urgent-1",)
    assert "SIMULTANEOUS_ADMISSION_ORDER_ASSUMPTION" not in replay.limitations


def test_external_trigger_replay_discards_event_that_expires_during_cooldown(
    app_config,
) -> None:
    start = datetime(2026, 8, 19, 12, tzinfo=UTC)
    end = start + timedelta(hours=1)
    events = (
        _event(
            observed_at=start,
            evidence_id="first",
            impact=Decimal("0.84"),
        ),
        _event(
            observed_at=start + timedelta(minutes=3),
            evidence_id="expires",
            impact=Decimal("0.84"),
        ),
    )
    dataset = freeze_historical_events(
        events=events,
        source="test-archive",
        requested_start=start,
        requested_end=end,
        collected_at=end,
    )
    spec = ExternalTriggerReplaySpec.freeze(
        plans=(_plan(app_config, start, ordinary_cooldown_seconds=1_200),),
        config=app_config,
        analysis_duration_seconds=10,
    )

    replay = run_external_trigger_replay(
        event_dataset=dataset,
        spec=spec,
        replay_start=start,
        replay_end=end,
    )

    assert len(replay.batches) == 1
    assert replay.scopes[0].expired_trigger_count == 1
    assert replay.scopes[0].unprocessed_trigger_count == 0


def test_external_trigger_replay_enforces_global_cross_symbol_admission(
    app_config,
) -> None:
    start = datetime(2026, 8, 19, 12, tzinfo=UTC)
    end = start + timedelta(minutes=10)
    events = (
        _event(
            observed_at=start,
            evidence_id="shared",
            impact=Decimal("1"),
            symbols=("BTCUSDT", "ETHUSDT"),
        ),
    )
    dataset = freeze_historical_events(
        events=events,
        source="test-archive",
        requested_start=start,
        requested_end=end,
        collected_at=end,
    )
    spec = ExternalTriggerReplaySpec.freeze(
        plans=(
            _plan(app_config, start, symbol="BTCUSDT"),
            _plan(app_config, start, symbol="ETHUSDT"),
        ),
        config=app_config,
        analysis_duration_seconds=10,
    )

    replay = run_external_trigger_replay(
        event_dataset=dataset,
        spec=spec,
        replay_start=start,
        replay_end=end,
    )

    assert [item.batch.symbol for item in replay.batches] == [
        "BTCUSDT",
        "ETHUSDT",
    ]
    assert replay.batches[0].batch.created_at == start
    assert replay.batches[1].batch.created_at == start + timedelta(
        seconds=app_config.trigger.minimum_call_interval_seconds
    )
    assert "SIMULTANEOUS_ADMISSION_ORDER_ASSUMPTION" in replay.limitations


def test_external_trigger_replay_carries_last_global_admission(app_config) -> None:
    start = datetime(2026, 8, 19, 12, tzinfo=UTC)
    end = start + timedelta(minutes=1)
    dataset = freeze_historical_events(
        events=(
            _event(
                observed_at=start,
                evidence_id="shared",
                impact=Decimal("1"),
                symbols=("BTCUSDT", "ETHUSDT"),
            ),
        ),
        source="test-archive",
        requested_start=start,
        requested_end=end,
        collected_at=end,
    )
    prior_call = start - timedelta(seconds=5)
    spec = ExternalTriggerReplaySpec.freeze(
        plans=(
            _plan(app_config, start, symbol="BTCUSDT"),
            _plan(app_config, start, symbol="ETHUSDT"),
        ),
        config=app_config,
        analysis_duration_seconds=10,
        initial_global_last_admitted_at=prior_call,
        initial_state_source="EXACT",
    )

    replay = run_external_trigger_replay(
        event_dataset=dataset,
        spec=spec,
        replay_start=start,
        replay_end=end,
    )

    assert len(replay.batches) == 2
    assert replay.batches[0].batch.created_at == start + timedelta(seconds=10)
    assert replay.batches[1].batch.created_at == start + timedelta(seconds=25)
