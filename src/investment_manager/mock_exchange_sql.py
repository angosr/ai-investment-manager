from __future__ import annotations

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.config import ExecutionPolicy
from investment_manager.domain import (
    ExitReason,
    MarketSnapshot,
    Order,
    OrderStatus,
    PositionLifecycle,
    RiskDecision,
    TradeIntent,
)
from investment_manager.execution import (
    MockExchange,
    entry_client_order_id,
    exit_client_order_id,
)
from investment_manager.ids import stable_id
from investment_manager.persistence import mock_exchange_orders, mock_exchange_protections


class SqlMockExchange:
    """独立事务的持久化 Mock 交易所；模拟真实外部系统的提交边界。"""

    def __init__(
        self,
        engine: Engine,
        policy: ExecutionPolicy,
        *,
        accept_protection: bool = True,
    ) -> None:
        self._engine = engine
        self._policy = policy
        self._accept_protection = accept_protection

    def query_order(self, client_order_id: str) -> Order | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(mock_exchange_orders.c.payload).where(
                    mock_exchange_orders.c.client_order_id == client_order_id
                )
            ).scalar_one_or_none()
        return Order.model_validate(payload) if payload is not None else None

    def query_entry_order(
        self,
        *,
        intent: TradeIntent,
        risk: RiskDecision,
        market: MarketSnapshot,
    ) -> Order | None:
        del market
        return self.query_order(entry_client_order_id(intent, risk))

    def submit(
        self,
        *,
        intent: TradeIntent,
        risk: RiskDecision,
        market: MarketSnapshot,
    ) -> Order:
        client_order_id = entry_client_order_id(intent, risk)
        existing = self.query_order(client_order_id)
        if existing is not None:
            return existing
        order = MockExchange(self._policy).submit(intent=intent, risk=risk, market=market)
        return self._insert_order(order, role="ENTRY", observed_at=market.as_of)

    def cancel_remaining(self, order: Order) -> Order:
        with self._engine.begin() as connection:
            payload = connection.execute(
                select(mock_exchange_orders.c.payload)
                .where(mock_exchange_orders.c.client_order_id == order.client_order_id)
                .with_for_update()
            ).scalar_one()
            stored = Order.model_validate(payload)
            if stored.status not in {OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED}:
                return stored
            canceled = stored.model_copy(update={"status": OrderStatus.CANCELED})
            connection.execute(
                update(mock_exchange_orders)
                .where(mock_exchange_orders.c.client_order_id == order.client_order_id)
                .values(payload=canceled.model_dump(mode="json"))
            )
        return canceled

    def register_protection(self, lifecycle: PositionLifecycle) -> str | None:
        if not self._accept_protection:
            return None
        protection_id = stable_id(
            "protect", lifecycle.position_id, lifecycle.stop_price, lifecycle.quantity
        )
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(mock_exchange_protections).values(
                        protection_id=protection_id,
                        position_id=lifecycle.position_id,
                        created_at=lifecycle.opened_at,
                        payload=lifecycle.model_dump(mode="json"),
                    )
                )
        except IntegrityError:
            with self._engine.connect() as connection:
                existing = connection.execute(
                    select(mock_exchange_protections.c.protection_id).where(
                        mock_exchange_protections.c.position_id == lifecycle.position_id
                    )
                ).scalar_one()
            if existing != protection_id:
                raise ValueError("Mock 保护动作身份冲突") from None
        return protection_id

    def submit_position_exit(
        self,
        *,
        lifecycle: PositionLifecycle,
        market: MarketSnapshot,
        reason: ExitReason,
    ) -> Order:
        client_order_id = exit_client_order_id(lifecycle, reason)
        existing = self.query_order(client_order_id)
        if existing is not None:
            return existing
        order = MockExchange(self._policy).submit_position_exit(
            lifecycle=lifecycle,
            market=market,
            reason=reason,
        )
        return self._insert_order(order, role="EXIT", observed_at=market.as_of)

    def _insert_order(self, order: Order, *, role: str, observed_at) -> Order:
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(mock_exchange_orders).values(
                        client_order_id=order.client_order_id,
                        order_id=order.order_id,
                        role=role,
                        observed_at=observed_at,
                        payload=order.model_dump(mode="json"),
                    )
                )
        except IntegrityError:
            existing = self.query_order(order.client_order_id)
            if existing != order:
                raise ValueError("相同 client_order_id 的 Mock 交易所事实不一致") from None
            return existing
        return order

    @property
    def orders(self) -> tuple[Order, ...]:
        with self._engine.connect() as connection:
            payloads = connection.execute(
                select(mock_exchange_orders.c.payload).order_by(
                    mock_exchange_orders.c.observed_at,
                    mock_exchange_orders.c.client_order_id,
                )
            ).scalars()
            return tuple(Order.model_validate(payload) for payload in payloads)
