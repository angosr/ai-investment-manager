"""Immutable grouped TradePlan handoff ledger."""

from __future__ import annotations

from typing import Protocol

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.execution.planning.planner import TradePlan
from investment_manager.execution.tables import trade_plans
from investment_manager.risk.tables import portfolio_risk_decisions


class TradePlanStore(Protocol):
    def record(self, plan: TradePlan) -> bool: ...


class SqlTradePlanStore:
    """Immutable handoff ledger from Risk authorization to grouped Execution."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(self, plan: TradePlan) -> bool:
        try:
            with self._engine.begin() as connection:
                approved_target_id = connection.execute(
                    select(portfolio_risk_decisions.c.approved_target_id).where(
                        portfolio_risk_decisions.c.approved_target_id
                        == plan.approved_target_id
                    )
                ).scalar_one_or_none()
                if approved_target_id != plan.approved_target_id:
                    raise ValueError("TradePlan 缺少匹配的权威 Risk 授权")
                connection.execute(
                    insert(trade_plans).values(
                        plan_id=plan.plan_id,
                        approved_target_id=plan.approved_target_id,
                        cycle_id=plan.cycle_id,
                        created_at=plan.created_at,
                        plan_hash=plan.plan_hash,
                        payload=plan.model_dump(mode="json"),
                    )
                )
            return True
        except IntegrityError:
            existing = self.for_approved_target(plan.approved_target_id)
            if existing != plan:
                raise ValueError("Risk 授权已存在且 TradePlan 内容不同") from None
            return False

    def plan(self, plan_id: str) -> TradePlan | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(trade_plans.c.payload).where(trade_plans.c.plan_id == plan_id)
            ).scalar_one_or_none()
        return None if payload is None else TradePlan.model_validate(payload)

    def for_approved_target(self, approved_target_id: str) -> TradePlan | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(trade_plans.c.payload).where(
                    trade_plans.c.approved_target_id == approved_target_id
                )
            ).scalar_one_or_none()
        return None if payload is None else TradePlan.model_validate(payload)
