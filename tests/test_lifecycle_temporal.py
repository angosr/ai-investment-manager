from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal

from temporalio.testing import WorkflowEnvironment

from investment_manager.cycle import AnalysisCycle
from investment_manager.execution.ledger import InMemoryLifecycleLedger
from investment_manager.execution.lifecycle import OpenLifecycleRecord, PositionLifecycleManager
from investment_manager.execution.lifecycle_runtime import (
    LifecycleTemporalCoordinator,
    LifecycleTemporalWorker,
    LifecycleWorkflowExecution,
    LifecycleWorkflowStatus,
    PositionLifecycleActivities,
    build_lifecycle_workflow_request,
)
from investment_manager.execution.models import PositionLifecycleStatus
from investment_manager.execution.reconciliation import MockReconciler
from investment_manager.market.models import (
    ClosedMarketBar,
    MarketQuote,
    MarketTrade,
)
from investment_manager.market.repository import InMemoryMarketDataStore


def test_position_lifecycle_workflow_persists_progress_and_closes_after_timeout(
    app_config, replay_input
) -> None:
    cycle = AnalysisCycle.create(app_config)
    entry = cycle.run(replay_input)
    assert entry.position_lifecycle is not None
    assert entry.account_after is not None
    lifecycle = entry.position_lifecycle
    first_time = lifecycle.opened_at + timedelta(minutes=1)
    exit_time = lifecycle.max_exit_at + timedelta(minutes=1)
    store = InMemoryMarketDataStore()
    for bar in replay_input.market.bars:
        store.put_bar(
            ClosedMarketBar(
                symbol="BTCUSDT",
                interval="5m",
                open_time=bar.event_time,
                close_time=bar.event_time + timedelta(minutes=5) - timedelta(milliseconds=1),
                observed_at=bar.observed_at,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                source="recorded-test",
            )
        )
    for sequence, (at, price) in enumerate(
        (
            (first_time, lifecycle.entry_price + 1),
            (exit_time, lifecycle.entry_price + 2),
        ),
        start=1,
    ):
        store.put_quote(
            MarketQuote(
                quote_id=f"q-{sequence}",
                symbol="BTCUSDT",
                observed_at=at,
                bid=price - Decimal("0.01"),
                bid_quantity="1",
                ask=price + Decimal("0.01"),
                ask_quantity="1",
                update_id=sequence,
                source="recorded-test",
            )
        )
        store.put_trade(
            MarketTrade(
                trade_id=f"t-{sequence}",
                symbol="BTCUSDT",
                aggregate_trade_id=sequence,
                event_time=at,
                observed_at=at,
                price=price,
                quantity="0.1",
                buyer_is_maker=False,
                source="recorded-test",
            )
        )

    class AccountState:
        def account_for_cycle(self, *, cycle_id, as_of, initial_quote_balance):
            return entry.account_after.model_copy(
                update={"cycle_id": cycle_id, "as_of": as_of, "observed_at": as_of}
            )

    times = iter((first_time, exit_time))
    lifecycle_ledger = InMemoryLifecycleLedger()
    activities = PositionLifecycleActivities(
        config=app_config,
        market_store=store,
        state=AccountState(),
        manager=PositionLifecycleManager(
            exchange=cycle.exchange,
            reconciler=MockReconciler(),
            risk_budget=cycle.risk_budget,
            lifecycle_ledger=lifecycle_ledger,
        ),
        clock=lambda: next(times),
    )

    async def scenario() -> LifecycleWorkflowExecution:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            policy = app_config.temporal.model_copy(
                update={"lifecycle_task_queue": "quant-core-lifecycle-test"}
            )
            request = build_lifecycle_workflow_request(
                OpenLifecycleRecord(
                    lifecycle=lifecycle,
                    pipeline_version=app_config.pipeline.version,
                ),
                temporal_policy=policy,
                poll_seconds=2,
            )
            coordinator = LifecycleTemporalCoordinator(env.client, policy)
            async with LifecycleTemporalWorker(env.client, policy, activities):
                workflow_id = await coordinator.ensure(request)
                handle = env.client.get_workflow_handle(workflow_id)
                raw = await handle.result()
                assert await coordinator.ensure(request) == workflow_id
            return LifecycleWorkflowExecution.model_validate(raw)

    result = asyncio.run(scenario())
    assert result.status == LifecycleWorkflowStatus.CLOSED
    assert result.lifecycle is not None
    assert result.lifecycle.status == PositionLifecycleStatus.CLOSED
    assert result.lifecycle.highest_price == lifecycle.entry_price + 2
    assert cycle.risk_budget.status(lifecycle.reservation_id) == "RELEASED"
    assert lifecycle_ledger.count == 1
