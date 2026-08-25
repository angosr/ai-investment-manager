from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

import httpx
from websockets.asyncio.client import connect

from investment_manager.governance.policy import DeploymentStage
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.market.models import (
    ClosedMarketBar,
    MarketEvent,
    MarketQuote,
    MarketTrade,
)
from investment_manager.market.perpetual.client import BinanceUsdmRestClient
from investment_manager.market.perpetual.service import (
    BinancePerpetualMarketService,
    PerpetualRefreshResult,
)
from investment_manager.market.policy import MarketDataPolicy
from investment_manager.market.repository import MarketDataStore
from investment_manager.scheduling.models import AnalysisTriggerType, build_trigger_event
from investment_manager.settings import AppConfig

logger = logging.getLogger(__name__)


def _from_milliseconds(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=UTC)


class TriggerSink(Protocol):
    def record_trigger(self, trigger) -> bool: ...


@dataclass(slots=True)
class _MarketShockSecond:
    second: int
    low_price: Decimal
    high_price: Decimal


@dataclass(slots=True)
class _MarketShockWindow:
    seconds: deque[_MarketShockSecond]
    low_price: Decimal
    high_price: Decimal
    last_triggered_at: datetime | None = None


class MarketShockDetector:
    """以内存有界的滚动秒级窗口检测冲击；同窗口冷却并从当前价重新定基。"""

    def __init__(
        self,
        *,
        pipeline_id: str,
        relative_move_threshold: Decimal,
        window_seconds: int,
        trigger_expiry_seconds: int,
        sink: TriggerSink,
        analysis_owner_symbol: str | None = None,
        trigger_symbols: tuple[str, ...] | None = None,
    ) -> None:
        if relative_move_threshold <= 0:
            raise ValueError("市场冲击阈值必须为正数")
        if window_seconds < 1:
            raise ValueError("市场冲击窗口必须为正数")
        self._pipeline_id = pipeline_id
        self._threshold = relative_move_threshold
        self._window_seconds = window_seconds
        self._trigger_expiry_seconds = trigger_expiry_seconds
        self._sink = sink
        self._analysis_owner_symbol = analysis_owner_symbol
        if trigger_symbols is not None and len(set(trigger_symbols)) != len(trigger_symbols):
            raise ValueError("市场冲击触发品种必须唯一")
        self._trigger_symbols = None if trigger_symbols is None else frozenset(trigger_symbols)
        self._windows: dict[str, _MarketShockWindow] = {}

    def observe(self, event: MarketEvent) -> bool:
        if self._trigger_symbols is not None and event.symbol not in self._trigger_symbols:
            return False
        if isinstance(event, MarketQuote):
            return False
        if isinstance(event, ClosedMarketBar):
            state = self._windows.get(event.symbol)
            if (
                state is not None
                and state.last_triggered_at is not None
                and event.open_time <= state.last_triggered_at <= event.close_time
            ):
                return False
            relative_move = max(
                abs(event.high / event.open - 1),
                abs(event.low / event.open - 1),
            )
            dedup_key = f"rolling-bar-v2:{event.interval}:{event.open_time.isoformat()}"
            occurred_at = min(event.close_time, event.observed_at)
            observed_at = event.observed_at
        else:
            occurred_at = min(event.event_time, event.observed_at)
            observed_at = event.observed_at
            state = self._windows.get(event.symbol)
            if state is None:
                self._windows[event.symbol] = self._new_window(occurred_at, event.price)
                return False
            relative_move = self._observe_trade(state, occurred_at, event.price)
            if relative_move is None:
                return False
            dedup_key = (
                f"rolling-trade-v2:{self._window_seconds}:{event.aggregate_trade_id}"
            )
        if relative_move < self._threshold:
            return False
        state = self._windows.get(event.symbol)
        if (
            state is not None
            and state.last_triggered_at is not None
            and occurred_at - state.last_triggered_at < timedelta(seconds=self._window_seconds)
        ):
            reference_price = event.close if isinstance(event, ClosedMarketBar) else event.price
            self._windows[event.symbol] = self._new_window(
                observed_at if isinstance(event, ClosedMarketBar) else occurred_at,
                reference_price,
                last_triggered_at=state.last_triggered_at,
            )
            return False
        priority = min(100, max(1, int(relative_move / self._threshold * 80)))
        trigger = build_trigger_event(
            trigger_type=AnalysisTriggerType.MARKET_SHOCK,
            symbol=self._analysis_owner_symbol or event.symbol,
            pipeline_id=self._pipeline_id,
            occurred_at=occurred_at,
            observed_at=observed_at,
            priority=priority,
            dedup_key=dedup_key,
            affected_symbols=(event.symbol,),
            expires_at=observed_at + timedelta(seconds=self._trigger_expiry_seconds),
        )
        inserted = self._sink.record_trigger(trigger)
        reference_price = event.close if isinstance(event, ClosedMarketBar) else event.price
        reset_at = observed_at if isinstance(event, ClosedMarketBar) else occurred_at
        self._windows[event.symbol] = self._new_window(
            reset_at,
            reference_price,
            last_triggered_at=occurred_at,
        )
        return inserted

    def _observe_trade(
        self,
        state: _MarketShockWindow,
        occurred_at: datetime,
        price: Decimal,
    ) -> Decimal | None:
        second = int(require_utc(occurred_at).timestamp())
        current = state.seconds[-1]
        if second < current.second:
            return None
        if second > current.second:
            state.seconds.append(
                _MarketShockSecond(
                    second=second,
                    low_price=price,
                    high_price=price,
                )
            )
            cutoff = second - self._window_seconds
            while state.seconds[0].second < cutoff:
                state.seconds.popleft()
            state.low_price = min(item.low_price for item in state.seconds)
            state.high_price = max(item.high_price for item in state.seconds)
        else:
            current.low_price = min(current.low_price, price)
            current.high_price = max(current.high_price, price)
            state.low_price = min(state.low_price, price)
            state.high_price = max(state.high_price, price)
        return max(
            abs(price / state.low_price - 1),
            abs(price / state.high_price - 1),
        )

    @staticmethod
    def _new_window(
        at: datetime,
        price: Decimal,
        *,
        last_triggered_at: datetime | None = None,
    ) -> _MarketShockWindow:
        second = int(require_utc(at).timestamp())
        sample = _MarketShockSecond(
            second=second,
            low_price=price,
            high_price=price,
        )
        return _MarketShockWindow(
            seconds=deque((sample,)),
            low_price=price,
            high_price=price,
            last_triggered_at=last_triggered_at,
        )


class BinanceMessageParser:
    def parse(self, payload: str | bytes, *, observed_at: datetime) -> MarketEvent | None:
        observed_at = require_utc(observed_at)
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        raw = json.loads(payload)
        if not isinstance(raw, dict):
            raise ValueError("Binance WebSocket 消息必须是对象")
        stream = raw.get("stream")
        data = raw.get("data", raw)
        if not isinstance(data, dict):
            raise ValueError("Binance WebSocket data 必须是对象")
        if not isinstance(stream, str):
            event_type = data.get("e")
            stream = str(event_type or "")

        symbol = str(data.get("s", "")).upper()
        if not symbol:
            raise ValueError("Binance WebSocket 消息缺少 symbol")
        lower_stream = stream.lower()
        if "@bookticker" in lower_stream or ("b" in data and "a" in data and "e" not in data):
            update_id = int(data["u"])
            return MarketQuote(
                quote_id=stable_id("binance_quote", symbol, update_id),
                symbol=symbol,
                observed_at=observed_at,
                bid=Decimal(str(data["b"])),
                bid_quantity=Decimal(str(data["B"])),
                ask=Decimal(str(data["a"])),
                ask_quantity=Decimal(str(data["A"])),
                update_id=update_id,
                source="binance-websocket",
            )
        if data.get("e") == "aggTrade":
            aggregate_trade_id = int(data["a"])
            return MarketTrade(
                trade_id=stable_id("binance_trade", symbol, aggregate_trade_id),
                symbol=symbol,
                aggregate_trade_id=aggregate_trade_id,
                event_time=_from_milliseconds(int(data["T"])),
                observed_at=observed_at,
                price=Decimal(str(data["p"])),
                quantity=Decimal(str(data["q"])),
                buyer_is_maker=bool(data["m"]),
                source="binance-websocket",
            )
        if data.get("e") == "kline":
            kline = data.get("k")
            if not isinstance(kline, dict):
                raise ValueError("Binance kline 消息缺少 k 对象")
            if kline.get("x") is not True:
                return None
            flow_keys = ("q", "V", "Q")
            if any(key in kline for key in flow_keys) and not all(
                key in kline for key in flow_keys
            ):
                raise ValueError("Binance kline WebSocket 现货成交摘要不完整")
            spot_flow = (
                {
                    "quote_volume": Decimal(str(kline["q"])),
                    "taker_buy_base_volume": Decimal(str(kline["V"])),
                    "taker_buy_quote_volume": Decimal(str(kline["Q"])),
                }
                if all(key in kline for key in flow_keys)
                else {}
            )
            return ClosedMarketBar(
                symbol=symbol,
                interval=str(kline["i"]),
                open_time=_from_milliseconds(int(kline["t"])),
                close_time=_from_milliseconds(int(kline["T"])),
                observed_at=observed_at,
                open=Decimal(str(kline["o"])),
                high=Decimal(str(kline["h"])),
                low=Decimal(str(kline["l"])),
                close=Decimal(str(kline["c"])),
                volume=Decimal(str(kline["v"])),
                **spot_flow,
                source="binance-websocket",
            )
        raise ValueError("不支持的 Binance WebSocket 消息类型")


class JsonHttpTransport(Protocol):
    async def get(self, path: str, params: dict[str, Any]) -> Any: ...


@dataclass(slots=True)
class HttpxPublicJsonTransport:
    base_url: str
    timeout_seconds: int
    _client: httpx.AsyncClient | None = field(default=None, init=False, repr=False)

    async def get(self, path: str, params: dict[str, Any]) -> Any:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                follow_redirects=False,
            )
        response = await self._client.get(path, params=params)
        response.raise_for_status()
        return response.json()

    async def aclose(self) -> None:
        if self._client is None:
            return
        await self._client.aclose()
        self._client = None


@dataclass(slots=True)
class BinancePublicRestClient:
    transport: JsonHttpTransport
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    async def fetch_quote(self, symbol: str) -> MarketQuote:
        raw = await self.transport.get("/api/v3/ticker/bookTicker", {"symbol": symbol})
        observed_at = require_utc(self.clock())
        if not isinstance(raw, dict) or str(raw.get("symbol")) != symbol:
            raise ValueError("Binance bookTicker REST 响应非法")
        identity = content_hash({"symbol": symbol, "raw": raw, "at": observed_at.isoformat()})
        return MarketQuote(
            quote_id=stable_id("binance_rest_quote", identity),
            symbol=symbol,
            observed_at=observed_at,
            bid=Decimal(str(raw["bidPrice"])),
            bid_quantity=Decimal(str(raw["bidQty"])),
            ask=Decimal(str(raw["askPrice"])),
            ask_quantity=Decimal(str(raw["askQty"])),
            source="binance-rest",
        )

    async def fetch_latest_trade(self, symbol: str) -> MarketTrade:
        raw = await self.transport.get("/api/v3/aggTrades", {"symbol": symbol, "limit": 1})
        observed_at = require_utc(self.clock())
        if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict):
            raise ValueError("Binance aggTrades REST 响应非法")
        item = raw[0]
        aggregate_trade_id = int(item["a"])
        return MarketTrade(
            trade_id=stable_id("binance_trade", symbol, aggregate_trade_id),
            symbol=symbol,
            aggregate_trade_id=aggregate_trade_id,
            event_time=_from_milliseconds(int(item["T"])),
            observed_at=observed_at,
            price=Decimal(str(item["p"])),
            quantity=Decimal(str(item["q"])),
            buyer_is_maker=bool(item["m"]),
            source="binance-rest",
        )

    async def fetch_closed_bars(
        self, symbol: str, *, interval: str, limit: int
    ) -> tuple[ClosedMarketBar, ...]:
        raw = await self.transport.get(
            "/api/v3/klines",
            {"symbol": symbol, "interval": interval, "limit": limit},
        )
        observed_at = require_utc(self.clock())
        if not isinstance(raw, list):
            raise ValueError("Binance klines REST 响应非法")
        observed_ms = int(observed_at.timestamp() * 1000)
        bars: list[ClosedMarketBar] = []
        for item in raw:
            if not isinstance(item, list) or len(item) < 7:
                raise ValueError("Binance kline REST 条目非法")
            if 7 < len(item) < 11:
                raise ValueError("Binance kline REST 现货成交摘要不完整")
            if int(item[6]) >= observed_ms:
                continue
            spot_flow = (
                {
                    "quote_volume": Decimal(str(item[7])),
                    "taker_buy_base_volume": Decimal(str(item[9])),
                    "taker_buy_quote_volume": Decimal(str(item[10])),
                }
                if len(item) >= 11
                else {}
            )
            bars.append(
                ClosedMarketBar(
                    symbol=symbol,
                    interval=interval,
                    open_time=_from_milliseconds(int(item[0])),
                    close_time=_from_milliseconds(int(item[6])),
                    observed_at=observed_at,
                    open=Decimal(str(item[1])),
                    high=Decimal(str(item[2])),
                    low=Decimal(str(item[3])),
                    close=Decimal(str(item[4])),
                    volume=Decimal(str(item[5])),
                    **spot_flow,
                    source="binance-rest",
                )
            )
        return tuple(bars)


@dataclass(slots=True)
class MarketBootstrapper:
    client: BinancePublicRestClient
    store: MarketDataStore
    policy: MarketDataPolicy

    async def refresh(self) -> None:
        async def refresh_symbol(symbol: str) -> None:
            quote, trade, bars = await asyncio.gather(
                self.client.fetch_quote(symbol),
                self.client.fetch_latest_trade(symbol),
                self.client.fetch_closed_bars(
                    symbol,
                    interval=self.policy.interval,
                    limit=self.policy.bar_window + 1,
                ),
            )
            self.store.put_quote(quote)
            self.store.put_trade(trade)
            for bar in bars:
                self.store.put_bar(bar)

        await asyncio.gather(*(refresh_symbol(symbol) for symbol in self.policy.symbols))


class WebSocketConnector(Protocol):
    @asynccontextmanager
    async def open(self) -> AsyncIterator[AsyncIterator[str | bytes]]: ...


@dataclass(slots=True)
class BinanceWebSocketConnector:
    policy: MarketDataPolicy

    @property
    def uri(self) -> str:
        streams = [
            stream
            for symbol in self.policy.symbols
            for stream in (
                f"{symbol.lower()}@bookTicker",
                f"{symbol.lower()}@aggTrade",
                f"{symbol.lower()}@kline_{self.policy.interval}",
            )
        ]
        return f"{self.policy.websocket_base_url}/stream?streams={'/'.join(streams)}"

    @asynccontextmanager
    async def open(self) -> AsyncIterator[AsyncIterator[str | bytes]]:
        async with connect(
            self.uri,
            open_timeout=self.policy.rest_timeout_seconds,
            ping_interval=None,
            close_timeout=5,
            max_size=1_048_576,
        ) as socket:
            yield socket.__aiter__()


@dataclass(slots=True)
class MarketStreamHealth:
    connect_count: int = 0
    reconnect_count: int = 0
    message_count: int = 0
    last_message_at: datetime | None = None
    last_error_class: str | None = None
    persisted_count: int = 0


class BinanceMarketStreamService:
    """由进程监督器承载的持续连接；流程状态不进入 Temporal。"""

    def __init__(
        self,
        *,
        policy: MarketDataPolicy,
        bootstrapper: MarketBootstrapper,
        connector: WebSocketConnector,
        parser: BinanceMessageParser,
        store: MarketDataStore,
        market_observer: Callable[[MarketEvent], bool] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._policy = policy
        self._bootstrapper = bootstrapper
        self._connector = connector
        self._parser = parser
        self._store = store
        self._market_observer = market_observer
        self._clock = clock
        self._last_persisted_at: dict[tuple[str, str], datetime] = {}
        self.health = MarketStreamHealth()

    async def run(self, stop: asyncio.Event) -> None:
        backoff = self._policy.reconnect_initial_seconds
        while not stop.is_set():
            try:
                await self._bootstrapper.refresh()
                async with self._connector.open() as messages:
                    self.health.connect_count += 1
                    backoff = self._policy.reconnect_initial_seconds
                    started = asyncio.get_running_loop().time()
                    while not stop.is_set():
                        age = asyncio.get_running_loop().time() - started
                        if age >= self._policy.planned_reconnect_seconds:
                            break
                        timeout = min(
                            self._policy.stream_silence_seconds,
                            self._policy.planned_reconnect_seconds - age,
                        )
                        payload = await asyncio.wait_for(anext(messages), timeout=timeout)
                        observed_at = require_utc(self._clock())
                        event = self._parser.parse(payload, observed_at=observed_at)
                        self.health.message_count += 1
                        self.health.last_message_at = observed_at
                        self.health.last_error_class = None
                        if event is None:
                            continue
                        self._process_event(event)
            except asyncio.CancelledError:
                raise
            except (Exception, StopAsyncIteration) as exc:
                if self.health.last_error_class != type(exc).__name__:
                    logger.exception("market stream disconnected")
                self.health.last_error_class = type(exc).__name__
            if stop.is_set():
                break
            self.health.reconnect_count += 1
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            backoff = min(backoff * 2, self._policy.reconnect_maximum_seconds)

    def _store_event(self, event: MarketEvent) -> bool:
        if isinstance(event, MarketQuote):
            if not self._persistence_due(
                "quote",
                event.symbol,
                event.observed_at,
                self._policy.quote_persist_interval_ms,
            ):
                return False
            return self._store.put_quote(event)
        elif isinstance(event, MarketTrade):
            if not self._persistence_due(
                "trade",
                event.symbol,
                event.observed_at,
                self._policy.trade_persist_interval_ms,
            ):
                return False
            return self._store.put_trade(event)
        else:
            return self._store.put_bar(event)

    def _process_event(self, event: MarketEvent) -> None:
        if self._store_event(event):
            self.health.persisted_count += 1
        if self._market_observer is not None:
            self._market_observer(event)

    def _persistence_due(
        self,
        event_type: str,
        symbol: str,
        observed_at: datetime,
        interval_ms: int,
    ) -> bool:
        key = (event_type, symbol)
        previous = self._last_persisted_at.get(key)
        if previous is not None and observed_at - previous < timedelta(milliseconds=interval_ms):
            return False
        self._last_persisted_at[key] = observed_at
        return True


@dataclass(slots=True)
class MarketRuntime:
    spot: BinanceMarketStreamService
    perpetual: BinancePerpetualMarketService | None = None
    _transports: tuple[HttpxPublicJsonTransport, ...] = ()

    async def run(self, stop: asyncio.Event) -> None:
        try:
            if self.perpetual is None:
                await self.spot.run(stop)
                return
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(self.spot.run(stop))
                tasks.create_task(self.perpetual.run(stop))
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        await asyncio.gather(*(transport.aclose() for transport in self._transports))


def assemble_shadow_market_stream(
    config: AppConfig,
    store: MarketDataStore,
    *,
    market_observer: Callable[[MarketEvent], bool] | None = None,
    perpetual_refresh_observer: Callable[[PerpetualRefreshResult], None]
    | None = None,
) -> MarketRuntime:
    if (
        config.deployment.stage not in {DeploymentStage.SHADOW, DeploymentStage.TESTNET}
        or not config.deployment.shadow_market_data_enabled
    ):
        raise ValueError("Binance 公开行情服务只允许在 SHADOW 或 TESTNET 阶段启动")
    policy = config.market_data
    transport = HttpxPublicJsonTransport(policy.rest_base_url, policy.rest_timeout_seconds)
    client = BinancePublicRestClient(transport)
    spot = BinanceMarketStreamService(
        policy=policy,
        bootstrapper=MarketBootstrapper(client, store, policy),
        connector=BinanceWebSocketConnector(policy),
        parser=BinanceMessageParser(),
        store=store,
        market_observer=market_observer,
    )
    perpetual = None
    transports = [transport]
    if policy.perpetual_instruments:
        perpetual_transport = HttpxPublicJsonTransport(
            policy.perpetual_rest_base_url,
            policy.rest_timeout_seconds,
        )
        transports.append(perpetual_transport)
        perpetual = BinancePerpetualMarketService(
            policy=policy,
            client=BinanceUsdmRestClient(perpetual_transport),
            store=store,
            refresh_observer=perpetual_refresh_observer,
        )
    return MarketRuntime(
        spot=spot,
        perpetual=perpetual,
        _transports=tuple(transports),
    )
