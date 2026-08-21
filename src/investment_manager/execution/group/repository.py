"""Optimistic durable store for recoverable execution groups."""

from __future__ import annotations

from datetime import datetime
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
from investment_manager.kernel.time import require_utc


class ConcurrentExecutionUpdate(RuntimeError):
    pass


class ExecutionGroupStore(Protocol):
    def record(self, group: ExecutionGroup) -> bool: ...

    def group(self, group_id: str) -> ExecutionGroup | None: ...

    def visible(self, *, as_of: datetime) -> tuple[ExecutionGroup, ...]: ...

    def for_plan(self, plan_id: str) -> tuple[ExecutionGroup, ...]: ...

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

    def visible(self, *, as_of: datetime) -> tuple[ExecutionGroup, ...]:
        as_of = require_utc(as_of)
        with self._engine.connect() as connection:
            payloads = connection.execute(
                select(execution_groups.c.payload)
                .where(execution_groups.c.started_at <= as_of)
                .order_by(
                    execution_groups.c.started_at,
                    execution_groups.c.group_id,
                )
            ).scalars()
            return tuple(ExecutionGroup.model_validate(item) for item in payloads)

    def for_plan(self, plan_id: str) -> tuple[ExecutionGroup, ...]:
        with self._engine.connect() as connection:
            payloads = connection.execute(
                select(execution_groups.c.payload)
                .where(execution_groups.c.plan_id == plan_id)
                .order_by(execution_groups.c.sleeve_id)
            ).scalars()
            return tuple(ExecutionGroup.model_validate(item) for item in payloads)

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
        for previous, observed in zip(
            current.target_legs,
            updated.target_legs,
            strict=True,
        ):
            _validate_leg_progress(previous, observed)
        updated_compensations = {item.execution_leg_id: item for item in updated.compensation_legs}
        for previous in current.compensation_legs:
            observed = updated_compensations.get(previous.execution_leg_id)
            if observed is None or _leg_contract(observed) != _leg_contract(previous):
                raise ValueError("ExecutionGroup 既有 Compensation Leg 合同不得变更")
            _validate_leg_progress(
                previous,
                observed,
            )
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


def _validate_leg_progress(previous: ExecutionLeg, observed: ExecutionLeg) -> None:
    if observed.filled_quantity < previous.filled_quantity:
        raise ValueError("ExecutionLeg 累计成交数量不得倒退")
    if previous.venue_order_id is not None and (observed.venue_order_id != previous.venue_order_id):
        raise ValueError("ExecutionLeg Venue order identity 不得变更")
    if (
        previous.observed_at is not None
        and observed.observed_at is not None
        and observed.observed_at < previous.observed_at
    ):
        raise ValueError("ExecutionLeg Venue 观察时间不得倒退")
    if previous.status.terminal and observed != previous:
        raise ValueError("ExecutionLeg 终态不得变更")
