from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select

from investment_manager.execution.tables import (
    fills,
    orders,
)
from investment_manager.forecast.codex.capacity import (
    CapacityBucket,
    CapacitySnapshot,
    CapacityWindow,
)
from investment_manager.forecast.codex.protocol import FailureClass
from investment_manager.forecast.codex.repository import (
    SqlAccountLeaseStore,
    SqlCodexAuditStore,
)
from investment_manager.forecast.codex.router import AttemptAudit
from investment_manager.forecast.tables import codex_account_capacity, codex_runs
from investment_manager.legacy.cycle import AnalysisCycle
from investment_manager.legacy.exchange import MockExchange
from investment_manager.legacy.repository import (
    SqlFactLedger,
    analysis_cycles,
    metric_observations,
)
from investment_manager.risk.budget import (
    SqlRiskBudgetStore,
    portfolio_risk_budgets,
    risk_reservations,
)
from investment_manager.schema import create_schema


@pytest.fixture
def sql_cycle(app_config):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    ledger = SqlFactLedger(engine)
    cycle = AnalysisCycle.with_adapters(
        app_config,
        ledger=ledger,
        exchange=MockExchange(app_config.execution),
        risk_budget=SqlRiskBudgetStore(engine),
    )
    return engine, cycle


def test_sql_ledger_persists_and_reconstructs_complete_cycle(sql_cycle, replay_input) -> None:
    engine, cycle = sql_cycle

    first = cycle.run(replay_input)
    reconstructed = cycle.run(replay_input)

    assert reconstructed == first
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(analysis_cycles)) == 1
        assert connection.scalar(select(func.count()).select_from(risk_reservations)) == 1
        assert connection.scalar(select(func.count()).select_from(orders)) == 1
        assert connection.scalar(select(func.count()).select_from(fills)) == 1
        assert connection.scalar(select(func.count()).select_from(metric_observations)) == 12
        budget = connection.execute(select(portfolio_risk_budgets)).mappings().one()
        assert budget["reserved_amount"] == 0
        assert budget["exposure_risk_amount"] > 0


def test_sql_ledger_records_actual_cycle_commit_time(app_config, replay_input) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    committed_at = replay_input.market.as_of + timedelta(seconds=7)
    ledger = SqlFactLedger(engine, clock=lambda: committed_at)
    cycle = AnalysisCycle.with_adapters(
        app_config,
        ledger=ledger,
        exchange=MockExchange(app_config.execution),
        risk_budget=SqlRiskBudgetStore(engine),
    )

    cycle.run(replay_input)

    with engine.connect() as connection:
        row = connection.execute(
            select(analysis_cycles.c.as_of, analysis_cycles.c.created_at)
        ).one()
    assert row.as_of == replay_input.market.as_of.replace(tzinfo=None)
    assert row.created_at == committed_at.replace(tzinfo=None)


def test_same_cycle_id_with_different_snapshot_is_rejected(sql_cycle, replay_input) -> None:
    _, cycle = sql_cycle
    cycle.run(replay_input)
    changed_account = replay_input.account.model_copy(update={"quote_balance": Decimal("9999")})
    changed_input = replay_input.model_copy(update={"account": changed_account})

    with pytest.raises(ValueError, match="冻结输入或面板哈希不同"):
        cycle.run(changed_input)


def test_sql_codex_lease_is_exclusive_and_reusable_after_release() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    store = SqlAccountLeaseStore(engine)
    now = datetime.now(tz=UTC)

    first = store.try_acquire("codex_a", "cycle-1", "attempt-1", now + timedelta(minutes=2))
    conflict = store.try_acquire("codex_a", "cycle-2", "attempt-2", now + timedelta(minutes=2))
    other = store.try_acquire("codex_b", "cycle-2", "attempt-3", now + timedelta(minutes=2))

    assert first is not None
    assert conflict is None
    assert other is not None
    assert store.has_active("codex_a", now)

    store.release(first.lease_id)
    replacement = store.try_acquire("codex_a", "cycle-3", "attempt-4", now + timedelta(minutes=2))
    assert replacement is not None


def test_sql_codex_audit_keeps_only_anonymous_capacity_and_run_metadata() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    store = SqlCodexAuditStore(engine)
    now = datetime.now(tz=UTC)
    snapshot = CapacitySnapshot(
        account_id="codex_a",
        observed_at=now,
        buckets=(
            CapacityBucket(
                limit_id="codex",
                primary=CapacityWindow(
                    used_percent=Decimal("35"),
                    window_duration_minutes=15,
                    resets_at=now + timedelta(minutes=15),
                ),
                secondary=None,
                reached_type=None,
            ),
        ),
    )
    attempt = AttemptAudit(
        run_id="run-1",
        cycle_id="cycle-before-ledger-write",
        account_id="codex_a",
        attempt=1,
        observed_at=now,
        completed_at=now + timedelta(milliseconds=125),
        duration_ms=125,
        runtime_policy_version="codex-runtime-test",
        status="FAILED",
        failure=FailureClass.RATE_LIMIT,
        bundle_hash="bundle-hash",
        usage={"input_tokens": 10},
        diagnostics={"event_count": 4, "last_event": "turn/started"},
        analysis_behavior_hash="c" * 64,
    )

    store.record_capacity(snapshot)
    store.record_attempt(attempt)
    store.record_attempt(attempt)

    with pytest.raises(ValueError, match="审计事实不一致"):
        store.record_attempt(replace(attempt, status="SUCCEEDED", failure=None))

    with engine.connect() as connection:
        capacity = connection.execute(select(codex_account_capacity)).mappings().one()
        run = connection.execute(select(codex_runs)).mappings().one()
    serialized = str(capacity["payload"]) + str(run["payload"])
    assert capacity["account_id"] == "codex_a"
    assert run["error_class"] == "RATE_LIMIT"
    assert run["payload"]["duration_ms"] == 125
    assert run["payload"]["runtime_policy_version"] == "codex-runtime-test"
    assert run["payload"]["diagnostics"] == {
        "event_count": 4,
        "last_event": "turn/started",
    }
    assert run["payload"]["analysis_behavior_hash"] == "c" * 64
    assert "codex_home" not in serialized
    assert "auth.json" not in serialized
