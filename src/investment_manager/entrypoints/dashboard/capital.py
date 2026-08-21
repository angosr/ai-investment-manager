"""Read-only projection of the active product-capital ledger for the dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from investment_manager.execution.group.models import ExecutionGroup
from investment_manager.execution.planning.planner import TradePlan
from investment_manager.execution.tables import (
    execution_groups,
    mock_product_orders,
    trade_plans,
)
from investment_manager.forecast.tables import forecasts
from investment_manager.kernel.time import require_utc
from investment_manager.platform.time import database_utc
from investment_manager.portfolio.models import (
    PortfolioAccountSnapshot,
    PortfolioTarget,
)
from investment_manager.portfolio.tables import (
    portfolio_account_snapshots,
    portfolio_targets,
)
from investment_manager.risk.portfolio import PortfolioRiskDecision
from investment_manager.risk.tables import portfolio_risk_decisions
from investment_manager.settings import AppConfig


@dataclass(frozen=True, slots=True)
class CapitalOverview:
    enabled: bool
    account: PortfolioAccountSnapshot | None = None
    target: PortfolioTarget | None = None
    risk: PortfolioRiskDecision | None = None
    plan: TradePlan | None = None
    active_groups: tuple[ExecutionGroup, ...] = ()
    base_forecast_count: int = 0
    calibrated_forecast_count: int = 0
    latest_forecast_available_at: datetime | None = None
    latest_forecast_valid_until: datetime | None = None
    total_order_count: int = 0
    entry_window_start: datetime | None = None
    entry_window_end: datetime | None = None


class CapitalDashboardReader:
    """Load a compact current-state view without inventing a second ledger."""

    def __init__(self, engine: Engine, config: AppConfig) -> None:
        self._engine = engine
        self._config = config

    def overview(self, *, now: datetime) -> CapitalOverview:
        now = require_utc(now)
        if not self._config.capital.enabled:
            return CapitalOverview(enabled=False)
        with self._engine.connect() as connection:
            account = self._latest_payload(
                connection,
                portfolio_account_snapshots.c.payload,
                portfolio_account_snapshots.c.as_of,
                portfolio_account_snapshots.c.snapshot_id,
                PortfolioAccountSnapshot,
                secondary_order=portfolio_account_snapshots.c.revision,
            )
            target = self._latest_payload(
                connection,
                portfolio_targets.c.payload,
                portfolio_targets.c.as_of,
                portfolio_targets.c.target_id,
                PortfolioTarget,
            )
            risk = None
            if target is not None:
                risk = self._payload_for(
                    connection,
                    select(portfolio_risk_decisions.c.payload).where(
                        portfolio_risk_decisions.c.target_id == target.target_id
                    ),
                    PortfolioRiskDecision,
                )
            plan = None
            if risk is not None and risk.approved_target is not None:
                plan = self._payload_for(
                    connection,
                    select(trade_plans.c.payload).where(
                        trade_plans.c.approved_target_id
                        == risk.approved_target.approved_target_id
                    ),
                    TradePlan,
                )
            active = tuple(
                ExecutionGroup.model_validate(payload)
                for payload in connection.execute(
                    select(execution_groups.c.payload)
                    .where(execution_groups.c.terminal.is_(False))
                    .order_by(
                        execution_groups.c.updated_at,
                        execution_groups.c.group_id,
                    )
                ).scalars()
            )
            forecast_counts = {
                kind: int(count)
                for kind, count in connection.execute(
                    select(forecasts.c.kind, func.count()).group_by(forecasts.c.kind)
                )
            }
            latest_forecast = connection.execute(
                select(forecasts.c.available_at, forecasts.c.valid_until)
                .order_by(
                    forecasts.c.available_at.desc(),
                    forecasts.c.forecast_id.desc(),
                )
                .limit(1)
            ).one_or_none()
            order_count = int(
                connection.scalar(select(func.count()).select_from(mock_product_orders))
                or 0
            )
        window_start, window_end = self._entry_window(now)
        return CapitalOverview(
            enabled=True,
            account=account,
            target=target,
            risk=risk,
            plan=plan,
            active_groups=active,
            base_forecast_count=forecast_counts.get("BASE", 0),
            calibrated_forecast_count=forecast_counts.get("CALIBRATED", 0),
            latest_forecast_available_at=(
                database_utc(latest_forecast.available_at)
                if latest_forecast is not None
                else None
            ),
            latest_forecast_valid_until=(
                database_utc(latest_forecast.valid_until)
                if latest_forecast is not None
                else None
            ),
            total_order_count=order_count,
            entry_window_start=window_start,
            entry_window_end=window_end,
        )

    @staticmethod
    def _latest_payload(
        connection,
        payload_column,
        time_column,
        id_column,
        model,
        *,
        secondary_order=None,
    ):
        ordering = [time_column.desc()]
        if secondary_order is not None:
            ordering.append(secondary_order.desc())
        ordering.append(id_column.desc())
        payload = connection.execute(
            select(payload_column)
            .order_by(*ordering)
            .limit(1)
        ).scalar_one_or_none()
        return None if payload is None else model.model_validate(payload)

    @staticmethod
    def _payload_for(connection, statement, model):
        payload = connection.execute(statement).scalar_one_or_none()
        return None if payload is None else model.model_validate(payload)

    def _entry_window(self, now: datetime) -> tuple[datetime, datetime]:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start + self._entry_delay
        if now <= end:
            return start, end
        if start.month == 12:
            start = start.replace(year=start.year + 1, month=1)
        else:
            start = start.replace(month=start.month + 1)
        return start, start + self._entry_delay

    @property
    def _entry_delay(self) -> timedelta:
        return timedelta(
            minutes=self._config.carry_forecast.maximum_monthly_entry_delay_minutes
        )


def serialize_capital_overview(overview: CapitalOverview) -> dict:
    account = overview.account
    target = overview.target
    risk = overview.risk
    plan = overview.plan
    return {
        "enabled": overview.enabled,
        "entry_window": {
            "start": _iso(overview.entry_window_start),
            "end": _iso(overview.entry_window_end),
        },
        "account": None
        if account is None
        else {
            "as_of": _iso(account.as_of),
            "cash_balance": str(account.cash_balance),
            "equity": str(account.equity),
            "daily_pnl": str(account.daily_pnl),
            "drawdown_fraction": str(account.drawdown_fraction),
            "reconciled": account.reconciled,
            "kill_switch_active": account.kill_switch_active,
            "positions": [
                {
                    "instrument": item.instrument.key,
                    "quantity": str(item.quantity),
                    "average_price": str(item.average_price),
                }
                for item in account.positions
            ],
        },
        "decision": {
            "as_of": _iso(target.as_of if target is not None else None),
            "reason_codes": list(target.reason_codes) if target is not None else [],
            "target_sleeve_count": len(target.sleeves) if target is not None else 0,
            "risk_outcome": risk.outcome.value if risk is not None else None,
            "plan_group_count": len(plan.groups) if plan is not None else 0,
            "plan_omission_count": len(plan.omissions) if plan is not None else 0,
        },
        "execution": {
            "active_group_count": len(overview.active_groups),
            "active_groups": [
                {
                    "group_id": item.group_id,
                    "status": item.status.value,
                    "updated_at": _iso(item.updated_at),
                    "unhedged_notional": str(item.unhedged_notional),
                }
                for item in overview.active_groups
            ],
            "total_order_count": overview.total_order_count,
        },
        "forecast": {
            "base_count": overview.base_forecast_count,
            "calibrated_count": overview.calibrated_forecast_count,
            "latest_available_at": _iso(overview.latest_forecast_available_at),
            "latest_valid_until": _iso(overview.latest_forecast_valid_until),
        },
    }


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()
