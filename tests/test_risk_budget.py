from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from investment_manager.domain import RiskReservation
from investment_manager.risk_budget import InMemoryRiskBudgetStore


def _reservation(identifier: str, risk_amount: str) -> RiskReservation:
    return RiskReservation(
        reservation_id=identifier,
        cycle_id=f"cycle-{identifier}",
        intent_id=f"intent-{identifier}",
        symbol="BTCUSDT",
        risk_amount=Decimal(risk_amount),
        quantity=Decimal("1"),
        expires_at=datetime.now(tz=UTC) + timedelta(minutes=1),
    )


def test_concurrent_reservations_cannot_overspend_portfolio_budget() -> None:
    store = InMemoryRiskBudgetStore()
    reservations = (_reservation("a", "60"), _reservation("b", "60"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(
                lambda item: store.reserve(item, maximum_total_risk=Decimal("100")),
                reservations,
            )
        )

    assert sum(result.claimed for result in results) == 1
    assert {result.reason_code for result in results} == {
        "RISK_RESERVED",
        "PORTFOLIO_RISK_BUDGET_EXHAUSTED",
    }


def test_consumed_risk_remains_committed_until_reconciliation_releases_it() -> None:
    store = InMemoryRiskBudgetStore()
    first = _reservation("a", "60")
    second = _reservation("b", "60")

    assert store.reserve(first, maximum_total_risk=Decimal("100")).claimed
    store.consume(first.reservation_id)
    denied = store.reserve(second, maximum_total_risk=Decimal("100"))
    assert denied.claimed is False

    store.release(first.reservation_id)
    assert store.reserve(second, maximum_total_risk=Decimal("100")).claimed
