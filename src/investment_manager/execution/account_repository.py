from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import case, select
from sqlalchemy.engine import Connection, Engine

from investment_manager.execution.models import AccountSnapshot
from investment_manager.execution.reconciliation.engine import (
    ReconciliationReport,
    ReconciliationStatus,
)
from investment_manager.execution.tables import account_snapshots
from investment_manager.kernel.time import require_utc


class ReconciliationReportReader(Protocol):
    def latest(self, *, as_of: datetime) -> ReconciliationReport | None: ...


class AccountSnapshotReader(Protocol):
    def account_for_cycle(
        self,
        *,
        cycle_id: str,
        as_of: datetime,
        initial_quote_balance: Decimal,
    ) -> AccountSnapshot: ...


def latest_account_snapshot_payload(
    connection: Connection,
    *,
    as_of: datetime,
):
    """Return the one authoritative account projection visible at ``as_of``."""

    phase_priority = case(
        (account_snapshots.c.phase == "POST_EXIT", 3),
        (account_snapshots.c.phase == "POST_EXECUTION", 2),
        else_=1,
    )
    return connection.execute(
        select(account_snapshots.c.payload)
        .where(account_snapshots.c.as_of <= as_of)
        .order_by(
            account_snapshots.c.as_of.desc(),
            phase_priority.desc(),
            account_snapshots.c.snapshot_id.desc(),
        )
        .limit(1)
    ).scalar_one_or_none()


class SqlAccountSnapshotReader:
    """Project the current account from immutable execution/reconciliation facts."""

    def __init__(
        self,
        engine: Engine,
        *,
        maximum_reconciliation_age_seconds: int | None = None,
        reports: ReconciliationReportReader | None = None,
    ) -> None:
        if maximum_reconciliation_age_seconds is not None and reports is None:
            raise ValueError("启用对账新鲜度约束时必须注入 ReconciliationReportReader")
        self._engine = engine
        self._maximum_reconciliation_age_seconds = maximum_reconciliation_age_seconds
        self._reports = reports

    def account_for_cycle(
        self,
        *,
        cycle_id: str,
        as_of: datetime,
        initial_quote_balance: Decimal,
    ) -> AccountSnapshot:
        as_of = require_utc(as_of)
        if self._maximum_reconciliation_age_seconds is not None:
            assert self._reports is not None
            report = self._reports.latest(as_of=as_of)
            if report is not None:
                authoritative = report.authoritative_account
                fresh = (
                    as_of - report.as_of
                ).total_seconds() <= self._maximum_reconciliation_age_seconds
                daily_pnl = (
                    authoritative.daily_pnl
                    if authoritative.as_of.date() == as_of.date()
                    else Decimal("0")
                )
                return authoritative.model_copy(
                    update={
                        "cycle_id": cycle_id,
                        "as_of": as_of,
                        "observed_at": report.as_of,
                        "daily_pnl": daily_pnl,
                        "reconciled": (
                            fresh and report.status == ReconciliationStatus.MATCHED
                        ),
                    }
                )
        with self._engine.connect() as connection:
            payload = latest_account_snapshot_payload(connection, as_of=as_of)
        if payload is None:
            return AccountSnapshot(
                cycle_id=cycle_id,
                as_of=as_of,
                observed_at=as_of,
                quote_balance=initial_quote_balance,
                reconciled=self._maximum_reconciliation_age_seconds is None,
            )
        previous = AccountSnapshot.model_validate(payload)
        daily_pnl = (
            previous.daily_pnl
            if previous.as_of.date() == as_of.date()
            else Decimal("0")
        )
        return previous.model_copy(
            update={
                "cycle_id": cycle_id,
                "as_of": as_of,
                "observed_at": as_of,
                "daily_pnl": daily_pnl,
                "reconciled": (
                    previous.reconciled
                    and self._maximum_reconciliation_age_seconds is None
                ),
            }
        )
