from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from investment_manager.execution.models import (
    ExitReason,
    Fill,
    Order,
    OrderStatus,
    OrderType,
    PositionLifecycle,
    Side,
)
from investment_manager.execution.policy import ExecutionPolicy
from investment_manager.kernel.identity import stable_id
from investment_manager.legacy.models import TradeIntent
from investment_manager.market.models import MarketSnapshot
from investment_manager.risk.models import (
    RiskDecision,
    RiskOutcome,
)


def entry_client_order_id(intent: TradeIntent, risk: RiskDecision) -> str:
    return stable_id("mock", intent.cycle_id, risk.intent_hash, intent.action.value)[:36]


def exit_client_order_id(lifecycle: PositionLifecycle, reason: ExitReason) -> str:
    return stable_id("mock-exit", lifecycle.position_id, reason.value)[:36]


class ExecutionExchange(Protocol):
    """执行边界；submit/exit 的实现必须先按确定性 client_order_id 查询。"""

    def query_order(self, client_order_id: str) -> Order | None: ...

    def query_entry_order(
        self,
        *,
        intent: TradeIntent,
        risk: RiskDecision,
        market: MarketSnapshot,
    ) -> Order | None: ...

    def submit(
        self,
        *,
        intent: TradeIntent,
        risk: RiskDecision,
        market: MarketSnapshot,
    ) -> Order: ...

    def cancel_remaining(self, order: Order) -> Order: ...

    def register_protection(self, lifecycle: PositionLifecycle) -> str | None: ...

    def submit_position_exit(
        self,
        *,
        lifecycle: PositionLifecycle,
        market: MarketSnapshot,
        reason: ExitReason,
    ) -> Order: ...


class MockExchange:
    """确定性的 Mock 撮合器；相同 clientOrderId 永远返回同一订单。"""

    def __init__(self, policy: ExecutionPolicy, *, accept_protection: bool = True) -> None:
        self._policy = policy
        self._orders: dict[str, Order] = {}
        self._accept_protection = accept_protection
        self._protections: set[str] = set()

    def submit(
        self,
        *,
        intent: TradeIntent,
        risk: RiskDecision,
        market: MarketSnapshot,
    ) -> Order:
        if (
            risk.outcome != RiskOutcome.APPROVED
            or risk.reservation is None
            or risk.quantity is None
        ):
            raise ValueError("没有有效 RiskReservation，禁止提交订单")
        if risk.reservation.expires_at <= market.as_of:
            raise ValueError("RiskReservation 已过期")

        client_order_id = entry_client_order_id(intent, risk)
        existing = self._orders.get(client_order_id)
        if existing is not None:
            return existing

        base_price = market.ask if intent.side == Side.BUY else market.bid
        executable = True
        fill_price = base_price
        if intent.entry.order_type == OrderType.LIMIT:
            assert intent.entry.price is not None
            executable = (
                intent.entry.price >= market.ask
                if intent.side == Side.BUY
                else intent.entry.price <= market.bid
            )
            fill_price = intent.entry.price
        else:
            slippage = self._policy.market_slippage_bps / Decimal("10000")
            multiplier = (
                Decimal("1") + slippage if intent.side == Side.BUY else Decimal("1") - slippage
            )
            fill_price = base_price * multiplier

        order_id = stable_id("order", client_order_id)
        fills: tuple[Fill, ...] = ()
        status = OrderStatus.NEW
        if executable:
            filled_quantity = risk.quantity * self._policy.default_fill_fraction
            fee = filled_quantity * fill_price * self._policy.fee_bps / Decimal("10000")
            fill = Fill(
                fill_id=stable_id("fill", order_id, "1"),
                order_id=order_id,
                event_time=market.as_of,
                price=fill_price,
                quantity=filled_quantity,
                fee=fee,
            )
            fills = (fill,)
            status = (
                OrderStatus.FILLED
                if self._policy.default_fill_fraction == Decimal("1")
                else OrderStatus.PARTIALLY_FILLED
            )

        order = Order(
            order_id=order_id,
            client_order_id=client_order_id,
            cycle_id=intent.cycle_id,
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            side=intent.side,
            order_type=intent.entry.order_type,
            requested_quantity=risk.quantity,
            limit_price=intent.entry.price,
            status=status,
            fills=fills,
        )
        self._orders[client_order_id] = order
        return order

    def query_entry_order(
        self,
        *,
        intent: TradeIntent,
        risk: RiskDecision,
        market: MarketSnapshot,
    ) -> Order | None:
        del market
        return self.query_order(entry_client_order_id(intent, risk))

    def query_order(self, client_order_id: str) -> Order | None:
        return self._orders.get(client_order_id)

    def register_protection(self, lifecycle: PositionLifecycle) -> str | None:
        if not self._accept_protection:
            return None
        protection_id = stable_id(
            "protect", lifecycle.position_id, lifecycle.stop_price, lifecycle.quantity
        )
        self._protections.add(protection_id)
        return protection_id

    def cancel_remaining(self, order: Order) -> Order:
        if order.status not in {OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED}:
            return order
        canceled = order.model_copy(update={"status": OrderStatus.CANCELED})
        self._orders[order.client_order_id] = canceled
        return canceled

    def submit_position_exit(
        self,
        *,
        lifecycle: PositionLifecycle,
        market: MarketSnapshot,
        reason: ExitReason,
    ) -> Order:
        client_order_id = exit_client_order_id(lifecycle, reason)
        existing = self._orders.get(client_order_id)
        if existing is not None:
            return existing
        slippage = self._policy.market_slippage_bps / Decimal("10000")
        fill_price = market.bid * (Decimal("1") - slippage)
        order_id = stable_id("order", client_order_id)
        fee = lifecycle.quantity * fill_price * self._policy.fee_bps / Decimal("10000")
        fill = Fill(
            fill_id=stable_id("fill", order_id, "1"),
            order_id=order_id,
            event_time=market.as_of,
            price=fill_price,
            quantity=lifecycle.quantity,
            fee=fee,
        )
        order = Order(
            order_id=order_id,
            client_order_id=client_order_id,
            cycle_id=lifecycle.cycle_id,
            intent_id=lifecycle.intent_id,
            symbol=lifecycle.symbol,
            side=Side.SELL,
            order_type=OrderType.MARKET,
            requested_quantity=lifecycle.quantity,
            status=OrderStatus.FILLED,
            fills=(fill,),
        )
        self._orders[client_order_id] = order
        return order

    @property
    def orders(self) -> tuple[Order, ...]:
        return tuple(self._orders.values())
