"""Optimistic durable store for recoverable execution groups."""

from __future__ import annotations

from typing import Protocol

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.execution.group.models import (
    ExecutionGroup,
    ExecutionLeg,
    new_execution_group,
)
from investment_manager.execution.planning.planner import TradePlan
from investment_manager.execution.tables import execution_groups, trade_plans


class ConcurrentExecutionUpdate(RuntimeError):
    pass


class ExecutionGroupStore(Protocol):
    def record(self, group: ExecutionGroup) -> bool: ...

    def group(self, group_id: str) -> ExecutionGroup | None: ...

    def save(self, group: ExecutionGroup, *, expected_revision: int) -> bool: ...


class SqlExecutionGroupStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(self, group: ExecutionGroup) -> bool:
        if group.revision != 0:
            raise ValueError("新 ExecutionGroup revision 必须为 0")
        self._validate_initial_group(group)
        try:
            with self._engine.begin() as connection:
                connection.execute(insert(execution_groups).values(**self._values(group)))
            return True
        except IntegrityError:
            existing = self.group(group.group_id)
            if existing is not None:
                if existing != group:
                    raise ValueError("ExecutionGroup identity 已存在且内容不同") from None
                return False
            active = self.active_for_sleeve(group.sleeve_id)
            if active is not None:
                raise ValueError("相同 Sleeve 已有未终结 ExecutionGroup") from None
            raise

    def group(self, group_id: str) -> ExecutionGroup | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(execution_groups.c.payload).where(execution_groups.c.group_id == group_id)
            ).scalar_one_or_none()
        return None if payload is None else ExecutionGroup.model_validate(payload)

    def active_for_sleeve(self, sleeve_id: str) -> ExecutionGroup | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(execution_groups.c.payload).where(
                    execution_groups.c.sleeve_id == sleeve_id,
                    execution_groups.c.terminal.is_(False),
                )
            ).scalar_one_or_none()
        return None if payload is None else ExecutionGroup.model_validate(payload)

    def save(self, group: ExecutionGroup, *, expected_revision: int) -> bool:
        if group.revision != expected_revision + 1:
            raise ValueError("ExecutionGroup save 必须恰好推进一个 revision")
        current = self.group(group.group_id)
        if current is None:
            raise ValueError("ExecutionGroup 尚未持久化")
        if current == group:
            return False
        self._validate_transition(current, group)
        with self._engine.begin() as connection:
            result = connection.execute(
                update(execution_groups)
                .where(
                    execution_groups.c.group_id == group.group_id,
                    execution_groups.c.revision == expected_revision,
                )
                .values(**self._values(group))
            )
        if result.rowcount == 1:
            return True
        existing = self.group(group.group_id)
        if existing == group:
            return False
        raise ConcurrentExecutionUpdate("ExecutionGroup 被并发推进，请重新对账")

    def _validate_initial_group(self, group: ExecutionGroup) -> None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(trade_plans.c.payload).where(trade_plans.c.plan_id == group.plan_id)
            ).scalar_one_or_none()
        if payload is None:
            raise ValueError("ExecutionGroup 缺少权威 TradePlan")
        plan = TradePlan.model_validate(payload)
        planned = next(
            (item for item in plan.groups if item.group_id == group.group_id),
            None,
        )
        if planned is None:
            raise ValueError("ExecutionGroup 不属于指定 TradePlan")
        expected = new_execution_group(
            plan_id=plan.plan_id,
            planned=planned,
            started_at=group.started_at,
        )
        if expected != group:
            raise ValueError("ExecutionGroup 初态与冻结 TradePlan 不一致")

    @staticmethod
    def _validate_transition(current: ExecutionGroup, updated: ExecutionGroup) -> None:
        immutable = (
            "group_id",
            "plan_id",
            "approved_target_id",
            "cycle_id",
            "sleeve_id",
            "valid_until",
            "maximum_unhedged_notional",
            "maximum_unhedged_seconds",
            "started_at",
        )
        if any(getattr(current, name) != getattr(updated, name) for name in immutable):
            raise ValueError("ExecutionGroup 冻结合同不得变更")
        if updated.updated_at < current.updated_at:
            raise ValueError("ExecutionGroup 时间不能倒退")
        if tuple(_leg_contract(item) for item in current.target_legs) != tuple(
            _leg_contract(item) for item in updated.target_legs
        ):
            raise ValueError("ExecutionGroup Target Leg 合同不得变更")
        updated_compensations = {
            item.execution_leg_id: _leg_contract(item) for item in updated.compensation_legs
        }
        if any(
            updated_compensations.get(item.execution_leg_id) != _leg_contract(item)
            for item in current.compensation_legs
        ):
            raise ValueError("ExecutionGroup 既有 Compensation Leg 合同不得变更")
        allowed = {
            "EXECUTING": {"EXECUTING", "RECOVERING", "COMPENSATING", "HEDGED"},
            "RECOVERING": {"EXECUTING", "RECOVERING", "COMPENSATING", "HEDGED"},
            "COMPENSATING": {"COMPENSATING", "FLAT"},
            "HEDGED": set(),
            "FLAT": set(),
        }
        if updated.status.value not in allowed[current.status.value]:
            raise ValueError(f"非法 ExecutionGroup 状态迁移: {current.status} -> {updated.status}")

    @staticmethod
    def _values(group: ExecutionGroup) -> dict[str, object]:
        return {
            "group_id": group.group_id,
            "plan_id": group.plan_id,
            "cycle_id": group.cycle_id,
            "sleeve_id": group.sleeve_id,
            "status": group.status.value,
            "terminal": group.terminal,
            "revision": group.revision,
            "started_at": group.started_at,
            "updated_at": group.updated_at,
            "payload": group.model_dump(mode="json"),
        }


def _leg_contract(leg: ExecutionLeg) -> tuple[object, ...]:
    return (
        leg.execution_leg_id,
        leg.planned_leg_id,
        leg.group_id,
        leg.role,
        leg.attempt,
        leg.client_order_id,
        leg.instrument,
        leg.side,
        leg.requested_quantity,
        leg.reference_price,
    )
