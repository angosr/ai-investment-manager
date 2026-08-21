"""One-step reconciler for recoverable multi-leg execution groups."""

from __future__ import annotations

from datetime import datetime, timedelta

from investment_manager.execution.group.models import (
    ExecutionGroup,
    ExecutionGroupStatus,
    ExecutionLeg,
    ExecutionLegStatus,
    compensation_leg,
    new_execution_group,
)
from investment_manager.execution.group.repository import ExecutionGroupStore
from investment_manager.execution.planning.planner import PlannedTradeGroup, TradePlan
from investment_manager.execution.venue.product import (
    ProductOrder,
    ProductOrderStatus,
    ProductOrderVenue,
    UnknownVenueResult,
)
from investment_manager.kernel.time import require_utc


class ExecutionGroupEngine:
    """Reconcile venue facts before every idempotent mutation; never invent a failure terminal."""

    def __init__(
        self,
        *,
        store: ExecutionGroupStore,
        venue: ProductOrderVenue,
    ) -> None:
        self._store = store
        self._venue = venue

    def start(
        self,
        *,
        plan: TradePlan,
        planned: PlannedTradeGroup,
        as_of: datetime,
    ) -> ExecutionGroup:
        if planned not in plan.groups:
            raise ValueError("PlannedTradeGroup 不属于指定 TradePlan")
        group = new_execution_group(
            plan_id=plan.plan_id,
            planned=planned,
            started_at=as_of,
        )
        self._store.record(group)
        stored = self._store.group(group.group_id)
        if stored is None:  # pragma: no cover - store contract guard
            raise RuntimeError("ExecutionGroup 持久化后不可见")
        return stored

    def run_once(self, group_id: str, *, as_of: datetime) -> ExecutionGroup:
        as_of = require_utc(as_of)
        group = self._store.group(group_id)
        if group is None:
            raise ValueError("ExecutionGroup 不存在")
        if as_of < group.updated_at:
            raise ValueError("ExecutionGroup 不能用过去时点推进")
        if group.terminal:
            return group
        if group.status == ExecutionGroupStatus.COMPENSATING:
            return self._compensate(group, as_of=as_of)

        target_legs = tuple(
            self._sync_leg(
                leg,
                as_of=as_of,
                allow_submit=as_of < group.valid_until,
            )
            for leg in group.target_legs
        )
        if all(item.status == ExecutionLegStatus.FILLED for item in target_legs):
            return self._save(
                group,
                as_of=as_of,
                status=ExecutionGroupStatus.HEDGED,
                target_legs=target_legs,
                unhedged_since=None,
            )

        observed = self._evolve(
            group,
            as_of=as_of,
            target_legs=target_legs,
            unhedged_since=self._unhedged_since(
                group,
                target_legs=target_legs,
                as_of=as_of,
            ),
        )
        if self._must_compensate(observed, as_of=as_of):
            compensating = self._persist(
                group,
                observed.model_copy(
                    update={
                        "status": ExecutionGroupStatus.COMPENSATING,
                        "revision": group.revision + 1,
                    }
                ),
            )
            return self._compensate(compensating, as_of=as_of)

        status = (
            ExecutionGroupStatus.RECOVERING
            if any(
                item.status in {ExecutionLegStatus.PARTIALLY_FILLED, ExecutionLegStatus.UNKNOWN}
                for item in target_legs
            )
            or observed.unhedged_notional > 0
            else ExecutionGroupStatus.EXECUTING
        )
        return self._persist(
            group,
            observed.model_copy(update={"status": status, "revision": group.revision + 1}),
        )

    def _compensate(self, group: ExecutionGroup, *, as_of: datetime) -> ExecutionGroup:
        target_legs = tuple(self._stop_target(leg, as_of=as_of) for leg in group.target_legs)
        stopped = self._save(
            group,
            as_of=as_of,
            status=ExecutionGroupStatus.COMPENSATING,
            target_legs=target_legs,
        )
        if not all(item.status.terminal for item in stopped.target_legs):
            return stopped

        compensation_legs = self._ensure_compensation_legs(stopped)
        if compensation_legs != stopped.compensation_legs:
            stopped = self._save(
                stopped,
                as_of=as_of,
                status=ExecutionGroupStatus.COMPENSATING,
                compensation_legs=compensation_legs,
            )
        if not stopped.residual_quantities:
            return self._save(
                stopped,
                as_of=as_of,
                status=ExecutionGroupStatus.FLAT,
                unhedged_since=None,
            )

        reconciled_compensations = tuple(
            self._sync_leg(leg, as_of=as_of, allow_submit=True) if not leg.status.terminal else leg
            for leg in stopped.compensation_legs
        )
        reconciled = self._save(
            stopped,
            as_of=as_of,
            status=ExecutionGroupStatus.COMPENSATING,
            compensation_legs=reconciled_compensations,
        )
        if not reconciled.residual_quantities:
            return self._save(
                reconciled,
                as_of=as_of,
                status=ExecutionGroupStatus.FLAT,
                unhedged_since=None,
            )
        return reconciled

    def _sync_leg(
        self,
        leg: ExecutionLeg,
        *,
        as_of: datetime,
        allow_submit: bool,
    ) -> ExecutionLeg:
        if leg.status.terminal:
            return leg
        try:
            observed = self._venue.query(leg.client_order_id)
        except UnknownVenueResult:
            return leg.model_copy(update={"status": ExecutionLegStatus.UNKNOWN})
        if observed is None and allow_submit:
            try:
                observed = self._venue.submit(leg, observed_at=as_of)
            except UnknownVenueResult:
                try:
                    observed = self._venue.query(leg.client_order_id)
                except UnknownVenueResult:
                    observed = None
        if observed is not None:
            return self._from_order(leg, observed)
        if not allow_submit and leg.status == ExecutionLegStatus.PENDING:
            return leg.model_copy(
                update={
                    "status": ExecutionLegStatus.EXPIRED,
                    "observed_at": as_of,
                }
            )
        return leg.model_copy(update={"status": ExecutionLegStatus.UNKNOWN})

    def _stop_target(self, leg: ExecutionLeg, *, as_of: datetime) -> ExecutionLeg:
        if leg.status.terminal:
            return leg
        try:
            observed = self._venue.query(leg.client_order_id)
            if observed is None or not observed.status.terminal:
                observed = self._venue.cancel(leg.client_order_id, observed_at=as_of)
        except UnknownVenueResult:
            try:
                observed = self._venue.query(leg.client_order_id)
            except UnknownVenueResult:
                return leg.model_copy(update={"status": ExecutionLegStatus.UNKNOWN})
        if observed is not None:
            return self._from_order(leg, observed)
        return leg.model_copy(
            update={
                "status": ExecutionLegStatus.CANCELED,
                "observed_at": as_of,
            }
        )

    @staticmethod
    def _from_order(leg: ExecutionLeg, order: ProductOrder) -> ExecutionLeg:
        if (
            order.client_order_id != leg.client_order_id
            or order.execution_leg_id != leg.execution_leg_id
            or order.group_id != leg.group_id
            or order.instrument != leg.instrument
            or order.side != leg.side
            or order.requested_quantity != leg.requested_quantity
        ):
            raise ValueError("Venue order 与 ExecutionLeg 合同不一致")
        status = {
            ProductOrderStatus.WORKING: ExecutionLegStatus.WORKING,
            ProductOrderStatus.PARTIALLY_FILLED: ExecutionLegStatus.PARTIALLY_FILLED,
            ProductOrderStatus.FILLED: ExecutionLegStatus.FILLED,
            ProductOrderStatus.CANCELED: ExecutionLegStatus.CANCELED,
            ProductOrderStatus.REJECTED: ExecutionLegStatus.REJECTED,
            ProductOrderStatus.EXPIRED: ExecutionLegStatus.EXPIRED,
        }[order.status]
        return leg.model_copy(
            update={
                "status": status,
                "filled_quantity": order.filled_quantity,
                "average_fill_price": order.average_fill_price,
                "fee": order.fee,
                "venue_order_id": order.venue_order_id,
                "observed_at": order.observed_at,
            }
        )

    @staticmethod
    def _unhedged_since(
        group: ExecutionGroup,
        *,
        target_legs: tuple[ExecutionLeg, ...],
        as_of: datetime,
    ) -> datetime | None:
        candidate = group.model_copy(update={"target_legs": target_legs})
        if candidate.unhedged_notional <= 0:
            return None
        return group.unhedged_since or as_of

    @staticmethod
    def _must_compensate(group: ExecutionGroup, *, as_of: datetime) -> bool:
        if any(
            item.status
            in {
                ExecutionLegStatus.CANCELED,
                ExecutionLegStatus.REJECTED,
                ExecutionLegStatus.EXPIRED,
            }
            for item in group.target_legs
        ):
            return True
        if as_of >= group.valid_until:
            return True
        if group.unhedged_notional > group.maximum_unhedged_notional:
            return True
        return group.unhedged_since is not None and as_of >= (
            group.unhedged_since + timedelta(seconds=group.maximum_unhedged_seconds)
        )

    @staticmethod
    def _ensure_compensation_legs(group: ExecutionGroup) -> tuple[ExecutionLeg, ...]:
        compensations = list(group.compensation_legs)
        for target in group.target_legs:
            reversed_quantity = sum(
                item.filled_quantity
                for item in compensations
                if item.planned_leg_id == target.planned_leg_id
            )
            residual = target.filled_quantity - reversed_quantity
            active = any(
                item.planned_leg_id == target.planned_leg_id and not item.status.terminal
                for item in compensations
            )
            if residual <= 0 or active:
                continue
            attempt = 1 + max(
                (
                    item.attempt
                    for item in compensations
                    if item.planned_leg_id == target.planned_leg_id
                ),
                default=0,
            )
            compensations.append(
                compensation_leg(
                    target,
                    requested_quantity=residual,
                    attempt=attempt,
                )
            )
        return tuple(sorted(compensations, key=lambda item: item.execution_leg_id))

    def _save(
        self,
        group: ExecutionGroup,
        *,
        as_of: datetime,
        **changes,
    ) -> ExecutionGroup:
        if all(getattr(group, name) == value for name, value in changes.items()):
            return group
        return self._persist(
            group,
            self._evolve(group, as_of=as_of, **changes).model_copy(
                update={"revision": group.revision + 1}
            ),
        )

    def _persist(self, current: ExecutionGroup, updated: ExecutionGroup) -> ExecutionGroup:
        if current == updated:
            return current
        self._store.save(updated, expected_revision=current.revision)
        return updated

    @staticmethod
    def _evolve(
        group: ExecutionGroup,
        *,
        as_of: datetime,
        **changes,
    ) -> ExecutionGroup:
        payload = group.model_dump(mode="python")
        payload.update(changes)
        payload["updated_at"] = as_of
        return ExecutionGroup.model_validate(payload)
