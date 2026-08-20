from datetime import UTC, datetime

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.analyst import AttemptAudit, CapacitySnapshot, CodexLease
from investment_manager.forecast.tables import (
    codex_account_capacity,
    codex_account_leases,
    codex_runs,
)
from investment_manager.kernel.identity import stable_id


class SqlAccountLeaseStore:
    """数据库唯一约束保证跨 Worker 每账号最多一个 ACTIVE 租约。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def try_acquire(
        self, account_id: str, cycle_id: str, attempt_id: str, expires_at
    ) -> CodexLease | None:
        lease = CodexLease(
            lease_id=stable_id("lease", account_id, cycle_id, attempt_id),
            account_id=account_id,
            cycle_id=cycle_id,
            attempt_id=attempt_id,
            expires_at=expires_at,
        )
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    update(codex_account_leases)
                    .where(
                        codex_account_leases.c.account_id == account_id,
                        codex_account_leases.c.status == "ACTIVE",
                        codex_account_leases.c.expires_at <= datetime.now(tz=UTC),
                    )
                    .values(status="EXPIRED")
                )
                connection.execute(
                    insert(codex_account_leases).values(
                        lease_id=lease.lease_id,
                        account_id=account_id,
                        cycle_id=cycle_id,
                        attempt_id=attempt_id,
                        expires_at=expires_at,
                        status="ACTIVE",
                    )
                )
        except IntegrityError:
            return None
        return lease

    def release(self, lease_id: str) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                update(codex_account_leases)
                .where(
                    codex_account_leases.c.lease_id == lease_id,
                    codex_account_leases.c.status == "ACTIVE",
                )
                .values(status="RELEASED")
            )

    def has_active(self, account_id: str, now) -> bool:
        with self._engine.begin() as connection:
            connection.execute(
                update(codex_account_leases)
                .where(
                    codex_account_leases.c.account_id == account_id,
                    codex_account_leases.c.status == "ACTIVE",
                    codex_account_leases.c.expires_at <= now,
                )
                .values(status="EXPIRED")
            )
            active = connection.execute(
                select(codex_account_leases.c.lease_id).where(
                    codex_account_leases.c.account_id == account_id,
                    codex_account_leases.c.status == "ACTIVE",
                )
            ).scalar_one_or_none()
        return active is not None


class SqlCodexAuditStore:
    """仅保存匿名账号、额度窗口和运行元数据，不保存目录或完整账号响应。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record_capacity(self, snapshot: CapacitySnapshot) -> None:
        payload = {
            "account_id": snapshot.account_id,
            "observed_at": snapshot.observed_at.isoformat(),
            "effective_headroom": str(snapshot.effective_headroom),
            "buckets": [
                {
                    "limit_id": bucket.limit_id,
                    "primary": self._window_payload(bucket.primary),
                    "secondary": self._window_payload(bucket.secondary),
                    "reached_type": bucket.reached_type,
                }
                for bucket in snapshot.buckets
            ],
        }
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(codex_account_capacity).values(
                        account_id=snapshot.account_id,
                        observed_at=snapshot.observed_at,
                        effective_headroom=snapshot.effective_headroom,
                        healthy=snapshot.effective_headroom > 0,
                        payload=payload,
                    )
                )
        except IntegrityError:
            return

    def record_attempt(self, attempt: AttemptAudit) -> None:
        payload = {
            "observed_at": attempt.observed_at.isoformat(),
            "completed_at": attempt.completed_at.isoformat(),
            "duration_ms": attempt.duration_ms,
            "runtime_policy_version": attempt.runtime_policy_version,
            "bundle_hash": attempt.bundle_hash,
            "usage": attempt.usage,
            "diagnostics": attempt.diagnostics,
        }
        if attempt.analysis_behavior_hash is not None:
            payload["analysis_behavior_hash"] = attempt.analysis_behavior_hash
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(codex_runs).values(
                        run_id=attempt.run_id,
                        cycle_id=attempt.cycle_id,
                        account_id=attempt.account_id,
                        attempt=attempt.attempt,
                        status=attempt.status,
                        error_class=attempt.failure.value if attempt.failure else None,
                        payload=payload,
                    )
                )
        except IntegrityError:
            return

    @staticmethod
    def _window_payload(window):
        if window is None:
            return None
        return {
            "used_percent": str(window.used_percent),
            "window_duration_minutes": window.window_duration_minutes,
            "resets_at": window.resets_at.isoformat(),
        }
