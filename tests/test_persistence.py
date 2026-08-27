from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select

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
from investment_manager.schema import create_schema


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

    latest = store.latest_account_attempts(("codex_a", "codex_b"))
    assert set(latest) == {"codex_a"}
    assert latest["codex_a"].status == "FAILED"
    assert latest["codex_a"].failure == FailureClass.RATE_LIMIT
    assert latest["codex_a"].completed_at == now + timedelta(milliseconds=125)

    with pytest.raises(ValueError, match="审计事实不一致"):
        store.record_attempt(replace(attempt, status="SUCCEEDED", failure=None))

    with engine.connect() as connection:
        capacity = connection.execute(select(codex_account_capacity)).mappings().one()
        run = connection.execute(select(codex_runs)).mappings().one()
    serialized = str(capacity["payload"]) + str(run["payload"])
    assert capacity["account_id"] == "codex_a"
    assert run["error_class"] == "RATE_LIMIT"
    assert run["observed_at"] == now.replace(tzinfo=None)
    assert run["payload"]["duration_ms"] == 125
    assert run["payload"]["runtime_policy_version"] == "codex-runtime-test"
    assert run["payload"]["diagnostics"] == {
        "event_count": 4,
        "last_event": "turn/started",
    }
    assert run["payload"]["analysis_behavior_hash"] == "c" * 64
    assert "codex_home" not in serialized
    assert "auth.json" not in serialized
