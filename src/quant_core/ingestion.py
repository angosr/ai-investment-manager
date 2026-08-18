from __future__ import annotations

import asyncio
import json
import logging
import threading
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar, Protocol
from zoneinfo import ZoneInfo

import anyio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from pydantic import Field, field_validator

from quant_core.domain import FrozenModel, IntelligenceEvent, _require_utc
from quant_core.ids import content_hash, stable_id

logger = logging.getLogger(__name__)


class RawIntelligenceItem(FrozenModel):
    source_item_id: str
    source: str
    event_time: datetime
    observed_at: datetime
    title: str = Field(min_length=1, max_length=1_000)
    body: str = Field(default="", max_length=20_000)
    url: str | None = Field(default=None, max_length=2_000)
    rank: int | None = Field(default=None, ge=0)

    _utc_event_time = field_validator("event_time")(_require_utc)
    _utc_observed_at = field_validator("observed_at")(_require_utc)


class IntelligenceSource(Protocol):
    source_id: str

    def read(self, *, observed_at: datetime) -> tuple[RawIntelligenceItem, ...]: ...


class McpToolTransport(Protocol):
    def call(self, tool_name: str, arguments: dict[str, Any]) -> Any: ...


class StreamableHttpMcpTransport:
    def __init__(self, url: str, *, timeout_seconds: int = 15) -> None:
        if not url.startswith(("http://127.0.0.1:", "http://localhost:")):
            raise ValueError("MVP MCP 仅允许显式本机回环地址")
        self._url = url
        self._timeout_seconds = timeout_seconds

    def call(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        return anyio.run(self._call, tool_name, arguments)

    async def _call(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        with anyio.fail_after(self._timeout_seconds):
            async with streamablehttp_client(self._url) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments=arguments)
        if result.isError:
            raise RuntimeError(f"MCP tool failed: {tool_name}")
        texts = [getattr(item, "text", None) for item in result.content]
        texts = [item for item in texts if item is not None]
        if len(texts) != 1:
            raise ValueError("MCP 工具必须返回单个文本 JSON 内容块")
        return texts[0]


class TrendRadarMcpSource:
    """只调用固定只读工具；响应解析与 MCP 传输实现分离。"""

    source_id = "trendradar-mcp"

    def __init__(
        self,
        transport: McpToolTransport,
        *,
        platforms: tuple[str, ...] = (),
        limit: int = 100,
        source_timezone: str = "Asia/Shanghai",
    ) -> None:
        if not 1 <= limit <= 1000:
            raise ValueError("TrendRadar limit 必须在 1..1000")
        self._transport = transport
        self._platforms = platforms
        self._limit = limit
        self._timezone = ZoneInfo(source_timezone)

    def read(self, *, observed_at: datetime) -> tuple[RawIntelligenceItem, ...]:
        observed_at = _require_utc(observed_at)
        response = self._transport.call(
            "get_latest_news",
            {
                "platforms": list(self._platforms) or None,
                "limit": self._limit,
                "include_url": True,
            },
        )
        if isinstance(response, str):
            response = json.loads(response)
        if isinstance(response, dict):
            if response.get("success") is not True or not isinstance(response.get("data"), list):
                raise ValueError("TrendRadar get_latest_news 返回失败或缺少 data")
            response = response["data"]
        if not isinstance(response, list):
            raise ValueError("TrendRadar get_latest_news 响应必须是列表")
        items: list[RawIntelligenceItem] = []
        for raw in response:
            if not isinstance(raw, dict):
                raise ValueError("TrendRadar 新闻条目必须是对象")
            title = str(raw.get("title", "")).strip()
            platform = str(raw.get("platform", "unknown")).strip() or "unknown"
            timestamp = str(raw.get("timestamp", "")).strip()
            try:
                event_time = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=self._timezone
                )
            except ValueError as exc:
                raise ValueError("TrendRadar timestamp 格式非法") from exc
            event_time = event_time.astimezone(UTC)
            source_item_id = stable_id("trendradar_item", platform, title, event_time.isoformat())
            items.append(
                RawIntelligenceItem(
                    source_item_id=source_item_id,
                    source=f"trendradar:{platform}",
                    event_time=event_time,
                    observed_at=observed_at,
                    title=title,
                    body=title,
                    url=raw.get("url") or raw.get("mobileUrl") or None,
                    rank=int(raw["rank"]) if raw.get("rank") is not None else None,
                )
            )
        return tuple(items)


class EventNormalizer:
    _symbol_keywords: ClassVar[dict[str, tuple[str, ...]]] = {
        "BTCUSDT": ("bitcoin", "btc", "比特币"),
        "ETHUSDT": ("ethereum", "ether", "eth", "以太坊"),
    }
    _cross_asset_keywords: ClassVar[tuple[str, ...]] = (
        "federal reserve",
        "fed ",
        "cpi",
        "inflation",
        "interest rate",
        "tariff",
        "sanction",
        "sec ",
        "etf",
        "oil",
        "hormuz",
        "美联储",
        "利率",
        "降息",
        "加息",
        "通胀",
        "非农",
        "美元",
        "流动性",
        "关税",
        "制裁",
        "监管",
        "原油",
        "石油",
        "霍尔木兹",
    )
    _critical_cross_asset_keywords: ClassVar[tuple[str, ...]] = (
        "federal reserve",
        "cpi",
        "rate decision",
        "sanction",
        "hormuz",
        "美联储",
        "利率决议",
        "降息",
        "加息",
        "制裁",
        "霍尔木兹",
    )

    def __init__(
        self,
        *,
        version: str = "intelligence-normalizer-v2",
        universe: tuple[str, ...] = ("BTCUSDT", "ETHUSDT"),
        default_source_reliability: Decimal = Decimal("0.60"),
    ) -> None:
        if not universe or len(set(universe)) != len(universe):
            raise ValueError("事件路由 universe 必须非空且不重复")
        self._version = version
        self._universe = universe
        self._default_source_reliability = default_source_reliability

    def normalize(self, item: RawIntelligenceItem) -> IntelligenceEvent | None:
        if item.observed_at < item.event_time:
            raise ValueError("observed_at 不能早于 event_time")
        text = f"{item.title}\n{item.body}".lower()
        symbols = tuple(
            symbol
            for symbol, keywords in self._symbol_keywords.items()
            if symbol in self._universe and any(keyword in text for keyword in keywords)
        )
        relevance = Decimal("1")
        if not symbols:
            if not any(keyword in text for keyword in self._cross_asset_keywords):
                return None
            symbols = self._universe
            relevance = (
                Decimal("0.85")
                if any(keyword in text for keyword in self._critical_cross_asset_keywords)
                else Decimal("0.50")
            )
        rank_component = (
            Decimal("0.5")
            if item.rank is None
            else max(Decimal("0"), Decimal("1") - Decimal(item.rank) / Decimal("100"))
        )
        evidence_id = stable_id(
            "evidence",
            self._version,
            item.source,
            item.source_item_id,
            content_hash({"title": item.title, "body": item.body}),
        )
        return IntelligenceEvent(
            evidence_id=evidence_id,
            event_time=item.event_time,
            observed_at=item.observed_at,
            source=item.source,
            title=item.title,
            body=item.body,
            symbols=symbols,
            relevance=relevance,
            impact=rank_component,
            source_reliability=self._default_source_reliability,
            novelty=Decimal("1"),
        )


class EventStore(Protocol):
    def put(self, event: IntelligenceEvent) -> bool: ...

    def visible(self, *, symbol: str, as_of: datetime) -> tuple[IntelligenceEvent, ...]: ...


@dataclass(slots=True)
class InMemoryEventStore:
    _events: dict[str, IntelligenceEvent] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def put(self, event: IntelligenceEvent) -> bool:
        with self._lock:
            existing = self._events.get(event.evidence_id)
            if existing is not None:
                if existing != event:
                    raise ValueError("相同 evidence_id 的事件事实不一致")
                return False
            self._events[event.evidence_id] = event
            return True

    def visible(self, *, symbol: str, as_of: datetime) -> tuple[IntelligenceEvent, ...]:
        as_of = _require_utc(as_of)
        with self._lock:
            events = [
                item
                for item in self._events.values()
                if symbol in item.symbols and item.observed_at <= as_of
            ]
        return tuple(sorted(events, key=lambda item: (item.event_time, item.evidence_id)))


@dataclass(frozen=True, slots=True)
class CollectionResult:
    read_count: int
    normalized_count: int
    inserted_count: int


class InformationCollector:
    def __init__(
        self,
        sources: tuple[IntelligenceSource, ...],
        normalizer: EventNormalizer,
        store: EventStore,
    ) -> None:
        source_ids = [item.source_id for item in sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("IntelligenceSource source_id 必须唯一")
        self._sources = sources
        self._normalizer = normalizer
        self._store = store

    def collect(self, *, observed_at: datetime) -> CollectionResult:
        read_count = normalized_count = inserted_count = 0
        for source in self._sources:
            for raw in source.read(observed_at=observed_at):
                read_count += 1
                event = self._normalizer.normalize(raw)
                if event is None:
                    continue
                normalized_count += 1
                if self._store.put(event):
                    inserted_count += 1
        return CollectionResult(read_count, normalized_count, inserted_count)


@dataclass(slots=True)
class InformationCollectorHealth:
    collection_count: int = 0
    read_count: int = 0
    inserted_count: int = 0
    last_success_at: datetime | None = None
    last_error_class: str | None = None


class InformationCollectorService:
    """受监督的低频采集角色；Collector 本身保持可单次调用和可测试。"""

    def __init__(
        self,
        collector: InformationCollector,
        *,
        interval_seconds: int,
        clock=lambda: datetime.now(UTC),
    ) -> None:
        if interval_seconds < 1:
            raise ValueError("采集间隔必须为正数")
        self._collector = collector
        self._interval_seconds = interval_seconds
        self._clock = clock
        self.health = InformationCollectorHealth()

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            observed_at = _require_utc(self._clock())
            self.health.collection_count += 1
            try:
                result = await asyncio.to_thread(
                    self._collector.collect,
                    observed_at=observed_at,
                )
                self.health.read_count += result.read_count
                self.health.inserted_count += result.inserted_count
                self.health.last_success_at = observed_at
                self.health.last_error_class = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.health.last_error_class != type(exc).__name__:
                    logger.exception("information collector failed")
                self.health.last_error_class = type(exc).__name__
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._interval_seconds)
