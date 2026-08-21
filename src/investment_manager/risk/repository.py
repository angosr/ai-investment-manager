from __future__ import annotations

from typing import Protocol

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.kernel.identity import content_hash
from investment_manager.portfolio.tables import portfolio_targets
from investment_manager.risk.portfolio import PortfolioRiskDecision
from investment_manager.risk.tables import portfolio_risk_decisions


class PortfolioRiskStore(Protocol):
    def record(self, decision: PortfolioRiskDecision) -> bool: ...

    def for_approved_targets(
        self,
        approved_target_ids: tuple[str, ...],
    ) -> dict[str, PortfolioRiskDecision]: ...


class SqlPortfolioRiskStore:
    """Immutable authorization ledger bound to one persisted PortfolioTarget."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(self, decision: PortfolioRiskDecision) -> bool:
        try:
            with self._engine.begin() as connection:
                target_hash = connection.execute(
                    select(portfolio_targets.c.target_hash).where(
                        portfolio_targets.c.target_id == decision.target_id
                    )
                ).scalar_one_or_none()
                if target_hash != decision.target_hash:
                    raise ValueError("RiskDecision 缺少匹配的权威 PortfolioTarget")
                connection.execute(
                    insert(portfolio_risk_decisions).values(
                        decision_id=decision.decision_id,
                        target_id=decision.target_id,
                        approved_target_id=(
                            decision.approved_target.approved_target_id
                            if decision.approved_target is not None
                            else None
                        ),
                        outcome=decision.outcome.value,
                        decided_at=decision.decided_at,
                        decision_hash=content_hash(decision),
                        payload=decision.model_dump(mode="json"),
                    )
                )
            return True
        except IntegrityError:
            existing = self.for_target(decision.target_id)
            if existing != decision:
                raise ValueError("Risk target 已存在且决策内容不同") from None
            return False

    def decision(self, decision_id: str) -> PortfolioRiskDecision | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(portfolio_risk_decisions.c.payload).where(
                    portfolio_risk_decisions.c.decision_id == decision_id
                )
            ).scalar_one_or_none()
        return None if payload is None else PortfolioRiskDecision.model_validate(payload)

    def for_target(self, target_id: str) -> PortfolioRiskDecision | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(portfolio_risk_decisions.c.payload).where(
                    portfolio_risk_decisions.c.target_id == target_id
                )
            ).scalar_one_or_none()
        return None if payload is None else PortfolioRiskDecision.model_validate(payload)

    def for_approved_targets(
        self,
        approved_target_ids: tuple[str, ...],
    ) -> dict[str, PortfolioRiskDecision]:
        approved_target_ids = tuple(sorted(set(approved_target_ids)))
        if not approved_target_ids:
            return {}
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(
                    portfolio_risk_decisions.c.approved_target_id,
                    portfolio_risk_decisions.c.payload,
                ).where(portfolio_risk_decisions.c.approved_target_id.in_(approved_target_ids))
            ).all()
        return {
            row.approved_target_id: PortfolioRiskDecision.model_validate(row.payload)
            for row in rows
        }
