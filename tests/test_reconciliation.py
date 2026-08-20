from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from temporalio.testing import WorkflowEnvironment

from investment_manager.execution.reconciliation.engine import (
    DifferenceKind,
    ReconciliationEngine,
    ReconciliationStatus,
)
from investment_manager.execution.reconciliation.repository import (
    SqlLocalTradingStateSource,
    SqlMockExchangeTruthSource,
    SqlReconciliationReportStore,
)
from investment_manager.execution.reconciliation.service import (
    ReconciliationActivities,
    ReconciliationTemporalCoordinator,
    ReconciliationTemporalWorker,
    ReconciliationWorkflowStatus,
    _seconds_until_next_bucket,
    build_reconciliation_workflow_request,
)
from investment_manager.execution.venue.mock import SqlMockExchange
from investment_manager.legacy.cycle import AnalysisCycle
from investment_manager.legacy.repository import SqlFactLedger
from investment_manager.legacy.shadow import SqlShadowStateReader
from investment_manager.risk.budget import SqlRiskBudgetStore
from investment_manager.schema import create_schema


def _sources(engine, app_config):
    values = {"initial_quote_balance": app_config.shadow.initial_quote_balance}
    return (
        SqlLocalTradingStateSource(engine, **values),
        SqlMockExchangeTruthSource(engine, **values),
    )


def _sql_cycle(engine, app_config):
    return AnalysisCycle.with_adapters(
        app_config,
        ledger=SqlFactLedger(engine),
        exchange=SqlMockExchange(engine, app_config.execution),
        risk_budget=SqlRiskBudgetStore(engine),
    )


def test_reconciliation_schedule_tracks_absolute_boundaries() -> None:
    almost_boundary = datetime(2026, 8, 18, 12, 0, 59, 750_000, tzinfo=UTC)
    after_slow_run = datetime(2026, 8, 18, 12, 1, 7, tzinfo=UTC)

    assert _seconds_until_next_bucket(almost_boundary, bucket_seconds=60) == 0.25
    assert _seconds_until_next_bucket(after_slow_run, bucket_seconds=60) == 53


def test_reconciliation_matches_independent_mock_exchange_journal(app_config, replay_input) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    result = _sql_cycle(engine, app_config).run(replay_input)
    assert result.order is not None
    local, remote = _sources(engine, app_config)
    as_of = replay_input.market.as_of

    report = ReconciliationEngine(app_config.reconciliation).compare(
        local.snapshot(as_of=as_of),
        remote.snapshot(as_of=as_of),
        as_of=as_of,
    )

    assert report.status == ReconciliationStatus.MATCHED
    assert report.freeze_new_risk is False
    assert report.differences == ()

    next_day = remote.snapshot(as_of=as_of + timedelta(days=1))
    assert next_day.account.daily_pnl == Decimal("0")
    assert isinstance(next_day.account.daily_pnl, Decimal)


def test_reconciliation_does_not_compare_local_portfolio_protection_state(
    app_config, replay_input
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    _sql_cycle(engine, app_config).run(replay_input)
    local, remote = _sources(engine, app_config)
    as_of = replay_input.market.as_of
    local_snapshot = local.snapshot(as_of=as_of)
    protected_local = local_snapshot.model_copy(
        update={
            "account": local_snapshot.account.model_copy(
                update={
                    "drawdown_fraction": "0.02",
                    "equity_high_water": "10000",
                    "kill_switch_active": True,
                }
            )
        }
    )

    report = ReconciliationEngine(app_config.reconciliation).compare(
        protected_local,
        remote.snapshot(as_of=as_of),
        as_of=as_of,
    )

    assert report.status == ReconciliationStatus.MATCHED
    assert report.differences == ()


def test_remote_order_without_local_commit_freezes_new_risk(app_config, replay_input) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    cycle = _sql_cycle(engine, app_config)
    prepared = cycle.prepare(replay_input)
    assert prepared.execution_request is not None
    cycle.exchange.submit(
        intent=prepared.execution_request.intent,
        risk=prepared.execution_request.risk_decision,
        market=prepared.execution_request.market,
    )
    local, remote = _sources(engine, app_config)
    as_of = replay_input.market.as_of

    report = ReconciliationEngine(app_config.reconciliation).compare(
        local.snapshot(as_of=as_of),
        remote.snapshot(as_of=as_of),
        as_of=as_of,
    )

    assert report.status == ReconciliationStatus.MISMATCH
    assert report.freeze_new_risk is True
    assert DifferenceKind.ORDER_MISSING_LOCAL in {item.kind for item in report.differences}
    assert DifferenceKind.POSITION_MISSING_LOCAL in {item.kind for item in report.differences}


def test_reconciliation_workflow_persists_and_replays_report(
    app_config, replay_input, tmp_path
) -> None:
    async def scenario() -> None:
        engine = create_engine(
            f"sqlite+pysqlite:///{tmp_path / 'reconciliation-temporal.db'}",
            connect_args={"check_same_thread": False},
        )
        create_schema(engine)
        _sql_cycle(engine, app_config).run(replay_input)
        local, remote = _sources(engine, app_config)
        reports = SqlReconciliationReportStore(engine)
        activities = ReconciliationActivities(local=local, remote=remote, reports=reports)
        async with await WorkflowEnvironment.start_time_skipping() as env:
            policy = app_config.temporal.model_copy(
                update={"reconciliation_task_queue": "reconciliation-test"}
            )
            coordinator = ReconciliationTemporalCoordinator(env.client, policy)
            request = build_reconciliation_workflow_request(
                as_of=replay_input.market.as_of,
                reconciliation_policy=app_config.reconciliation,
                temporal_policy=policy,
            )
            async with ReconciliationTemporalWorker(env.client, policy, activities):
                first = await coordinator.execute(request)
                replayed = await coordinator.execute(request)
                history = [
                    event
                    async for event in env.client.get_workflow_handle(
                        request.workflow_id
                    ).fetch_history_events()
                ]
            assert first.status == ReconciliationWorkflowStatus.COMPLETED
            assert first.report is not None
            assert first.report.status == ReconciliationStatus.MATCHED
            assert replayed == first
            assert reports.latest(as_of=replay_input.market.as_of) == first.report
            scheduled = [
                event.activity_task_scheduled_event_attributes
                for event in history
                if event.WhichOneof("attributes") == "activity_task_scheduled_event_attributes"
            ]
            assert len(scheduled) == 1
            assert scheduled[0].use_workflow_build_id is False

    asyncio.run(scenario())


def test_shadow_account_fails_closed_without_fresh_matched_report(app_config, replay_input) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    _sql_cycle(engine, app_config).run(replay_input)
    reader = SqlShadowStateReader(
        engine,
        maximum_reconciliation_age_seconds=(app_config.reconciliation.maximum_report_age_seconds),
    )
    as_of = replay_input.market.as_of

    before = reader.account_for_cycle(
        cycle_id="before-reconciliation",
        as_of=as_of,
        initial_quote_balance=app_config.shadow.initial_quote_balance,
    )
    assert before.reconciled is False

    local, remote = _sources(engine, app_config)
    report = ReconciliationEngine(app_config.reconciliation).compare(
        local.snapshot(as_of=as_of),
        remote.snapshot(as_of=as_of),
        as_of=as_of,
    )
    SqlReconciliationReportStore(engine).record(report)
    matched = reader.account_for_cycle(
        cycle_id="after-reconciliation",
        as_of=as_of,
        initial_quote_balance=app_config.shadow.initial_quote_balance,
    )
    assert matched.reconciled is True

    stale_at = as_of + timedelta(seconds=app_config.reconciliation.maximum_report_age_seconds + 1)
    stale = reader.account_for_cycle(
        cycle_id="stale-reconciliation",
        as_of=stale_at,
        initial_quote_balance=app_config.shadow.initial_quote_balance,
    )
    assert stale.reconciled is False
