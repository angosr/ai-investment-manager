from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from temporalio.testing import WorkflowEnvironment

from investment_manager.execution.lifecycle.manager import PositionLifecycleManager
from investment_manager.execution.reconciliation.engine import MockReconciler
from investment_manager.execution.venue.mock import SqlMockExchange
from investment_manager.governance.evaluation.outcome_service import (
    OutcomeEvaluationActivities,
    OutcomeEvaluationTemporalCoordinator,
    OutcomeEvaluationTemporalWorker,
    OutcomeEvaluationWorkflowStatus,
    _seconds_until_next_poll,
    build_outcome_evaluation_workflow_request,
)
from investment_manager.governance.evaluation.outcome_store import SqlOutcomeWindowRepository
from investment_manager.governance.evaluation.performance import (
    OutcomeWindowEvaluator,
    OutcomeWindowStatus,
)
from investment_manager.legacy.cycle import AnalysisCycle
from investment_manager.legacy.repository import (
    SqlFactLedger,
    SqlLifecycleLedger,
)
from investment_manager.market.models import MarketSnapshot
from investment_manager.risk.budget import SqlRiskBudgetStore
from investment_manager.schema import create_schema


def _cycle(engine, config):
    return AnalysisCycle.with_adapters(
        config,
        ledger=SqlFactLedger(engine),
        exchange=SqlMockExchange(engine, config.execution),
        risk_budget=SqlRiskBudgetStore(engine),
    )


def test_outcome_window_aggregates_closed_trade_without_recomputing_it(
    app_config, replay_input
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    cycle = _cycle(engine, app_config)
    opened = cycle.run(replay_input)
    assert opened.position_lifecycle is not None
    assert opened.account_after is not None
    close_time = opened.position_lifecycle.max_exit_at + timedelta(minutes=1)
    close_market = MarketSnapshot.model_validate(
        {
            **replay_input.market.model_dump(mode="json"),
            "cycle_id": "outcome-window-close",
            "as_of": close_time,
            "observed_at": close_time,
        }
    )
    closed = PositionLifecycleManager(
        exchange=cycle.exchange,
        reconciler=MockReconciler(),
        risk_budget=cycle.risk_budget,
        lifecycle_ledger=SqlLifecycleLedger(engine),
    ).evaluate(
        lifecycle=opened.position_lifecycle,
        market=close_market,
        account=opened.account_after,
        pipeline_version=app_config.pipeline.version,
    )
    assert closed.outcome is not None
    repository = SqlOutcomeWindowRepository(engine)
    window_start = replay_input.market.as_of - timedelta(minutes=1)
    window_end = close_time + timedelta(minutes=1)
    facts = repository.load(
        pipeline_version=app_config.pipeline.version,
        window_start=window_start,
        window_end=window_end,
    )

    report = OutcomeWindowEvaluator(version=app_config.outcome_evaluation.version).evaluate(
        pipeline_version=app_config.pipeline.version,
        window_start=window_start,
        window_end=window_end,
        cycles=facts.cycles,
        outcomes=facts.outcomes,
        unresolved_cycle_ids=facts.unresolved_cycle_ids,
    )
    repository.record(report)

    assert report.status == OutcomeWindowStatus.COMPLETE
    assert report.cycle_count == 1
    assert report.closed_trade_count == 1
    assert report.net_pnl == closed.outcome.net_pnl
    assert report.total_fees == closed.outcome.total_fees
    assert report.incremental_net_pnl_vs_never_trade == report.net_pnl
    assert (
        repository.latest(
            pipeline_version=app_config.pipeline.version,
            window_start=window_start,
            window_end=window_end,
        )
        == report
    )


def test_open_trade_keeps_outcome_window_incomplete(app_config, replay_input) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    _cycle(engine, app_config).run(replay_input)
    repository = SqlOutcomeWindowRepository(engine)
    window_start = replay_input.market.as_of - timedelta(minutes=1)
    window_end = replay_input.market.as_of + timedelta(minutes=1)
    facts = repository.load(
        pipeline_version=app_config.pipeline.version,
        window_start=window_start,
        window_end=window_end,
    )

    report = OutcomeWindowEvaluator(version=app_config.outcome_evaluation.version).evaluate(
        pipeline_version=app_config.pipeline.version,
        window_start=window_start,
        window_end=window_end,
        cycles=facts.cycles,
        outcomes=facts.outcomes,
        unresolved_cycle_ids=facts.unresolved_cycle_ids,
    )

    assert report.status == OutcomeWindowStatus.INCOMPLETE
    assert report.unresolved_cycle_ids == (replay_input.market.cycle_id,)


def test_outcome_evaluation_workflow_replays_complete_no_trade_window(
    app_config, replay_input
) -> None:
    async def scenario() -> None:
        engine = create_engine(
            "sqlite+pysqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        create_schema(engine)
        no_trade_input = replay_input.model_copy(
            update={"frequency_orders_today": app_config.frequency.maximum_orders_per_day}
        )
        result = _cycle(engine, app_config).run(no_trade_input)
        assert result.order is None
        repository = SqlOutcomeWindowRepository(engine)
        activities = OutcomeEvaluationActivities(repository)
        async with await WorkflowEnvironment.start_time_skipping() as env:
            temporal_policy = app_config.temporal.model_copy(
                update={"outcome_evaluation_task_queue": "outcome-evaluation-test"}
            )
            request = build_outcome_evaluation_workflow_request(
                pipeline_version=app_config.pipeline.version,
                window_start=replay_input.market.as_of - timedelta(minutes=1),
                window_end=replay_input.market.as_of + timedelta(minutes=1),
                policy=app_config.outcome_evaluation,
                temporal_policy=temporal_policy,
            )
            coordinator = OutcomeEvaluationTemporalCoordinator(env.client, temporal_policy)
            async with OutcomeEvaluationTemporalWorker(env.client, temporal_policy, activities):
                first = await coordinator.execute(request)
                replayed = await coordinator.execute(request)
            assert first.status == OutcomeEvaluationWorkflowStatus.COMPLETED
            assert first.report is not None
            assert first.report.status == OutcomeWindowStatus.COMPLETE
            assert first.report.cycle_count == 1
            assert first.report.closed_trade_count == 0
            assert replayed == first

    asyncio.run(scenario())


def test_outcome_evaluation_poll_uses_absolute_utc_buckets() -> None:
    almost_boundary = datetime(2026, 8, 18, 12, 4, 59, 750000, tzinfo=UTC)
    after_slow_run = datetime(2026, 8, 18, 12, 5, 7, tzinfo=UTC)

    assert _seconds_until_next_poll(almost_boundary, poll_seconds=300) == 0.25
    assert _seconds_until_next_poll(after_slow_run, poll_seconds=300) == 293
