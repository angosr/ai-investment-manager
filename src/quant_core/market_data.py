from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

import httpx
from pydantic import Field, field_validator, model_validator
from websockets.asyncio.client import connect

from quant_core.config import AppConfig, DeploymentStage, MarketDataPolicy
from quant_core.domain import (
    FrozenModel,
    MarketBar,
    MarketSnapshot,
    Money,
    PositiveDecimal,
    _require_utc,
)
from quant_core.ids import content_hash, stable_id
from quant_core.trigger import AnalysisTriggerType, build_trigger_event

logger = logging.getLogger(__name__)


def _from_milliseconds(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=UTC)


class MarketQuote(FrozenModel):
    quote_id: str
    symbol: str
    observed_at: datetime
    bid: PositiveDecimal
    bid_quantity: Money
    ask: PositiveDecimal
    ask_quantity: Money
    update_id: int | None = Field(default=None, ge=0)
    source: str

    _utc_observed_at = field_validator("observed_at")(_require_utc)

    @model_validator(mode="after")
    def ask_must_not_be_below_bid(self):
        if self.ask < self.bid:
            raise ValueError("ask 不能低于 bid")
        return self


class MarketTrade(FrozenModel):
    trade_id: str
    symbol: str
    aggregate_trade_id: int = Field(ge=0)
    event_time: datetime
    observed_at: datetime
    price: PositiveDecimal
    quantity: PositiveDecimal
    buyer_is_maker: bool
    source: str

    _utc_event_time = field_validator("event_time")(_require_utc)
    _utc_observed_at = field_validator("observed_at")(_require_utc)


class ClosedMarketBar(FrozenModel):
    symbol: str
    interval: str
    open_time: datetime
    close_time: datetime
    observed_at: datetime
    open: PositiveDecimal
    high: PositiveDecimal
    low: PositiveDecimal
    close: PositiveDecimal
    volume: Money
    source: str

    _utc_open_time = field_validator("open_time")(_require_utc)
    _utc_close_time = field_validator("close_time")(_require_utc)
    _utc_observed_at = field_validator("observed_at")(_require_utc)

    @model_validator(mode="after")
    def range_and_times_must_be_valid(self):
        if self.close_time <= self.open_time:
            raise ValueError("K 线 close_time 必须晚于 open_time")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("K 线 OHLC 范围非法")
        return self

    def to_market_bar(self) -> MarketBar:
        return MarketBar(
            event_time=self.open_time,
            observed_at=self.observed_at,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
        )


MarketEvent = MarketQuote | MarketTrade | ClosedMarketBar


class TriggerSink(Protocol):
    def record_trigger(self, trigger) -> bool: ...


@dataclass(slots=True)
class _MarketShockWindow:
    bucket: int
    low_price: Decimal
    high_price: Decimal
    last_price: Decimal
    triggered: bool = False


class MarketShockDetector:
    """以配置 K 线边界实时检测窗口内冲击；每品种每窗口最多触发一次。"""

    def __init__(
        self,
        *,
        pipeline_id: str,
        relative_move_threshold: Decimal,
        window_seconds: int,
        trigger_expiry_seconds: int,
        sink: TriggerSink,
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
        self._windows: dict[str, _MarketShockWindow] = {}

    def observe(self, event: MarketEvent) -> bool:
        if isinstance(event, MarketQuote):
            return False
        if isinstance(event, ClosedMarketBar):
            relative_move = max(
                abs(event.high / event.open - 1),
                abs(event.low / event.open - 1),
            )
            dedup_key = f"bar:{event.interval}:{event.open_time.isoformat()}"
            occurred_at = min(event.close_time, event.observed_at)
            observed_at = event.observed_at
            bucket = self._bucket(event.open_time)
            state = self._windows.get(event.symbol)
            if state is not None and state.bucket == bucket and state.triggered:
                return False
        else:
            occurred_at = min(event.event_time, event.observed_at)
            observed_at = event.observed_at
            bucket = self._bucket(occurred_at)
            state = self._windows.get(event.symbol)
            if state is None:
                self._windows[event.symbol] = _MarketShockWindow(
                    bucket=bucket,
                    low_price=event.price,
                    high_price=event.price,
                    last_price=event.price,
                )
                return False
            if bucket > state.bucket:
                relative_move = abs(event.price / state.last_price - 1)
                state = _MarketShockWindow(
                    bucket=bucket,
                    low_price=event.price,
                    high_price=event.price,
                    last_price=event.price,
                )
                self._windows[event.symbol] = state
            elif bucket < state.bucket:
                return False
            else:
                relative_move = max(
                    abs(event.price / state.low_price - 1),
                    abs(event.price / state.high_price - 1),
                )
                state.low_price = min(state.low_price, event.price)
                state.high_price = max(state.high_price, event.price)
                state.last_price = event.price
            if state.triggered:
                return False
            dedup_key = f"trade-window-v1:{self._window_seconds}:{bucket}"
        if relative_move < self._threshold:
            return False
        priority = min(100, max(1, int(relative_move / self._threshold * 80)))
        trigger = build_trigger_event(
            trigger_type=AnalysisTriggerType.MARKET_SHOCK,
            symbol=event.symbol,
            pipeline_id=self._pipeline_id,
            occurred_at=occurred_at,
            observed_at=observed_at,
            priority=priority,
            dedup_key=dedup_key,
            expires_at=observed_at + timedelta(seconds=self._trigger_expiry_seconds),
        )
        inserted = self._sink.record_trigger(trigger)
        state = self._windows.get(event.symbol)
        if state is None or bucket > state.bucket:
            self._windows[event.symbol] = _MarketShockWindow(
                bucket=bucket,
                low_price=event.low if isinstance(event, ClosedMarketBar) else event.price,
                high_price=event.high if isinstance(event, ClosedMarketBar) else event.price,
                last_price=event.close if isinstance(event, ClosedMarketBar) else event.price,
                triggered=True,
            )
        elif state.bucket == bucket:
            state.triggered = True
        return inserted

    def _bucket(self, at: datetime) -> int:
        return int(_require_utc(at).timestamp()) // self._window_seconds


class MarketDataStore(Protocol):
    def put_quote(self, quote: MarketQuote) -> bool: ...

    def put_trade(self, trade: MarketTrade) -> bool: ...

    def put_bar(self, bar: ClosedMarketBar) -> bool: ...

    def latest_trade(self, *, symbol: str, as_of: datetime) -> MarketTrade: ...

    def snapshot(
        self,
        *,
        cycle_id: str,
        symbol: str,
        interval: str,
        as_of: datetime,
        bar_window: int,
        source: str,
    ) -> MarketSnapshot: ...


def _bar_market_facts(bar: ClosedMarketBar) -> dict[str, Any]:
    return bar.model_dump(exclude={"observed_at", "source"}, mode="json")


def _quote_market_facts(quote: MarketQuote) -> dict[str, Any]:
    return quote.model_dump(exclude={"observed_at", "source"}, mode="json")


def _trade_market_facts(trade: MarketTrade) -> dict[str, Any]:
    return trade.model_dump(exclude={"observed_at", "source"}, mode="json")


@dataclass(slots=True)
class InMemoryMarketDataStore:
    _quotes: dict[str, MarketQuote] = field(default_factory=dict)
    _trades: dict[tuple[str, int], MarketTrade] = field(default_factory=dict)
    _bars: dict[tuple[str, str, datetime], ClosedMarketBar] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def put_quote(self, quote: MarketQuote) -> bool:
        with self._lock:
            existing = self._quotes.get(quote.quote_id)
            if existing is not None:
                if _quote_market_facts(existing) != _quote_market_facts(quote):
                    raise ValueError("quote_id 冲突且事实不一致")
                return False
            self._quotes[quote.quote_id] = quote
            return True

    def put_trade(self, trade: MarketTrade) -> bool:
        key = (trade.symbol, trade.aggregate_trade_id)
        with self._lock:
            existing = self._trades.get(key)
            if existing is not None:
                if _trade_market_facts(existing) != _trade_market_facts(trade):
                    raise ValueError("aggregate_trade_id 冲突且事实不一致")
                return False
            self._trades[key] = trade
            return True

    def put_bar(self, bar: ClosedMarketBar) -> bool:
        key = (bar.symbol, bar.interval, bar.open_time)
        with self._lock:
            existing = self._bars.get(key)
            if existing is not None:
                if _bar_market_facts(existing) != _bar_market_facts(bar):
                    raise ValueError("已收盘 K 线唯一键冲突且事实不一致")
                return False
            self._bars[key] = bar
            return True

    def latest_trade(self, *, symbol: str, as_of: datetime) -> MarketTrade:
        as_of = _require_utc(as_of)
        with self._lock:
            visible = [
                item
                for item in self._trades.values()
                if item.symbol == symbol and item.observed_at <= as_of and item.event_time <= as_of
            ]
        if not visible:
            raise ValueError(f"{symbol} 缺少可见成交，无法计算组合权益")
        return max(
            visible,
            key=lambda item: (item.event_time, item.observed_at, item.aggregate_trade_id),
        )

    def snapshot(
        self,
        *,
        cycle_id: str,
        symbol: str,
        interval: str,
        as_of: datetime,
        bar_window: int,
        source: str,
    ) -> MarketSnapshot:
        as_of = _require_utc(as_of)
        with self._lock:
            quotes = [
                item
                for item in self._quotes.values()
                if item.symbol == symbol and item.observed_at <= as_of
            ]
            trades = [
                item
                for item in self._trades.values()
                if item.symbol == symbol and item.observed_at <= as_of and item.event_time <= as_of
            ]
            bars = [
                item
                for item in self._bars.values()
                if item.symbol == symbol and item.interval == interval and item.observed_at <= as_of
            ]
        if not quotes or not trades:
            raise ValueError("行情快照缺少可见的报价或成交")
        quote = max(quotes, key=lambda item: (item.observed_at, item.quote_id))
        trade = max(
            trades,
            key=lambda item: (item.event_time, item.observed_at, item.aggregate_trade_id),
        )
        selected_bars = sorted(bars, key=lambda item: item.open_time)[-bar_window:]
        if len(selected_bars) < 2:
            raise ValueError("行情快照至少需要两根已收盘 K 线")
        return MarketSnapshot(
            cycle_id=cycle_id,
            symbol=symbol,
            as_of=as_of,
            observed_at=min(quote.observed_at, trade.observed_at),
            bid=quote.bid,
            ask=quote.ask,
            last=trade.price,
            bars=tuple(item.to_market_bar() for item in selected_bars),
            source=source,
        )


class BinanceMessageParser:
    def parse(self, payload: str | bytes, *, observed_at: datetime) -> MarketEvent | None:
        observed_at = _require_utc(observed_at)
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
                source="binance-websocket",
            )
        raise ValueError("不支持的 Binance WebSocket 消息类型")


class JsonHttpTransport(Protocol):
    async def get(self, path: str, params: dict[str, Any]) -> Any: ...


@dataclass(slots=True)
class HttpxPublicJsonTransport:
    base_url: str
    timeout_seconds: int

    async def get(self, path: str, params: dict[str, Any]) -> Any:
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            follow_redirects=False,
        ) as client:
            response = await client.get(path, params=params)
            response.raise_for_status()
            return response.json()


@dataclass(slots=True)
class BinancePublicRestClient:
    transport: JsonHttpTransport
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    async def fetch_quote(self, symbol: str) -> MarketQuote:
        raw = await self.transport.get("/api/v3/ticker/bookTicker", {"symbol": symbol})
        observed_at = _require_utc(self.clock())
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
        observed_at = _require_utc(self.clock())
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
        observed_at = _require_utc(self.clock())
        if not isinstance(raw, list):
            raise ValueError("Binance klines REST 响应非法")
        observed_ms = int(observed_at.timestamp() * 1000)
        bars: list[ClosedMarketBar] = []
        for item in raw:
            if not isinstance(item, list) or len(item) < 7:
                raise ValueError("Binance kline REST 条目非法")
            if int(item[6]) >= observed_ms:
                continue
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
                        observed_at = _require_utc(self._clock())
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


def assemble_shadow_market_stream(
    config: AppConfig,
    store: MarketDataStore,
    *,
    market_observer: Callable[[MarketEvent], bool] | None = None,
) -> BinanceMarketStreamService:
    if (
        config.deployment.stage not in {DeploymentStage.SHADOW, DeploymentStage.TESTNET}
        or not config.deployment.shadow_market_data_enabled
    ):
        raise ValueError("Binance 公开行情服务只允许在 SHADOW 或 TESTNET 阶段启动")
    policy = config.market_data
    transport = HttpxPublicJsonTransport(policy.rest_base_url, policy.rest_timeout_seconds)
    client = BinancePublicRestClient(transport)
    return BinanceMarketStreamService(
        policy=policy,
        bootstrapper=MarketBootstrapper(client, store, policy),
        connector=BinanceWebSocketConnector(policy),
        parser=BinanceMessageParser(),
        store=store,
        market_observer=market_observer,
    )
