from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.execution.account_repository import (
    latest_account_snapshot_payload,
)
from investment_manager.execution.models import (
    AccountSnapshot,
    Order,
)
from investment_manager.execution.reconciliation import (
    MockReconciler,
    ReconciliationReport,
    TradingStateSnapshot,
)
from investment_manager.execution.tables import (
    mock_exchange_orders,
    orders,
    reconciliation_reports,
)
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.legacy.repository import analysis_cycles


class SqlLocalTradingStateSource:
    def __init__(
        self,
        engine: Engine,
        *,
        initial_quote_balance,
        bootstrap_from_reconciliation: bool = False,
    ) -> None:
        self._engine = engine
        self._initial_quote_balance = initial_quote_balance
        self._bootstrap_from_reconciliation = bootstrap_from_reconciliation
        self._reports = SqlReconciliationReportStore(engine)

    def snapshot(self, *, as_of: datetime) -> TradingStateSnapshot:
        as_of = require_utc(as_of)
        with self._engine.connect() as connection:
            account_payload = latest_account_snapshot_payload(connection, as_of=as_of)
            order_payloads = tuple(
                connection.execute(
                    select(orders.c.payload)
                    .join(
                        analysis_cycles,
                        analysis_cycles.c.cycle_id == orders.c.cycle_id,
                    )
                    .where(analysis_cycles.c.as_of <= as_of)
                    .order_by(analysis_cycles.c.as_of, orders.c.client_order_id)
                ).scalars()
            )
        if account_payload is not None:
            account = AccountSnapshot.model_validate(account_payload)
        elif (
            self._bootstrap_from_reconciliation
            and (previous_report := self._reports.latest(as_of=as_of)) is not None
        ):
            account = previous_report.authoritative_account.model_copy(
                update={
                    "cycle_id": stable_id("reconciliation_account", as_of.isoformat()),
                    "as_of": as_of,
                    "observed_at": previous_report.as_of,
                    "reconciled": True,
                }
            )
        else:
            account = AccountSnapshot(
                cycle_id=stable_id("reconciliation_account", as_of.isoformat()),
                as_of=as_of,
                observed_at=as_of,
                quote_balance=self._initial_quote_balance,
                reconciled=True,
            )
        local_orders = tuple(Order.model_validate(item) for item in order_payloads)
        payload = {
            "as_of": as_of.isoformat(),
            "account": account.model_dump(mode="json"),
            "orders": [item.model_dump(mode="json") for item in local_orders],
        }
        return TradingStateSnapshot(
            snapshot_id=stable_id("local_trading_state", content_hash(payload)),
            as_of=as_of,
            observed_at=as_of,
            account=account,
            orders=local_orders,
        )


class SqlMockExchangeTruthSource:
    def __init__(self, engine: Engine, *, initial_quote_balance) -> None:
        self._engine = engine
        self._initial_quote_balance = initial_quote_balance

    def snapshot(self, *, as_of: datetime) -> TradingStateSnapshot:
        as_of = require_utc(as_of)
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(
                    mock_exchange_orders.c.observed_at,
                    mock_exchange_orders.c.payload,
                )
                .where(mock_exchange_orders.c.observed_at <= as_of)
                .order_by(
                    mock_exchange_orders.c.observed_at,
                    mock_exchange_orders.c.client_order_id,
                )
            ).all()
        authoritative_orders = tuple(Order.model_validate(payload) for _, payload in rows)
        account = AccountSnapshot(
            cycle_id=stable_id("mock_exchange_account", as_of.isoformat()),
            as_of=as_of,
            observed_at=as_of,
            quote_balance=self._initial_quote_balance,
            reconciled=True,
        )
        reconciler = MockReconciler()
        for order in authoritative_orders:
            event_time = max(fill.event_time for fill in order.fills) if order.fills else as_of
            if account.as_of.date() != event_time.date():
                account = account.model_copy(update={"daily_pnl": Decimal("0")})
            account = reconciler.apply(account, order)
        daily_pnl = account.daily_pnl if account.as_of.date() == as_of.date() else Decimal("0")
        account = account.model_copy(
            update={
                "cycle_id": stable_id("mock_exchange_account", as_of.isoformat()),
                "as_of": as_of,
                "observed_at": as_of,
                "daily_pnl": daily_pnl,
                "reconciled": True,
            }
        )
        payload = {
            "as_of": as_of.isoformat(),
            "account": account.model_dump(mode="json"),
            "orders": [item.model_dump(mode="json") for item in authoritative_orders],
        }
        return TradingStateSnapshot(
            snapshot_id=stable_id("remote_trading_state", content_hash(payload)),
            as_of=as_of,
            observed_at=as_of,
            account=account,
            orders=authoritative_orders,
        )


class SqlReconciliationReportStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(self, report: ReconciliationReport) -> ReconciliationReport:
        payload = report.model_dump(mode="json")
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(reconciliation_reports).values(
                        report_id=report.report_id,
                        as_of=report.as_of,
                        policy_version=report.policy_version,
                        status=report.status.value,
                        freeze_new_risk=report.freeze_new_risk,
                        payload=payload,
                    )
                )
        except IntegrityError:
            with self._engine.connect() as connection:
                existing = connection.execute(
                    select(reconciliation_reports.c.payload).where(
                        reconciliation_reports.c.report_id == report.report_id
                    )
                ).scalar_one()
            if ReconciliationReport.model_validate(existing) != report:
                raise ValueError("相同 report_id 的对账事实不一致") from None
        return report

    def latest(self, *, as_of: datetime) -> ReconciliationReport | None:
        as_of = require_utc(as_of)
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(reconciliation_reports.c.payload)
                .where(reconciliation_reports.c.as_of <= as_of)
                .order_by(
                    reconciliation_reports.c.as_of.desc(),
                    reconciliation_reports.c.report_id.desc(),
                )
                .limit(1)
            ).scalar_one_or_none()
        return ReconciliationReport.model_validate(payload) if payload is not None else None
