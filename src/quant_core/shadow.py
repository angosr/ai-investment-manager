from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import case, func, select
from sqlalchemy.engine import Engine

from quant_core.domain import AccountSnapshot, _require_utc
from quant_core.persistence import account_snapshots, analysis_cycles, market_snapshots, orders
from quant_core.reconciliation import ReconciliationStatus
from quant_core.reconciliation_sql import SqlReconciliationReportStore


class ShadowStateReader(Protocol):
    def account_for_cycle(
        self,
        *,
        cycle_id: str,
        as_of: datetime,
        initial_quote_balance,
    ) -> AccountSnapshot: ...

    def last_cycle_at(self, *, symbol: str, as_of: datetime) -> datetime | None: ...

    def entry_orders_today(self, *, as_of: datetime) -> int: ...


class SqlShadowStateReader:
    """从不可变业务事实投影 Shadow 账户，不维护第二份可变账户账本。"""

    def __init__(
        self,
        engine: Engine,
        *,
        maximum_reconciliation_age_seconds: int | None = None,
    ) -> None:
        self._engine = engine
        self._maximum_reconciliation_age_seconds = maximum_reconciliation_age_seconds
        self._reconciliation_reports = SqlReconciliationReportStore(engine)

    def account_for_cycle(
        self,
        *,
        cycle_id: str,
        as_of: datetime,
        initial_quote_balance,
    ) -> AccountSnapshot:
        as_of = _require_utc(as_of)
        if self._maximum_reconciliation_age_seconds is not None:
            report = self._reconciliation_reports.latest(as_of=as_of)
            if report is not None:
                authoritative = report.authoritative_account
                fresh = (
                    as_of - report.as_of
                ).total_seconds() <= self._maximum_reconciliation_age_seconds
                daily_pnl = (
                    authoritative.daily_pnl if authoritative.as_of.date() == as_of.date() else 0
                )
                return authoritative.model_copy(
                    update={
                        "cycle_id": cycle_id,
                        "as_of": as_of,
                        "observed_at": report.as_of,
                        "daily_pnl": daily_pnl,
                        "reconciled": (fresh and report.status == ReconciliationStatus.MATCHED),
                    }
                )
        phase_priority = case(
            (account_snapshots.c.phase == "POST_EXIT", 3),
            (account_snapshots.c.phase == "POST_EXECUTION", 2),
            else_=1,
        )
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(account_snapshots.c.payload)
                .where(account_snapshots.c.as_of <= as_of)
                .order_by(account_snapshots.c.as_of.desc(), phase_priority.desc())
                .limit(1)
            ).scalar_one_or_none()
        if payload is None:
            return AccountSnapshot(
                cycle_id=cycle_id,
                as_of=as_of,
                observed_at=as_of,
                quote_balance=initial_quote_balance,
                reconciled=self._maximum_reconciliation_age_seconds is None,
            )
        previous = AccountSnapshot.model_validate(payload)
        daily_pnl = previous.daily_pnl if previous.as_of.date() == as_of.date() else 0
        return previous.model_copy(
            update={
                "cycle_id": cycle_id,
                "as_of": as_of,
                "observed_at": as_of,
                "daily_pnl": daily_pnl,
                "reconciled": (
                    previous.reconciled and self._maximum_reconciliation_age_seconds is None
                ),
            }
        )

    def last_cycle_at(self, *, symbol: str, as_of: datetime) -> datetime | None:
        as_of = _require_utc(as_of)
        with self._engine.connect() as connection:
            value = connection.execute(
                select(func.max(analysis_cycles.c.as_of))
                .join(
                    market_snapshots,
                    market_snapshots.c.cycle_id == analysis_cycles.c.cycle_id,
                )
                .where(
                    market_snapshots.c.symbol == symbol,
                    analysis_cycles.c.as_of <= as_of,
                )
            ).scalar_one_or_none()
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value

    def entry_orders_today(self, *, as_of: datetime) -> int:
        as_of = _require_utc(as_of)
        day_start = as_of.replace(hour=0, minute=0, second=0, microsecond=0)
        with self._engine.connect() as connection:
            return int(
                connection.execute(
                    select(func.count())
                    .select_from(
                        orders.join(
                            analysis_cycles,
                            analysis_cycles.c.cycle_id == orders.c.cycle_id,
                        )
                    )
                    .where(
                        orders.c.role == "ENTRY",
                        analysis_cycles.c.as_of >= day_start,
                        analysis_cycles.c.as_of <= as_of,
                    )
                ).scalar_one()
            )
