"""Persistent fault-injectable product venue for grouped paper execution."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.execution.group.models import ExecutionLeg
from investment_manager.execution.tables import mock_product_orders
from investment_manager.execution.venue.product import (
    ProductOrder,
    ProductOrderStatus,
    UnknownVenueResult,
)
from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.time import require_utc

_BPS = Decimal("10000")


class MockSubmitBehavior(StrEnum):
    FILL = "FILL"
    PARTIAL_FILL = "PARTIAL_FILL"
    REJECT = "REJECT"
    BEFORE_ACCEPT_RESPONSE_LOST = "BEFORE_ACCEPT_RESPONSE_LOST"
    AFTER_ACCEPT_RESPONSE_LOST = "AFTER_ACCEPT_RESPONSE_LOST"


class SqlMockProductVenue:
    """Mock venue facts survive application restarts and remain client-ID idempotent."""

    def __init__(
        self,
        engine: Engine,
        *,
        fee_bps: Decimal = Decimal("5"),
        submit_behaviors: Mapping[str, tuple[MockSubmitBehavior, ...]] | None = None,
    ) -> None:
        if fee_bps < 0:
            raise ValueError("Mock fee_bps 不能为负")
        self._engine = engine
        self._fee_bps = fee_bps
        self._behaviors = {key: list(values) for key, values in (submit_behaviors or {}).items()}

    def query(self, client_order_id: str) -> ProductOrder | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(mock_product_orders.c.payload).where(
                    mock_product_orders.c.client_order_id == client_order_id
                )
            ).scalar_one_or_none()
        return None if payload is None else ProductOrder.model_validate(payload)

    def submit(self, leg: ExecutionLeg, *, observed_at: datetime) -> ProductOrder:
        observed_at = require_utc(observed_at)
        existing = self.query(leg.client_order_id)
        if existing is not None:
            self._require_same_request(existing, leg)
            return existing
        behavior = self._next_behavior(leg.client_order_id)
        if behavior == MockSubmitBehavior.BEFORE_ACCEPT_RESPONSE_LOST:
            raise UnknownVenueResult("Mock 在接受订单前丢失响应")
        order = self._order_for(leg, behavior=behavior, observed_at=observed_at)
        stored = self._insert(order)
        if behavior == MockSubmitBehavior.AFTER_ACCEPT_RESPONSE_LOST:
            raise UnknownVenueResult("Mock 接受订单后丢失响应")
        return stored

    def cancel(
        self,
        client_order_id: str,
        *,
        observed_at: datetime,
    ) -> ProductOrder | None:
        observed_at = require_utc(observed_at)
        with self._engine.begin() as connection:
            payload = connection.execute(
                select(mock_product_orders.c.payload)
                .where(mock_product_orders.c.client_order_id == client_order_id)
                .with_for_update()
            ).scalar_one_or_none()
            if payload is None:
                return None
            stored = ProductOrder.model_validate(payload)
            if stored.status.terminal:
                return stored
            canceled = stored.model_copy(
                update={
                    "status": ProductOrderStatus.CANCELED,
                    "observed_at": observed_at,
                }
            )
            connection.execute(
                update(mock_product_orders)
                .where(mock_product_orders.c.client_order_id == client_order_id)
                .values(
                    status=canceled.status.value,
                    observed_at=observed_at,
                    payload=canceled.model_dump(mode="json"),
                )
            )
        return canceled

    def fill_remaining(self, client_order_id: str, *, observed_at: datetime) -> ProductOrder:
        """Advance a working mock order; only a test/paper-market event may call this."""

        observed_at = require_utc(observed_at)
        with self._engine.begin() as connection:
            payload = connection.execute(
                select(mock_product_orders.c.payload)
                .where(mock_product_orders.c.client_order_id == client_order_id)
                .with_for_update()
            ).scalar_one()
            stored = ProductOrder.model_validate(payload)
            if stored.status != ProductOrderStatus.PARTIALLY_FILLED:
                return stored
            completed = stored.model_copy(
                update={
                    "status": ProductOrderStatus.FILLED,
                    "filled_quantity": stored.requested_quantity,
                    "average_fill_price": stored.average_fill_price,
                    "fee": (
                        stored.requested_quantity
                        * (stored.average_fill_price or Decimal("0"))
                        * stored.instrument.contract_multiplier
                        * self._fee_bps
                        / _BPS
                    ),
                    "observed_at": observed_at,
                }
            )
            connection.execute(
                update(mock_product_orders)
                .where(mock_product_orders.c.client_order_id == client_order_id)
                .values(
                    status=completed.status.value,
                    observed_at=observed_at,
                    payload=completed.model_dump(mode="json"),
                )
            )
        return completed

    def _next_behavior(self, client_order_id: str) -> MockSubmitBehavior:
        behaviors = self._behaviors.get(client_order_id)
        return behaviors.pop(0) if behaviors else MockSubmitBehavior.FILL

    def _order_for(
        self,
        leg: ExecutionLeg,
        *,
        behavior: MockSubmitBehavior,
        observed_at: datetime,
    ) -> ProductOrder:
        if behavior == MockSubmitBehavior.REJECT:
            status = ProductOrderStatus.REJECTED
            filled = Decimal("0")
        elif behavior == MockSubmitBehavior.PARTIAL_FILL:
            status = ProductOrderStatus.PARTIALLY_FILLED
            filled = leg.requested_quantity / Decimal("2")
        else:
            status = ProductOrderStatus.FILLED
            filled = leg.requested_quantity
        price = leg.reference_price if filled > 0 else None
        fee = (
            filled
            * (price or Decimal("0"))
            * leg.instrument.contract_multiplier
            * self._fee_bps
            / _BPS
        )
        return ProductOrder(
            client_order_id=leg.client_order_id,
            venue_order_id=stable_id("mock_product_order", leg.client_order_id),
            group_id=leg.group_id,
            execution_leg_id=leg.execution_leg_id,
            instrument=leg.instrument,
            side=leg.side,
            requested_quantity=leg.requested_quantity,
            status=status,
            filled_quantity=filled,
            average_fill_price=price,
            fee=fee,
            observed_at=observed_at,
        )

    def _insert(self, order: ProductOrder) -> ProductOrder:
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(mock_product_orders).values(
                        client_order_id=order.client_order_id,
                        venue_order_id=order.venue_order_id,
                        group_id=order.group_id,
                        execution_leg_id=order.execution_leg_id,
                        status=order.status.value,
                        observed_at=order.observed_at,
                        payload=order.model_dump(mode="json"),
                    )
                )
        except IntegrityError:
            existing = self.query(order.client_order_id)
            if existing != order:
                raise ValueError("相同 client_order_id 的产品 Mock 事实不一致") from None
            return existing
        return order

    @staticmethod
    def _require_same_request(order: ProductOrder, leg: ExecutionLeg) -> None:
        if (
            order.execution_leg_id != leg.execution_leg_id
            or order.group_id != leg.group_id
            or order.instrument != leg.instrument
            or order.side != leg.side
            or order.requested_quantity != leg.requested_quantity
        ):
            raise ValueError("client_order_id 已绑定不同产品订单")
