from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from investment_manager.execution.models import (
    AccountSnapshot,
    ExitReason,
    Fill,
    Order,
    OrderStatus,
    OrderType,
    Position,
    PositionLifecycle,
    Side,
)
from investment_manager.execution.policy import BinanceTestnetPolicy
from investment_manager.execution.reconciliation.engine import (
    TradingStateSnapshot,
    TradingStateSource,
)
from investment_manager.governance.policy import DeploymentStage
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.types import floor_to_step
from investment_manager.legacy.exchange import entry_client_order_id, exit_client_order_id
from investment_manager.legacy.models import TradeIntent
from investment_manager.market.models import MarketSnapshot
from investment_manager.risk.models import (
    RiskDecision,
    RiskOutcome,
)
from investment_manager.settings import AppConfig


@dataclass(frozen=True, slots=True)
class BinanceCredentials:
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)

    @classmethod
    def from_environment(
        cls,
        policy: BinanceTestnetPolicy,
        *,
        environment: Mapping[str, str] | None = None,
        require_order_submission: bool = False,
    ) -> BinanceCredentials:
        values = environment if environment is not None else os.environ
        prefix = policy.credential_environment_prefix
        if values.get(f"{prefix}_ENVIRONMENT") != "testnet":
            raise ValueError("Binance 凭证环境必须显式为 testnet")
        api_key = values.get(f"{prefix}_API_KEY", "").strip()
        api_secret = values.get(f"{prefix}_API_SECRET", "").strip()
        if not api_key or not api_secret:
            raise ValueError("Binance Testnet API Key/Secret 未配置")
        if require_order_submission and values.get(
            f"{prefix}_ORDER_SUBMISSION_ENABLED", ""
        ).lower() not in {"1", "true", "yes"}:
            raise ValueError("Binance Testnet 订单提交环境门禁未开启")
        return cls(api_key=api_key, api_secret=api_secret)


class BinanceTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: int,
    ) -> tuple[int, Any]: ...


class HttpxBinanceTransport:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: int,
    ) -> tuple[int, Any]:
        response = httpx.request(
            method,
            url,
            headers=dict(headers),
            timeout=timeout_seconds,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise BinanceTransportError("Binance 返回非 JSON 响应") from exc
        return response.status_code, payload


class BinanceTransportError(RuntimeError):
    pass


class BinanceUnknownExecution(RuntimeError):
    pass


class BinanceManualIntervention(RuntimeError):
    pass


class BinanceApiError(RuntimeError):
    def __init__(self, status_code: int, code: int | None, message: str) -> None:
        super().__init__(f"Binance API error status={status_code} code={code}: {message}")
        self.status_code = status_code
        self.code = code


class BinanceTestnetClient:
    def __init__(
        self,
        policy: BinanceTestnetPolicy,
        credentials: BinanceCredentials,
        *,
        transport: BinanceTransport | None = None,
        wall_clock_ms=lambda: int(time.time() * 1000),
        monotonic_clock=time.monotonic,
    ) -> None:
        self._policy = policy
        self._credentials = credentials
        self._transport = transport or HttpxBinanceTransport()
        self._wall_clock_ms = wall_clock_ms
        self._monotonic_clock = monotonic_clock
        self._offset_ms = 0
        self._last_sync_monotonic: float | None = None
        self._time_lock = threading.Lock()

    def ping(self) -> None:
        self._public("GET", "/v3/ping")

    def server_time(self) -> int:
        payload = self._public("GET", "/v3/time")
        return int(payload["serverTime"])

    def exchange_info(self, symbol: str) -> dict[str, Any]:
        payload = self._public("GET", "/v3/exchangeInfo", {"symbol": symbol})
        symbols = payload.get("symbols", [])
        if len(symbols) != 1 or symbols[0].get("symbol") != symbol:
            raise ValueError(f"Binance Testnet 未返回唯一交易规则: {symbol}")
        return symbols[0]

    def ticker_price(self, symbol: str) -> Decimal:
        payload = self._public("GET", "/v3/ticker/price", {"symbol": symbol})
        return Decimal(str(payload["price"]))

    def account(self) -> dict[str, Any]:
        return self._signed("GET", "/v3/account", {"omitZeroBalances": "true"})

    def order_test(self, parameters: Mapping[str, Any]) -> dict[str, Any]:
        return self._signed("POST", "/v3/order/test", parameters)

    def new_order(self, parameters: Mapping[str, Any]) -> dict[str, Any]:
        return self._signed("POST", "/v3/order", parameters)

    def get_order(self, *, symbol: str, client_order_id: str) -> dict[str, Any] | None:
        try:
            return self._signed(
                "GET",
                "/v3/order",
                {"symbol": symbol, "origClientOrderId": client_order_id},
            )
        except BinanceApiError as exc:
            if exc.code == -2013:
                return None
            raise

    def cancel_order(self, *, symbol: str, client_order_id: str) -> dict[str, Any]:
        return self._signed(
            "DELETE",
            "/v3/order",
            {"symbol": symbol, "origClientOrderId": client_order_id},
        )

    def trades(self, *, symbol: str, order_id: str) -> list[dict[str, Any]]:
        payload = self._signed(
            "GET",
            "/v3/myTrades",
            {"symbol": symbol, "orderId": order_id},
        )
        if not isinstance(payload, list):
            raise ValueError("Binance myTrades 响应必须为数组")
        return payload

    def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        parameters = {"symbol": symbol} if symbol is not None else {}
        payload = self._signed("GET", "/v3/openOrders", parameters)
        if not isinstance(payload, list):
            raise ValueError("Binance openOrders 响应必须为数组")
        return payload

    def _public(
        self,
        method: str,
        path: str,
        parameters: Mapping[str, Any] | None = None,
    ) -> Any:
        query = _encode(parameters or {})
        url = f"{self._policy.rest_base_url}{path}" + (f"?{query}" if query else "")
        return self._request(method, url, headers={})

    def _signed(
        self,
        method: str,
        path: str,
        parameters: Mapping[str, Any],
        *,
        retry_clock: bool = True,
    ) -> Any:
        self._ensure_time_sync()
        values = dict(parameters)
        values["recvWindow"] = self._policy.recv_window_ms
        values["timestamp"] = self._wall_clock_ms() + self._offset_ms
        payload = _encode(values)
        signature = hmac.new(
            self._credentials.api_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        url = f"{self._policy.rest_base_url}{path}?{payload}&signature={signature}"
        try:
            return self._request(
                method,
                url,
                headers={"X-MBX-APIKEY": self._credentials.api_key},
            )
        except BinanceApiError as exc:
            if exc.code == -1021 and retry_clock:
                self._sync_time()
                return self._signed(method, path, parameters, retry_clock=False)
            raise

    def _request(self, method: str, url: str, *, headers: Mapping[str, str]) -> Any:
        try:
            status_code, payload = self._transport.request(
                method,
                url,
                headers=headers,
                timeout_seconds=self._policy.request_timeout_seconds,
            )
        except BinanceTransportError:
            raise
        except Exception as exc:
            raise BinanceTransportError("Binance 请求传输失败") from exc
        if status_code >= 400 or (isinstance(payload, dict) and int(payload.get("code", 0)) < 0):
            code = int(payload["code"]) if isinstance(payload, dict) and "code" in payload else None
            message = (
                str(payload.get("msg", "request failed"))
                if isinstance(payload, dict)
                else "request failed"
            )
            raise BinanceApiError(status_code, code, message)
        return payload

    def _ensure_time_sync(self) -> None:
        with self._time_lock:
            if (
                self._last_sync_monotonic is None
                or self._monotonic_clock() - self._last_sync_monotonic
                >= self._policy.time_sync_ttl_seconds
            ):
                self._sync_time_locked()

    def _sync_time(self) -> None:
        with self._time_lock:
            self._sync_time_locked()

    def _sync_time_locked(self) -> None:
        before = self._wall_clock_ms()
        server = self.server_time()
        after = self._wall_clock_ms()
        midpoint = before + (after - before) // 2
        self._offset_ms = server - midpoint
        self._last_sync_monotonic = self._monotonic_clock()


@dataclass(frozen=True, slots=True)
class SymbolRules:
    symbol: str
    base_asset: str
    quote_asset: str
    order_types: frozenset[str]
    tick_size: Decimal
    min_quantity: Decimal
    max_quantity: Decimal
    step_size: Decimal
    market_min_quantity: Decimal
    market_max_quantity: Decimal
    market_step_size: Decimal
    min_notional: Decimal
    max_notional: Decimal | None

    @classmethod
    def from_exchange_info(cls, raw: Mapping[str, Any]) -> SymbolRules:
        filters = {item["filterType"]: item for item in raw["filters"]}
        price = filters["PRICE_FILTER"]
        lot = filters["LOT_SIZE"]
        market = filters.get("MARKET_LOT_SIZE", lot)
        notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
        maximum = Decimal(str(notional.get("maxNotional", "0")))
        return cls(
            symbol=str(raw["symbol"]),
            base_asset=str(raw["baseAsset"]),
            quote_asset=str(raw["quoteAsset"]),
            order_types=frozenset(str(item) for item in raw["orderTypes"]),
            tick_size=Decimal(str(price["tickSize"])),
            min_quantity=Decimal(str(lot["minQty"])),
            max_quantity=Decimal(str(lot["maxQty"])),
            step_size=Decimal(str(lot["stepSize"])),
            market_min_quantity=Decimal(str(market["minQty"])),
            market_max_quantity=Decimal(str(market["maxQty"])),
            market_step_size=Decimal(str(market["stepSize"])),
            min_notional=Decimal(str(notional.get("minNotional", "0"))),
            max_notional=maximum if maximum > 0 else None,
        )

    def quantity(self, value: Decimal, *, market: bool, reference_price: Decimal) -> Decimal:
        minimum = self.market_min_quantity if market else self.min_quantity
        maximum = self.market_max_quantity if market else self.max_quantity
        step = self.market_step_size if market else self.step_size
        if step <= 0:
            minimum, maximum, step = self.min_quantity, self.max_quantity, self.step_size
        normalized = _floor_step(value, step)
        if normalized < minimum or normalized > maximum:
            raise ValueError("订单数量不符合 Binance LOT_SIZE")
        notional = normalized * reference_price
        if self.min_notional > 0 and notional < self.min_notional:
            raise ValueError("订单名义金额低于 Binance 最小值")
        if self.max_notional is not None and notional > self.max_notional:
            raise ValueError("订单名义金额高于 Binance 最大值")
        return normalized

    def price(self, value: Decimal) -> Decimal:
        return _floor_step(value, self.tick_size)


@dataclass(frozen=True, slots=True)
class _OrderContext:
    cycle_id: str
    intent_id: str
    symbol: str
    side: Side
    order_type: OrderType
    requested_quantity: Decimal
    limit_price: Decimal | None
    base_asset: str
    quote_asset: str


class BinanceTestnetExchange:
    """Binance Spot Testnet 执行边界；所有提交均先按 clientOrderId 查询。"""

    def __init__(self, client: BinanceTestnetClient, *, quote_asset: str = "USDT") -> None:
        self._client = client
        self._quote_asset = quote_asset
        self._rules: dict[str, SymbolRules] = {}

    def query_order(self, client_order_id: str) -> Order | None:
        raise ValueError("Binance 查询必须带完整订单上下文")

    def query_known_order(self, order: Order) -> Order | None:
        return self._find(order.client_order_id, self._context_from_order(order))

    def query_entry_order(
        self,
        *,
        intent: TradeIntent,
        risk: RiskDecision,
        market: MarketSnapshot,
    ) -> Order | None:
        context = self._entry_context(intent=intent, risk=risk, market=market)
        return self._find(entry_client_order_id(intent, risk), context)

    def order_from_remote(self, raw: Mapping[str, Any]) -> Order:
        symbol = str(raw["symbol"])
        rules = self.rules(symbol)
        quantity = Decimal(str(raw["origQty"]))
        price = Decimal(str(raw.get("price", "0")))
        raw_type = str(raw["type"])
        order_type = OrderType.MARKET if raw_type in {"MARKET", "STOP_LOSS"} else OrderType.LIMIT
        context = _OrderContext(
            cycle_id=stable_id("external_cycle", symbol, raw["clientOrderId"]),
            intent_id=stable_id("external_intent", symbol, raw["clientOrderId"]),
            symbol=symbol,
            side=Side(str(raw["side"])),
            order_type=order_type,
            requested_quantity=quantity,
            limit_price=price if price > 0 and order_type == OrderType.LIMIT else None,
            base_asset=rules.base_asset,
            quote_asset=rules.quote_asset,
        )
        return self._from_raw(raw, context)

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
            raise ValueError("没有有效 RiskReservation，禁止提交 Testnet 订单")
        if risk.reservation.expires_at <= market.as_of:
            raise ValueError("RiskReservation 已过期")
        if intent.side != Side.BUY:
            raise ValueError("Spot Testnet MVP 不允许无持仓开空")
        context = self._entry_context(intent=intent, risk=risk, market=market)
        quantity = context.requested_quantity
        limit_price = context.limit_price
        client_order_id = entry_client_order_id(intent, risk)
        existing = self._find(client_order_id, context)
        if existing is not None:
            return existing
        parameters: dict[str, Any] = {
            "symbol": intent.symbol,
            "side": intent.side.value,
            "type": intent.entry.order_type.value,
            "quantity": _decimal_text(quantity),
            "newClientOrderId": client_order_id,
            "newOrderRespType": "FULL",
        }
        if limit_price is not None:
            parameters.update(timeInForce="GTC", price=_decimal_text(limit_price))
        return self._submit_with_recovery(parameters, client_order_id, context)

    def _entry_context(
        self,
        *,
        intent: TradeIntent,
        risk: RiskDecision,
        market: MarketSnapshot,
    ) -> _OrderContext:
        if risk.quantity is None:
            raise ValueError("建仓查询缺少已冻结订单数量")
        rules = self.rules(intent.symbol)
        quantity = rules.quantity(
            risk.quantity,
            market=intent.entry.order_type == OrderType.MARKET,
            reference_price=market.ask,
        )
        limit_price = (
            rules.price(intent.entry.price)
            if intent.entry.order_type == OrderType.LIMIT and intent.entry.price is not None
            else None
        )
        return _OrderContext(
            cycle_id=intent.cycle_id,
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            side=intent.side,
            order_type=intent.entry.order_type,
            requested_quantity=quantity,
            limit_price=limit_price,
            base_asset=rules.base_asset,
            quote_asset=rules.quote_asset,
        )

    def cancel_remaining(self, order: Order) -> Order:
        if order.status not in {OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED}:
            return order
        context = self._context_from_order(order)
        raw = self._client.cancel_order(
            symbol=order.symbol,
            client_order_id=order.client_order_id,
        )
        return self._from_raw(raw, context)

    def register_protection(self, lifecycle: PositionLifecycle) -> str | None:
        rules = self.rules(lifecycle.symbol)
        client_order_id = protection_client_order_id(lifecycle)
        order_type = "STOP_LOSS" if "STOP_LOSS" in rules.order_types else "STOP_LOSS_LIMIT"
        if order_type not in rules.order_types:
            return None
        quantity = rules.quantity(
            lifecycle.quantity,
            market=order_type == "STOP_LOSS",
            reference_price=lifecycle.stop_price,
        )
        stop_price = rules.price(lifecycle.stop_price)
        context = _OrderContext(
            cycle_id=lifecycle.cycle_id,
            intent_id=lifecycle.intent_id,
            symbol=lifecycle.symbol,
            side=Side.SELL,
            order_type=(OrderType.MARKET if order_type == "STOP_LOSS" else OrderType.LIMIT),
            requested_quantity=quantity,
            limit_price=None,
            base_asset=rules.base_asset,
            quote_asset=rules.quote_asset,
        )
        if self._find(client_order_id, context) is not None:
            return client_order_id
        parameters: dict[str, Any] = {
            "symbol": lifecycle.symbol,
            "side": "SELL",
            "type": order_type,
            "quantity": _decimal_text(quantity),
            "stopPrice": _decimal_text(stop_price),
            "newClientOrderId": client_order_id,
            "newOrderRespType": "FULL",
        }
        if order_type == "STOP_LOSS_LIMIT":
            limit_price = rules.price(stop_price * Decimal("0.995"))
            parameters.update(timeInForce="GTC", price=_decimal_text(limit_price))
        try:
            self._submit_with_recovery(parameters, client_order_id, context)
        except (BinanceApiError, BinanceTransportError, BinanceUnknownExecution, ValueError):
            return None
        return client_order_id

    def submit_position_exit(
        self,
        *,
        lifecycle: PositionLifecycle,
        market: MarketSnapshot,
        reason: ExitReason,
    ) -> Order:
        rules = self.rules(lifecycle.symbol)
        protection_context = _OrderContext(
            cycle_id=lifecycle.cycle_id,
            intent_id=lifecycle.intent_id,
            symbol=lifecycle.symbol,
            side=Side.SELL,
            order_type=OrderType.MARKET,
            requested_quantity=lifecycle.quantity,
            limit_price=None,
            base_asset=rules.base_asset,
            quote_asset=rules.quote_asset,
        )
        if lifecycle.protection_id is not None:
            protection = self._find(lifecycle.protection_id, protection_context)
            if protection is not None and protection.status == OrderStatus.FILLED:
                return protection
            if protection is not None and protection.status == OrderStatus.PARTIALLY_FILLED:
                raise BinanceManualIntervention("保护单部分成交，冻结自动退出以避免重复卖出")
            if protection is not None and protection.status == OrderStatus.NEW:
                canceled = self.cancel_remaining(protection)
                if canceled.status != OrderStatus.CANCELED:
                    raise BinanceUnknownExecution("保护单撤销状态不确定")
                if canceled.fills:
                    raise BinanceManualIntervention(
                        "保护单撤销期间发生成交，冻结自动退出以避免重复卖出"
                    )
        quantity = rules.quantity(
            lifecycle.quantity,
            market=True,
            reference_price=market.bid,
        )
        context = _OrderContext(
            cycle_id=lifecycle.cycle_id,
            intent_id=lifecycle.intent_id,
            symbol=lifecycle.symbol,
            side=Side.SELL,
            order_type=OrderType.MARKET,
            requested_quantity=quantity,
            limit_price=None,
            base_asset=rules.base_asset,
            quote_asset=rules.quote_asset,
        )
        client_order_id = exit_client_order_id(lifecycle, reason)
        existing = self._find(client_order_id, context)
        if existing is not None:
            return existing
        parameters = {
            "symbol": lifecycle.symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": _decimal_text(quantity),
            "newClientOrderId": client_order_id,
            "newOrderRespType": "FULL",
        }
        return self._submit_with_recovery(parameters, client_order_id, context)

    def rules(self, symbol: str) -> SymbolRules:
        if symbol not in self._rules:
            rules = SymbolRules.from_exchange_info(self._client.exchange_info(symbol))
            if rules.quote_asset != self._quote_asset:
                raise ValueError("交易品种报价资产不在 Testnet 白名单")
            self._rules[symbol] = rules
        return self._rules[symbol]

    def _submit_with_recovery(
        self,
        parameters: Mapping[str, Any],
        client_order_id: str,
        context: _OrderContext,
    ) -> Order:
        try:
            raw = self._client.new_order(parameters)
        except BinanceTransportError as exc:
            recovered = self._find(client_order_id, context)
            if recovered is None:
                raise BinanceUnknownExecution("提交超时且按 clientOrderId 无法确认状态") from exc
            return recovered
        return self._from_raw(raw, context)

    def _find(self, client_order_id: str, context: _OrderContext) -> Order | None:
        raw = self._client.get_order(
            symbol=context.symbol,
            client_order_id=client_order_id,
        )
        return self._from_raw(raw, context) if raw is not None else None

    def _from_raw(self, raw: Mapping[str, Any], context: _OrderContext) -> Order:
        order_id = str(raw["orderId"])
        fills_raw = raw.get("fills")
        if not fills_raw and Decimal(str(raw.get("executedQty", "0"))) > 0:
            fills_raw = self._client.trades(symbol=context.symbol, order_id=order_id)
        event_time_ms = int(raw.get("transactTime") or raw.get("updateTime") or 0)
        fills = tuple(
            self._fill(
                item,
                order_id=order_id,
                event_time_ms=event_time_ms,
                context=context,
            )
            for item in (fills_raw or [])
        )
        return Order(
            order_id=order_id,
            client_order_id=str(raw.get("clientOrderId") or raw.get("origClientOrderId")),
            cycle_id=context.cycle_id,
            intent_id=context.intent_id,
            symbol=context.symbol,
            side=context.side,
            order_type=context.order_type,
            requested_quantity=context.requested_quantity,
            limit_price=context.limit_price,
            status=_order_status(str(raw["status"])),
            fills=fills,
        )

    @staticmethod
    def _fill(
        raw: Mapping[str, Any],
        *,
        order_id: str,
        event_time_ms: int,
        context: _OrderContext,
    ) -> Fill:
        price = Decimal(str(raw["price"]))
        quantity = Decimal(str(raw.get("qty") or raw.get("quantity")))
        commission = Decimal(str(raw.get("commission", "0")))
        commission_asset = str(raw.get("commissionAsset", context.quote_asset))
        if commission_asset == context.quote_asset:
            fee = commission
        elif commission_asset == context.base_asset:
            fee = commission * price
            if context.side == Side.BUY:
                quantity -= commission
        elif commission == 0:
            fee = Decimal("0")
        else:
            raise ValueError("暂不支持第三资产手续费，必须冻结并人工处理")
        fill_id = str(raw.get("id") or raw.get("tradeId") or stable_id("fill", order_id, raw))
        timestamp = int(raw.get("time") or event_time_ms)
        if timestamp <= 0:
            timestamp = int(time.time() * 1000)
        return Fill(
            fill_id=fill_id,
            order_id=order_id,
            event_time=datetime.fromtimestamp(timestamp / 1000, tz=UTC),
            price=price,
            quantity=quantity,
            fee=fee,
        )

    def _context_from_order(self, order: Order) -> _OrderContext:
        rules = self.rules(order.symbol)
        return _OrderContext(
            cycle_id=order.cycle_id,
            intent_id=order.intent_id,
            symbol=order.symbol,
            side=order.side,
            order_type=order.order_type,
            requested_quantity=order.requested_quantity,
            limit_price=order.limit_price,
            base_asset=rules.base_asset,
            quote_asset=rules.quote_asset,
        )


def protection_client_order_id(lifecycle: PositionLifecycle) -> str:
    return stable_id(
        "protect",
        lifecycle.position_id,
        lifecycle.stop_price,
        lifecycle.quantity,
    )[:36]


def _encode(parameters: Mapping[str, Any]) -> str:
    return urlencode([(key, str(value)) for key, value in parameters.items()])


def _floor_step(value: Decimal, step: Decimal) -> Decimal:
    return floor_to_step(value, step)


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _order_status(value: str) -> OrderStatus:
    mapping = {
        "NEW": OrderStatus.NEW,
        "PENDING_NEW": OrderStatus.NEW,
        "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
        "FILLED": OrderStatus.FILLED,
        "CANCELED": OrderStatus.CANCELED,
        "REJECTED": OrderStatus.REJECTED,
        "EXPIRED": OrderStatus.EXPIRED,
        "EXPIRED_IN_MATCH": OrderStatus.EXPIRED,
    }
    return mapping.get(value, OrderStatus.UNKNOWN)


class BinanceTradingStateSource:
    """用 Testnet 账户与订单重建远端真相，只投影本系统管理的库存。"""

    def __init__(
        self,
        client: BinanceTestnetClient,
        exchange: BinanceTestnetExchange,
        local: TradingStateSource,
        *,
        symbols: tuple[str, ...],
        quote_asset: str,
    ) -> None:
        self._client = client
        self._exchange = exchange
        self._local = local
        self._symbols = symbols
        self._quote_asset = quote_asset

    def snapshot(self, *, as_of: datetime) -> TradingStateSnapshot:
        local = self._local.snapshot(as_of=as_of)
        raw_account = self._client.account()
        balances = {item["asset"]: item for item in raw_account.get("balances", [])}
        quote = balances.get(self._quote_asset, {"free": "0", "locked": "0"})
        local_positions = {item.symbol: item for item in local.account.positions}
        positions: list[Position] = []
        for symbol, previous in local_positions.items():
            if symbol not in self._symbols:
                continue
            rules = self._exchange.rules(symbol)
            raw_balance = balances.get(rules.base_asset, {"free": "0", "locked": "0"})
            available = Decimal(str(raw_balance["free"])) + Decimal(str(raw_balance["locked"]))
            quantity = min(previous.quantity, available)
            if quantity <= 0:
                continue
            positions.append(
                Position(
                    symbol=symbol,
                    quantity=quantity,
                    average_price=previous.average_price,
                )
            )
        known_orders: dict[str, Order] = {}
        for order in local.orders:
            remote = self._exchange.query_known_order(order)
            if remote is not None:
                known_orders[remote.client_order_id] = remote
        for symbol in self._symbols:
            for raw in self._client.open_orders(symbol):
                client_order_id = str(raw["clientOrderId"])
                if client_order_id not in known_orders:
                    known_orders[client_order_id] = self._exchange.order_from_remote(raw)
        account = AccountSnapshot(
            cycle_id=stable_id("binance_testnet_account", as_of.isoformat()),
            as_of=as_of,
            observed_at=as_of,
            quote_balance=Decimal(str(quote["free"])),
            positions=tuple(sorted(positions, key=lambda item: item.symbol)),
            open_order_count=sum(
                item.status in {OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED}
                for item in known_orders.values()
            ),
            daily_pnl=local.account.daily_pnl,
            drawdown_fraction=local.account.drawdown_fraction,
            equity=local.account.equity,
            equity_high_water=local.account.equity_high_water,
            kill_switch_active=local.account.kill_switch_active,
            reconciled=True,
        )
        orders = tuple(sorted(known_orders.values(), key=lambda item: item.client_order_id))
        payload = {
            "as_of": as_of.isoformat(),
            "account": account.model_dump(mode="json"),
            "orders": [item.model_dump(mode="json") for item in orders],
        }
        return TradingStateSnapshot(
            snapshot_id=stable_id("binance_testnet_state", content_hash(payload)),
            as_of=as_of,
            observed_at=as_of,
            account=account,
            orders=orders,
        )


def assemble_binance_testnet(
    config: AppConfig,
) -> tuple[BinanceTestnetClient, BinanceTestnetExchange]:
    deployment = config.deployment
    if (
        deployment.stage != DeploymentStage.TESTNET
        or not deployment.testnet_order_submission_enabled
        or deployment.live_order_submission_enabled
        or deployment.credential_profile != "env:INVESTMENT_MANAGER_BINANCE"
        or not deployment.manual_approval_ref
    ):
        raise ValueError("Binance Testnet 执行门禁未完整通过")
    credentials = BinanceCredentials.from_environment(
        config.binance_testnet,
        require_order_submission=True,
    )
    client = BinanceTestnetClient(config.binance_testnet, credentials)
    return client, BinanceTestnetExchange(
        client,
        quote_asset=config.binance_testnet.quote_asset,
    )
