from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from threading import RLock
from typing import Protocol

from quant_core.domain import RiskReservation


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
