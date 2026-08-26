"""Minimal Binance Spot Testnet credentials, REST client, and symbol rules."""

from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from investment_manager.execution.policy import BinanceTestnetPolicy
from investment_manager.kernel.types import floor_to_step


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
        if status_code >= 400 or (
            isinstance(payload, dict) and int(payload.get("code", 0)) < 0
        ):
            code = (
                int(payload["code"])
                if isinstance(payload, dict) and "code" in payload
                else None
            )
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
        normalized = floor_to_step(value, step)
        if normalized < minimum or normalized > maximum:
            raise ValueError("订单数量不符合 Binance LOT_SIZE")
        notional = normalized * reference_price
        if self.min_notional > 0 and notional < self.min_notional:
            raise ValueError("订单名义金额低于 Binance 最小值")
        if self.max_notional is not None and notional > self.max_notional:
            raise ValueError("订单名义金额高于 Binance 最大值")
        return normalized

    def price(self, value: Decimal) -> Decimal:
        return floor_to_step(value, self.tick_size)


def _encode(parameters: Mapping[str, Any]) -> str:
    return urlencode([(key, str(value)) for key, value in parameters.items()])
