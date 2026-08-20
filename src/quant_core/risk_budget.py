from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from threading import RLock
from typing import Protocol

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Numeric,
    String,
    Table,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from quant_core.domain import RiskReservation
from quant_core.platform.database import metadata

risk_reservations = Table(
    "risk_reservations",
    metadata,
    Column("reservation_id", String(128), primary_key=True),
    Column("cycle_id", String(128), nullable=False, unique=True),
    Column("intent_id", String(128), nullable=False, unique=True),
    Column("symbol", String(32), nullable=False),
    Column("risk_amount", Numeric(38, 18), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("status", String(32), nullable=False),
    Column("payload", JSON, nullable=False),
)

portfolio_risk_budgets = Table(
    "portfolio_risk_budgets",
    metadata,
    Column("portfolio_id", String(64), primary_key=True),
    Column("reserved_amount", Numeric(38, 18), nullable=False),
    Column("exposure_risk_amount", Numeric(38, 18), nullable=False),
)


@dataclass(frozen=True, slots=True)
class ReservationClaim:
    claimed: bool
    reason_code: str


class RiskBudgetStore(Protocol):
    def reserve(
        self, reservation: RiskReservation, *, maximum_total_risk: Decimal
    ) -> ReservationClaim: ...

    def consume(self, reservation_id: str) -> None: ...

    def release(self, reservation_id: str) -> None: ...


@dataclass(slots=True)
class InMemoryRiskBudgetStore:
    _reservations: dict[str, tuple[RiskReservation, str]] = field(default_factory=dict)
    _lock: RLock = field(default_factory=RLock)

    def reserve(
        self, reservation: RiskReservation, *, maximum_total_risk: Decimal
    ) -> ReservationClaim:
        with self._lock:
            if reservation.reservation_id in self._reservations:
                return ReservationClaim(False, "RESERVATION_ALREADY_EXISTS")
            committed = sum(
                item.risk_amount
                for item, status in self._reservations.values()
                if status in {"ACTIVE", "CONSUMED"}
            )
            if committed + reservation.risk_amount > maximum_total_risk:
                return ReservationClaim(False, "PORTFOLIO_RISK_BUDGET_EXHAUSTED")
            self._reservations[reservation.reservation_id] = (reservation, "ACTIVE")
            return ReservationClaim(True, "RISK_RESERVED")

    def consume(self, reservation_id: str) -> None:
        with self._lock:
            reservation, status = self._reservations[reservation_id]
            if status != "ACTIVE":
                raise ValueError(f"Reservation 状态不能转为 CONSUMED: {status}")
            self._reservations[reservation_id] = (reservation, "CONSUMED")

    def release(self, reservation_id: str) -> None:
        with self._lock:
            reservation, status = self._reservations[reservation_id]
            if status == "RELEASED":
                return
            self._reservations[reservation_id] = (reservation, "RELEASED")

    def status(self, reservation_id: str) -> str | None:
        with self._lock:
            item = self._reservations.get(reservation_id)
            return item[1] if item else None


def bootstrap_risk_budget(engine: Engine, *, portfolio_id: str = "primary") -> None:
    with engine.begin() as connection:
        exists = connection.execute(
            select(portfolio_risk_budgets.c.portfolio_id).where(
                portfolio_risk_budgets.c.portfolio_id == portfolio_id
            )
        ).scalar_one_or_none()
        if exists is None:
            connection.execute(
                insert(portfolio_risk_budgets).values(
                    portfolio_id=portfolio_id,
                    reserved_amount=0,
                    exposure_risk_amount=0,
                )
            )


class SqlRiskBudgetStore:
    """通过组合预算行锁实现跨 Worker 的原子风险占用。"""

    def __init__(self, engine: Engine, *, portfolio_id: str = "primary") -> None:
        self._engine = engine
        self._portfolio_id = portfolio_id

    def reserve(
        self,
        reservation: RiskReservation,
        *,
        maximum_total_risk: Decimal,
    ) -> ReservationClaim:
        try:
            with self._engine.begin() as connection:
                budget = (
                    connection.execute(
                        select(portfolio_risk_budgets)
                        .where(portfolio_risk_budgets.c.portfolio_id == self._portfolio_id)
                        .with_for_update()
                    )
                    .mappings()
                    .one()
                )
                existing = connection.execute(
                    select(risk_reservations.c.status).where(
                        risk_reservations.c.reservation_id == reservation.reservation_id
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    return ReservationClaim(False, "RESERVATION_ALREADY_EXISTS")
                committed = budget["reserved_amount"] + budget["exposure_risk_amount"]
                if committed + reservation.risk_amount > maximum_total_risk:
                    return ReservationClaim(False, "PORTFOLIO_RISK_BUDGET_EXHAUSTED")
                connection.execute(
                    insert(risk_reservations).values(
                        reservation_id=reservation.reservation_id,
                        cycle_id=reservation.cycle_id,
                        intent_id=reservation.intent_id,
                        symbol=reservation.symbol,
                        risk_amount=reservation.risk_amount,
                        expires_at=reservation.expires_at,
                        status="ACTIVE",
                        payload=reservation.model_dump(mode="json"),
                    )
                )
                connection.execute(
                    update(portfolio_risk_budgets)
                    .where(portfolio_risk_budgets.c.portfolio_id == self._portfolio_id)
                    .values(reserved_amount=budget["reserved_amount"] + reservation.risk_amount)
                )
        except IntegrityError:
            return ReservationClaim(False, "RESERVATION_ALREADY_EXISTS")
        return ReservationClaim(True, "RISK_RESERVED")

    def consume(self, reservation_id: str) -> None:
        with self._engine.begin() as connection:
            budget = self._locked_budget(connection)
            reservation = (
                connection.execute(
                    select(risk_reservations)
                    .where(risk_reservations.c.reservation_id == reservation_id)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if reservation["status"] != "ACTIVE":
                raise ValueError(
                    f"Reservation 状态不能转为 CONSUMED: {reservation['status']}"
                )
            amount = reservation["risk_amount"]
            connection.execute(
                update(risk_reservations)
                .where(risk_reservations.c.reservation_id == reservation_id)
                .values(status="CONSUMED")
            )
            connection.execute(
                update(portfolio_risk_budgets)
                .where(portfolio_risk_budgets.c.portfolio_id == self._portfolio_id)
                .values(
                    reserved_amount=budget["reserved_amount"] - amount,
                    exposure_risk_amount=budget["exposure_risk_amount"] + amount,
                )
            )

    def release(self, reservation_id: str) -> None:
        with self._engine.begin() as connection:
            budget = self._locked_budget(connection)
            reservation = (
                connection.execute(
                    select(risk_reservations)
                    .where(risk_reservations.c.reservation_id == reservation_id)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            status = reservation["status"]
            if status == "RELEASED":
                return
            amount = reservation["risk_amount"]
            if status == "ACTIVE":
                budget_values = {"reserved_amount": budget["reserved_amount"] - amount}
            elif status == "CONSUMED":
                budget_values = {
                    "exposure_risk_amount": budget["exposure_risk_amount"] - amount
                }
            else:
                raise ValueError(f"未知 Reservation 状态: {status}")
            connection.execute(
                update(risk_reservations)
                .where(risk_reservations.c.reservation_id == reservation_id)
                .values(status="RELEASED")
            )
            connection.execute(
                update(portfolio_risk_budgets)
                .where(portfolio_risk_budgets.c.portfolio_id == self._portfolio_id)
                .values(**budget_values)
            )

    def _locked_budget(self, connection: Connection):
        return (
            connection.execute(
                select(portfolio_risk_budgets)
                .where(portfolio_risk_budgets.c.portfolio_id == self._portfolio_id)
                .with_for_update()
            )
            .mappings()
            .one()
        )
