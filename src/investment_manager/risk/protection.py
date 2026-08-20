from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from threading import RLock
from typing import Protocol

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Numeric,
    String,
    Table,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine

from investment_manager.execution.models import AccountSnapshot
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.platform.database import metadata
from investment_manager.risk.policy import RiskPolicy

portfolio_protection_states = Table(
    "portfolio_protection_states",
    metadata,
    Column("portfolio_id", String(64), primary_key=True),
    Column("kill_switch_active", Boolean, nullable=False),
    Column("high_water_equity", Numeric(38, 18), nullable=True),
    Column("last_equity", Numeric(38, 18), nullable=True),
    Column("drawdown_fraction", Numeric(38, 18), nullable=False),
    Column("trip_reason", String(128), nullable=True),
    Column("tripped_at", DateTime(timezone=True), nullable=True),
    Column("last_reset_at", DateTime(timezone=True), nullable=True),
    Column("last_reset_reason", String(512), nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=True),
)


def bootstrap_portfolio_protection(
    engine: Engine,
    *,
    portfolio_id: str = "primary",
) -> None:
    with engine.begin() as connection:
        exists = connection.execute(
            select(portfolio_protection_states.c.portfolio_id).where(
                portfolio_protection_states.c.portfolio_id == portfolio_id
            )
        ).scalar_one_or_none()
        if exists is None:
            connection.execute(
                insert(portfolio_protection_states).values(
                    portfolio_id=portfolio_id,
                    kill_switch_active=False,
                    high_water_equity=None,
                    last_equity=None,
                    drawdown_fraction=0,
                    trip_reason=None,
                    tripped_at=None,
                    last_reset_at=None,
                    last_reset_reason=None,
                    updated_at=None,
                )
            )


class PortfolioProtectionState(FrozenModel):
    portfolio_id: str
    kill_switch_active: bool
    high_water_equity: Decimal
    last_equity: Decimal
    drawdown_fraction: Decimal
    trip_reason: str | None = None
    tripped_at: datetime | None = None
    last_reset_at: datetime | None = None
    last_reset_reason: str | None = None
    updated_at: datetime


class PortfolioProtectionStore(Protocol):
    def observe(
        self,
        account: AccountSnapshot,
        *,
        marks: Mapping[str, Decimal],
        as_of: datetime,
    ) -> AccountSnapshot: ...


def marked_equity(account: AccountSnapshot, marks: Mapping[str, Decimal]) -> Decimal:
    missing = sorted({position.symbol for position in account.positions} - marks.keys())
    if missing:
        raise ValueError(f"持仓缺少盯市价格: {','.join(missing)}")
    return account.quote_balance + sum(
        (position.quantity * marks[position.symbol] for position in account.positions),
        Decimal("0"),
    )


def _next_protection(
    *,
    equity: Decimal,
    previous_high_water: Decimal | None,
    initial_equity: Decimal,
    already_active: bool,
    existing_reason: str | None,
    existing_tripped_at: datetime | None,
    account_daily_pnl: Decimal,
    policy: RiskPolicy,
    as_of: datetime,
) -> tuple[Decimal, Decimal, bool, str | None, datetime | None]:
    high_water = max(
        initial_equity if previous_high_water is None else previous_high_water,
        equity,
    )
    drawdown = (
        Decimal("0") if high_water == 0 else max(Decimal("0"), (high_water - equity) / high_water)
    )
    active = already_active
    trip_reason = existing_reason
    tripped_at = existing_tripped_at
    if not active and account_daily_pnl < -policy.maximum_daily_loss:
        active = True
        trip_reason = "DAILY_LOSS_LIMIT_EXCEEDED"
        tripped_at = as_of
    elif not active and drawdown > policy.maximum_drawdown_fraction:
        active = True
        trip_reason = "DRAWDOWN_LIMIT_EXCEEDED"
        tripped_at = as_of
    return high_water, drawdown, active, trip_reason, tripped_at


class InMemoryPortfolioProtectionStore:
    def __init__(self, *, policy: RiskPolicy, initial_equity: Decimal) -> None:
        self._policy = policy
        self._initial_equity = initial_equity
        self._high_water: Decimal | None = None
        self._active = False
        self._trip_reason: str | None = None
        self._tripped_at: datetime | None = None
        self._lock = RLock()

    def observe(
        self,
        account: AccountSnapshot,
        *,
        marks: Mapping[str, Decimal],
        as_of: datetime,
    ) -> AccountSnapshot:
        as_of = require_utc(as_of)
        equity = marked_equity(account, marks)
        with self._lock:
            (
                self._high_water,
                drawdown,
                self._active,
                self._trip_reason,
                self._tripped_at,
            ) = _next_protection(
                equity=equity,
                previous_high_water=self._high_water,
                initial_equity=self._initial_equity,
                already_active=self._active,
                existing_reason=self._trip_reason,
                existing_tripped_at=self._tripped_at,
                account_daily_pnl=account.daily_pnl,
                policy=self._policy,
                as_of=as_of,
            )
            return account.model_copy(
                update={
                    "equity": equity,
                    "equity_high_water": self._high_water,
                    "drawdown_fraction": drawdown,
                    "kill_switch_active": self._active,
                }
            )


class SqlPortfolioProtectionStore:
    """以组合权益高水位驱动持久熔断；状态变化由数据库行锁串行化。"""

    def __init__(
        self,
        engine: Engine,
        *,
        policy: RiskPolicy,
        initial_equity: Decimal,
        portfolio_id: str = "primary",
    ) -> None:
        self._engine = engine
        self._policy = policy
        self._initial_equity = initial_equity
        self._portfolio_id = portfolio_id

    def observe(
        self,
        account: AccountSnapshot,
        *,
        marks: Mapping[str, Decimal],
        as_of: datetime,
    ) -> AccountSnapshot:
        as_of = require_utc(as_of)
        equity = marked_equity(account, marks)
        with self._engine.begin() as connection:
            row = (
                connection.execute(
                    select(portfolio_protection_states)
                    .where(portfolio_protection_states.c.portfolio_id == self._portfolio_id)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            high_water, drawdown, active, trip_reason, tripped_at = _next_protection(
                equity=equity,
                previous_high_water=row["high_water_equity"],
                initial_equity=self._initial_equity,
                already_active=bool(row["kill_switch_active"]),
                existing_reason=row["trip_reason"],
                existing_tripped_at=row["tripped_at"],
                account_daily_pnl=account.daily_pnl,
                policy=self._policy,
                as_of=as_of,
            )
            connection.execute(
                update(portfolio_protection_states)
                .where(portfolio_protection_states.c.portfolio_id == self._portfolio_id)
                .values(
                    kill_switch_active=active,
                    high_water_equity=high_water,
                    last_equity=equity,
                    drawdown_fraction=drawdown,
                    trip_reason=trip_reason,
                    tripped_at=tripped_at,
                    updated_at=as_of,
                )
            )
        return account.model_copy(
            update={
                "equity": equity,
                "equity_high_water": high_water,
                "drawdown_fraction": drawdown,
                "kill_switch_active": active,
            }
        )

    def current(self) -> PortfolioProtectionState | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(portfolio_protection_states).where(
                        portfolio_protection_states.c.portfolio_id == self._portfolio_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None or row["last_equity"] is None or row["updated_at"] is None:
            return None
        return PortfolioProtectionState.model_validate(dict(row))

    def reset(self, *, reset_at: datetime, reason: str) -> PortfolioProtectionState:
        reset_at = require_utc(reset_at)
        reason = reason.strip()
        if not reason:
            raise ValueError("人工恢复必须提供原因")
        with self._engine.begin() as connection:
            row = (
                connection.execute(
                    select(portfolio_protection_states)
                    .where(portfolio_protection_states.c.portfolio_id == self._portfolio_id)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if row["last_equity"] is None:
                raise ValueError("账户保护状态尚未完成首次权益观测")
            connection.execute(
                update(portfolio_protection_states)
                .where(portfolio_protection_states.c.portfolio_id == self._portfolio_id)
                .values(
                    kill_switch_active=False,
                    high_water_equity=row["last_equity"],
                    drawdown_fraction=0,
                    trip_reason=None,
                    tripped_at=None,
                    last_reset_at=reset_at,
                    last_reset_reason=reason,
                    updated_at=reset_at,
                )
            )
        current = self.current()
        assert current is not None
        return current
