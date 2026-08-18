from __future__ import annotations

import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text

from quant_core.cycle import AnalysisCycle
from quant_core.domain import MarketSnapshot
from quant_core.execution import MockExchange
from quant_core.lifecycle import PositionLifecycleManager
from quant_core.market_data_sql import market_metadata
from quant_core.persistence import (
    SqlEventStore,
    SqlFactLedger,
    SqlLifecycleLedger,
    SqlOpenLifecycleRepository,
    SqlRiskBudgetStore,
    account_snapshots,
    build_engine,
    decision_outcomes,
    metadata,
    metric_observations,
    orders,
    portfolio_risk_budgets,
)
from quant_core.reconciliation import MockReconciler
from quant_core.trigger import TriggerNow, build_initial_trigger_plan, build_trigger_plan_patch
from quant_core.trigger_sql import (
    PostgresOutboxListener,
    PostgresTriggerLeadership,
    SqlTriggerRepository,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.integration
def test_postgres_cycle_transaction_and_risk_budget(app_config, replay_input) -> None:
    database_url = os.environ.get("QUANT_CORE_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("未配置隔离的 PostgreSQL 测试数据库")
    if "quant_core_test" not in database_url:
        raise RuntimeError("集成测试只允许操作名称包含 quant_core_test 的专用数据库")

    engine = build_engine(database_url)
    if engine.dialect.name != "postgresql":
        raise RuntimeError("该契约测试必须使用 PostgreSQL")
    market_metadata.drop_all(engine)
    metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    migration_config = Config(str(ROOT / "alembic.ini"))
    migration_config.set_main_option("script_location", str(ROOT / "migrations"))
    migration_config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(migration_config, "head")
    first_leader = PostgresTriggerLeadership(
        engine, app_config.trigger.dispatcher_advisory_lock_key
    )
    second_leader = PostgresTriggerLeadership(
        engine, app_config.trigger.dispatcher_advisory_lock_key
    )
    assert first_leader.acquire()
    assert not second_leader.acquire()
    first_leader.release()
    assert second_leader.acquire()
    second_leader.release()
    with PostgresOutboxListener(engine) as listener:
        assert SqlEventStore(engine, pipeline_id=app_config.pipeline.version).put(
            replay_input.events[0]
        )
        assert listener.wait(1)
        trigger_plans = SqlTriggerRepository(engine, app_config.trigger)
        initial_plan = trigger_plans.create_plan(
            build_initial_trigger_plan(
                symbol="BTCUSDT",
                pipeline_id=app_config.pipeline.version,
                manifest_id="release-bootstrap-v1",
                updated_at=replay_input.market.as_of,
                heartbeat_seconds=900,
            )
        )
        assert listener.wait(1)
        patch = build_trigger_plan_patch(
            plan=initial_plan,
            submitted_at=replay_input.market.as_of,
            operations=(TriggerNow(request_id="postgres-now-1", reason="PostgreSQL 契约"),),
        )
        revised = trigger_plans.apply_patch(
            patch,
            now=replay_input.market.as_of,
            current_manifest_id="release-bootstrap-v1",
        )
        assert revised.plan.revision == 2
        assert listener.wait(1)
    cycle = AnalysisCycle.with_adapters(
        app_config,
        ledger=SqlFactLedger(engine),
        exchange=MockExchange(app_config.execution),
        risk_budget=SqlRiskBudgetStore(engine),
    )

    first = cycle.run(replay_input)
    replayed = cycle.run(replay_input)

    assert replayed == first
    assert first.order is not None
    assert len(cycle.exchange.orders) == 1

    assert first.position_lifecycle is not None
    assert first.account_after is not None
    open_repository = SqlOpenLifecycleRepository(engine)
    assert [item.lifecycle.position_id for item in open_repository.list_open()] == [
        first.position_lifecycle.position_id
    ]
    exit_time = first.position_lifecycle.max_exit_at + timedelta(minutes=1)
    exit_market = MarketSnapshot.model_validate(
        {
            **replay_input.market.model_dump(mode="json"),
            "cycle_id": "cycle-postgres-exit-001",
            "as_of": exit_time,
            "observed_at": exit_time,
            "bid": first.position_lifecycle.entry_price + Decimal("0.99"),
            "ask": first.position_lifecycle.entry_price + Decimal("1.01"),
            "last": first.position_lifecycle.entry_price + Decimal("1"),
        }
    )
    lifecycle_ledger = SqlLifecycleLedger(engine)
    manager = PositionLifecycleManager(
        exchange=cycle.exchange,
        reconciler=MockReconciler(),
        risk_budget=cycle.risk_budget,
        lifecycle_ledger=lifecycle_ledger,
    )

    closed = manager.evaluate(
        lifecycle=first.position_lifecycle,
        market=exit_market,
        account=first.account_after,
        pipeline_version=app_config.pipeline.version,
    )
    replayed_close = manager.evaluate(
        lifecycle=first.position_lifecycle,
        market=exit_market,
        account=first.account_after,
        pipeline_version=app_config.pipeline.version,
    )

    assert closed == replayed_close
    assert closed.outcome is not None
    assert open_repository.list_open() == ()
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(orders)) == 2
        assert connection.scalar(select(func.count()).select_from(account_snapshots)) == 3
        assert connection.scalar(select(func.count()).select_from(decision_outcomes)) == 1
        assert connection.scalar(select(func.count()).select_from(metric_observations)) == 16
        budget = connection.execute(select(portfolio_risk_budgets)).mappings().one()
        assert budget["reserved_amount"] == 0
        assert budget["exposure_risk_amount"] == 0
