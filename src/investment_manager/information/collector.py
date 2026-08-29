from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any, ClassVar, Protocol
from urllib.parse import urlparse
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from pydantic import Field, field_validator

from investment_manager.information.coverage import build_source_poll_record
from investment_manager.information.models import (
    CausalDomain,
    IntelligenceEvent,
    SourcePollRecord,
    SourcePollStatus,
)
from investment_manager.information.official.document import (
    MAXIMUM_OFFICIAL_DOCUMENT_CHARACTERS,
    OfficialHtmlDocument,
    build_official_decision_excerpt,
    parse_official_html_document,
)
from investment_manager.information.policy import NewsNowEventFeed, OfficialEventFeed
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel

logger = logging.getLogger(__name__)


def _bounded_external_text(value: Any, *, maximum_length: int) -> str:
    """Bound untrusted source text before constructing strict internal facts."""

    return str(value).strip()[:maximum_length]


def _bounded_external_url(value: Any) -> str | None:
    bounded = _bounded_external_text(value, maximum_length=2_000) if value else ""
    return bounded or None


class RawIntelligenceItem(FrozenModel):
    source_item_id: str
    source: str
    acquisition_route: str = "legacy-unknown"
    event_time: datetime
    observed_at: datetime
    title: str = Field(min_length=1, max_length=1_000)
    body: str = Field(
        default="",
        max_length=MAXIMUM_OFFICIAL_DOCUMENT_CHARACTERS,
    )
    decision_excerpt: str = Field(default="", max_length=2_000)
    url: str | None = Field(default=None, max_length=2_000)
    source_reliability: Decimal = Field(default=Decimal("0.60"), ge=0, le=1)
    rank: int | None = Field(default=None, ge=0)
    immediate_review_eligible: bool = False
    directional_support_eligible: bool = False

    _utc_event_time = field_validator("event_time")(require_utc)
    _utc_observed_at = field_validator("observed_at")(require_utc)


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
        observed_at = require_utc(observed_at)
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
            original_title = str(raw.get("title", "")).strip()
            title = _bounded_external_text(original_title, maximum_length=1_000)
            platform = str(raw.get("platform", "unknown")).strip() or "unknown"
            timestamp = str(raw.get("timestamp", "")).strip()
            try:
                event_time = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=self._timezone
                )
            except ValueError as exc:
                raise ValueError("TrendRadar timestamp 格式非法") from exc
            event_time = event_time.astimezone(UTC)
            source_item_id = stable_id(
                "trendradar_item", platform, original_title, event_time.isoformat()
            )
            items.append(
                RawIntelligenceItem(
                    source_item_id=source_item_id,
                    source=f"trendradar:{platform}",
                    acquisition_route="trendradar-mcp-v1",
                    event_time=event_time,
                    observed_at=observed_at,
                    title=title,
                    body=_bounded_external_text(
                        original_title,
                        maximum_length=20_000,
                    ),
                    url=_bounded_external_url(raw.get("url") or raw.get("mobileUrl")),
                    rank=int(raw["rank"]) if raw.get("rank") is not None else None,
                )
            )
        return tuple(items)


class NewsNowTransport(Protocol):
    def fetch(self, source_id: str) -> Any: ...


class HttpxNewsNowTransport:
    """只访问显式回环 NewsNow；不跟随重定向，也不接触新闻正文 URL。"""

    def __init__(self, base_url: str, *, timeout_seconds: int = 15) -> None:
        if not base_url.startswith(("http://127.0.0.1:", "http://localhost:")):
            raise ValueError("NewsNow 仅允许显式本机回环地址")
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def fetch(self, source_id: str) -> Any:
        with httpx.Client(
            base_url=self._base_url,
            timeout=self._timeout_seconds,
            follow_redirects=False,
        ) as client:
            response = client.get("/api/s", params={"id": source_id, "latest": ""})
            response.raise_for_status()
            return response.json()


class NewsNowSource:
    """读取 NewsNow 快速结构化源，并复用 TrendRadar 的平台事实身份去重。"""

    source_id = "newsnow-fast"

    def __init__(
        self,
        transport: NewsNowTransport,
        *,
        feeds: tuple[NewsNowEventFeed, ...],
        maximum_age_seconds: int,
        clock=lambda: datetime.now(UTC),
    ) -> None:
        source_ids = tuple(item.stream_id for item in feeds)
        if not feeds or len(source_ids) != len(set(source_ids)):
            raise ValueError("NewsNow feeds 必须非空且不重复")
        if maximum_age_seconds < 1:
            raise ValueError("NewsNow 快速源最大事件年龄必须为正数")
        self._transport = transport
        self._feeds = feeds
        self._maximum_age = timedelta(seconds=maximum_age_seconds)
        self._clock = clock

    def read(self, *, observed_at: datetime) -> tuple[RawIntelligenceItem, ...]:
        requested_at = require_utc(observed_at)
        items: list[RawIntelligenceItem] = []
        for feed in self._feeds:
            source_id = feed.stream_id
            response = self._transport.fetch(source_id)
            actual_observed_at = max(requested_at, require_utc(self._clock()))
            if not isinstance(response, dict):
                raise ValueError("NewsNow 响应必须是对象")
            if response.get("status") not in {"success", "cache"}:
                raise ValueError("NewsNow 返回失败状态")
            if response.get("id") != source_id or not isinstance(response.get("items"), list):
                raise ValueError("NewsNow 响应 source id 或 items 非法")
            for rank, raw in enumerate(response["items"], start=1):
                if not isinstance(raw, dict):
                    raise ValueError("NewsNow 新闻条目必须是对象")
                original_title = str(raw.get("title", "")).strip()
                if not original_title:
                    raise ValueError("NewsNow 新闻标题不能为空")
                title = _bounded_external_text(
                    original_title,
                    maximum_length=1_000,
                )
                event_time = self._event_time(raw.get("pubDate"))
                if event_time > actual_observed_at:
                    raise ValueError("NewsNow pubDate 晚于实际观测时间")
                if actual_observed_at - event_time > self._maximum_age:
                    continue
                source_item_id = stable_id(
                    "trendradar_item",
                    source_id,
                    original_title,
                    event_time.isoformat(),
                )
                items.append(
                    RawIntelligenceItem(
                        source_item_id=source_item_id,
                        source=f"trendradar:{source_id}",
                        acquisition_route="newsnow-fast-v1",
                        event_time=event_time,
                        observed_at=actual_observed_at,
                        title=title,
                        body=_bounded_external_text(
                            (
                                raw.get("extra", {}).get("hover", original_title)
                                if isinstance(raw.get("extra"), dict)
                                else original_title
                            ),
                            maximum_length=20_000,
                        ),
                        url=_bounded_external_url(raw.get("url") or raw.get("mobileUrl")),
                        rank=rank,
                        immediate_review_eligible=(feed.immediate_review_eligible),
                    )
                )
        return tuple(items)

    @staticmethod
    def _event_time(value: Any) -> datetime:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value / 1000, tz=UTC)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("NewsNow pubDate 格式非法") from exc
            if parsed.tzinfo is None:
                raise ValueError("NewsNow pubDate 必须包含时区")
            return parsed.astimezone(UTC)
        raise ValueError("NewsNow pubDate 缺失或格式非法")


class OfficialRssSource:
    """Read one pinned government release feed as high-provenance event evidence."""

    def __init__(
        self,
        feed: OfficialEventFeed,
        *,
        maximum_age_seconds: int,
        timeout_seconds: int = 15,
        maximum_bytes: int = 2_000_000,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if maximum_age_seconds < 1 or timeout_seconds < 1 or maximum_bytes < 1:
            raise ValueError("official RSS age/timeout/size 必须为正数")
        self.source_id = f"official-rss:{feed.stream_id}"
        self._feed = feed
        self._maximum_age = timedelta(seconds=maximum_age_seconds)
        self._timeout_seconds = timeout_seconds
        self._maximum_bytes = maximum_bytes
        self._transport = transport
        self._validators: dict[str, str] = {}
        self._entry_pattern = (
            re.compile(feed.entry_path_pattern) if feed.entry_path_pattern else None
        )
        self._entry_documents: dict[str, OfficialHtmlDocument] = {}

    def read(self, *, observed_at: datetime) -> tuple[RawIntelligenceItem, ...]:
        observed_at = require_utc(observed_at)
        headers = {
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
            "User-Agent": "investment-manager-official-events/1.0",
            **self._validators,
        }
        with httpx.Client(
            timeout=self._timeout_seconds,
            follow_redirects=False,
            transport=self._transport,
        ) as client:
            response = client.get(self._feed.url, headers=headers)
            if response.status_code == 304:
                return ()
            response.raise_for_status()
            if str(response.url) != self._feed.url:
                raise ValueError("official RSS 响应 URL 与固定请求不一致")
            if not response.content or len(response.content) > self._maximum_bytes:
                raise ValueError("official RSS 响应为空或超过大小上限")
            self._validators = {
                name: value
                for name, value in (
                    ("If-None-Match", response.headers.get("etag")),
                    ("If-Modified-Since", response.headers.get("last-modified")),
                )
                if value
            }
            return self._parse(
                response.content,
                observed_at=observed_at,
                client=client,
            )

    def _parse(
        self,
        content: bytes,
        *,
        observed_at: datetime,
        client: httpx.Client,
    ) -> tuple[RawIntelligenceItem, ...]:
        try:
            root = ElementTree.fromstring(content)
        except ElementTree.ParseError as exc:
            raise ValueError("official RSS XML 非法") from exc
        entries = tuple(
            node for node in root.iter() if _xml_local_name(node.tag) in {"item", "entry"}
        )
        items: list[RawIntelligenceItem] = []
        retained_entry_urls: set[str] = set()
        for entry in entries:
            title = _xml_child_text(entry, ("title",))
            published = _xml_child_text(entry, ("pubDate", "published", "updated", "date"))
            url = _xml_entry_url(
                entry,
                expected_hostname=urlparse(self._feed.url).hostname,
            )
            if not title or not published or not url:
                continue
            event_time = _parse_feed_time(published)
            if event_time > observed_at:
                raise ValueError("official RSS 发布时间晚于系统观测时间")
            if observed_at - event_time > self._maximum_age:
                continue
            guid = _xml_child_text(entry, ("guid", "id")) or url
            feed_summary = _xml_child_text(
                entry,
                ("description", "summary", "content"),
            )
            feed_body = feed_summary or title
            body = feed_body
            decision_excerpt = ""
            acquisition_route = "official-rss-v1"
            entry_url = self._bounded_entry_url(url)
            if entry_url is not None:
                retained_entry_urls.add(entry_url)
                try:
                    document = self._entry_document(client, entry_url)
                    body = document.body
                    decision_excerpt = build_official_decision_excerpt(
                        document,
                        source_summary=feed_summary,
                    )
                    acquisition_route = "official-rss-entry-v3"
                    url = entry_url
                except (httpx.HTTPError, ValueError) as exc:
                    logger.warning(
                        "official RSS 正文补全失败，保留可审计 RSS 事实: stream=%s url=%s error=%s",
                        self._feed.stream_id,
                        entry_url,
                        exc,
                    )
            items.append(
                RawIntelligenceItem(
                    source_item_id=stable_id(
                        "official_rss_item",
                        self._feed.stream_id,
                        guid,
                        event_time.isoformat(),
                    ),
                    source=f"official:{self._feed.stream_id}",
                    acquisition_route=acquisition_route,
                    event_time=event_time,
                    observed_at=observed_at,
                    title=_bounded_external_text(title, maximum_length=1_000),
                    body=_bounded_external_text(
                        body,
                        maximum_length=MAXIMUM_OFFICIAL_DOCUMENT_CHARACTERS,
                    ),
                    decision_excerpt=_bounded_external_text(
                        decision_excerpt,
                        maximum_length=2_000,
                    ),
                    url=url,
                    source_reliability=Decimal("1"),
                    rank=0,
                    immediate_review_eligible=(self._feed.immediate_review_eligible),
                    directional_support_eligible=True,
                )
            )
        self._entry_documents = {
            url: document
            for url, document in self._entry_documents.items()
            if url in retained_entry_urls
        }
        return tuple(items)

    def _bounded_entry_url(self, value: str) -> str | None:
        if self._entry_pattern is None:
            return None
        feed = urlparse(self._feed.url)
        parsed = urlparse(value)
        path = parsed.path.rstrip("/") or "/"
        if (
            parsed.scheme != "https"
            or parsed.hostname != feed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or self._entry_pattern.fullmatch(path) is None
        ):
            return None
        return parsed._replace(path=path).geturl()

    def _entry_document(self, client: httpx.Client, url: str) -> OfficialHtmlDocument:
        if cached := self._entry_documents.get(url):
            return cached
        response = client.get(
            url,
            headers={
                "Accept": "text/html",
                "User-Agent": "investment-manager-official-events/1.0",
            },
        )
        response.raise_for_status()
        if str(response.url).rstrip("/") != url.rstrip("/"):
            raise ValueError("official RSS entry 响应 URL 与固定请求不一致")
        if not response.content or len(response.content) > self._maximum_bytes:
            raise ValueError("official RSS entry 响应为空或超过大小上限")
        document = parse_official_html_document(response.text)
        if not document.title.strip() or not document.body.strip():
            raise ValueError("official RSS entry 缺少标题或正文")
        self._entry_documents[url] = document
        return document


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_child_text(element: ElementTree.Element, names: tuple[str, ...]) -> str:
    for child in element:
        if _xml_local_name(child.tag) in names:
            value = "".join(child.itertext()).strip()
            if value:
                return value
    return ""


def _xml_entry_url(
    element: ElementTree.Element,
    *,
    expected_hostname: str | None,
) -> str | None:
    for child in element:
        if _xml_local_name(child.tag) != "link":
            continue
        value = (child.get("href") or child.text or "").strip()
        parsed = urlparse(value)
        if (
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.hostname == expected_hostname
            and parsed.username is None
            and parsed.password is None
        ):
            return _bounded_external_url(value)
    return None


def _parse_feed_time(value: str) -> datetime:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("official RSS 时间格式非法") from exc
    if parsed.tzinfo is None:
        raise ValueError("official RSS 时间必须包含时区")
    return parsed.astimezone(UTC)


class EventNormalizer:
    _base_asset_aliases: ClassVar[dict[str, tuple[str, ...]]] = {
        "BTC": ("bitcoin", "比特币"),
        "ETH": ("ethereum", "ether", "以太坊"),
    }
    _cross_asset_keywords: ClassVar[tuple[str, ...]] = (
        "central bank",
        "federal reserve",
        "fed ",
        "cpi",
        "dollar index",
        "dxy",
        "inflation",
        "employment situation",
        "fiscal",
        "gross domestic product",
        "gdp",
        "government bond",
        "personal consumption expenditures",
        "personal income",
        "monetary policy",
        "policy rate",
        "balance sheet",
        "pce",
        "payroll",
        "producer price index",
        "ppi",
        "unemployment rate",
        "interest rate",
        "treasury yield",
        "bond yield",
        "sovereign debt",
        "government debt",
        "blockade",
        "tariff",
        "sanction",
        "oil",
        "shipping",
        "armed conflict",
        "ceasefire",
        "央行",
        "美联储",
        "利率",
        "降息",
        "加息",
        "通胀",
        "非农",
        "美元指数",
        "流动性",
        "财政",
        "国债",
        "收益率",
        "债务",
        "封锁",
        "关税",
        "制裁",
        "监管",
        "原油",
        "石油",
        "航运",
        "武装冲突",
        "停火",
    )
    _crypto_context_keywords: ClassVar[tuple[str, ...]] = (
        "crypto",
        "cryptocurrency",
        "digital asset",
        "加密货币",
        "数字资产",
        "blockchain",
        "stablecoin",
        "crypto exchange",
        "coinbase",
        "binance",
        "区块链",
        "稳定币",
        "加密交易所",
    )

    def __init__(
        self,
        *,
        version: str = "intelligence-normalizer-v9",
        universe: tuple[str, ...] = ("BTCUSDT", "ETHUSDT"),
        quote_asset: str = "USDT",
    ) -> None:
        if not universe or len(set(universe)) != len(universe):
            raise ValueError("事件路由 universe 必须非空且不重复")
        if not quote_asset or any(not symbol.endswith(quote_asset) for symbol in universe):
            raise ValueError("事件路由品种必须使用统一 quote_asset 后缀")
        self._version = version
        self._universe = universe
        self._quote_asset = quote_asset

    def normalize(self, item: RawIntelligenceItem) -> IntelligenceEvent | None:
        if item.observed_at < item.event_time:
            raise ValueError("observed_at 不能早于 event_time")
        text = f"{item.title}\n{item.body}".lower()
        symbols = tuple(
            symbol
            for symbol in self._universe
            if any(
                self._contains_symbol_keyword(text, keyword)
                for keyword in self._keywords_for_symbol(symbol)
            )
        )
        relevance = Decimal("1")
        if not symbols:
            if not self._has_crypto_context(text) and not self._has_cross_asset_relevance(text):
                return None
            symbols = self._universe
            relevance = Decimal("0.85")
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
            content_hash(
                {
                    "title": item.title,
                    "body": item.body,
                    "decision_excerpt": item.decision_excerpt,
                }
            ),
        )
        return IntelligenceEvent(
            evidence_id=evidence_id,
            normalizer_version=self._version,
            acquisition_route=item.acquisition_route,
            event_time=item.event_time,
            observed_at=item.observed_at,
            source=item.source,
            title=item.title,
            body=item.body,
            decision_excerpt=item.decision_excerpt,
            url=item.url,
            symbols=symbols,
            relevance=relevance,
            attention_priority=rank_component * relevance,
            source_reliability=item.source_reliability,
            novelty=Decimal("1"),
            immediate_review_eligible=item.immediate_review_eligible,
            directional_support_eligible=item.directional_support_eligible,
        )

    def _keywords_for_symbol(self, symbol: str) -> tuple[str, ...]:
        base_asset = symbol[: -len(self._quote_asset)]
        return (
            symbol.lower(),
            base_asset.lower(),
            *self._base_asset_aliases.get(base_asset, ()),
        )

    def _has_cross_asset_relevance(self, text: str) -> bool:
        if any(keyword in text for keyword in self._cross_asset_keywords):
            return True
        return "etf" in text and any(
            self._contains_symbol_keyword(text, keyword)
            for keyword in self._crypto_context_keywords
        )

    def _has_crypto_context(self, text: str) -> bool:
        return any(
            self._contains_symbol_keyword(text, keyword)
            for keyword in self._crypto_context_keywords
        )

    @staticmethod
    def _contains_symbol_keyword(text: str, keyword: str) -> bool:
        if keyword.isascii() and keyword.isalnum():
            return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text) is not None
        return keyword in text


class EventStore(Protocol):
    def put(self, event: IntelligenceEvent) -> bool: ...

    def exact(
        self,
        *,
        evidence_ids: tuple[str, ...],
        as_of: datetime,
    ) -> tuple[IntelligenceEvent, ...]: ...

    def visible(self, *, symbol: str, as_of: datetime) -> tuple[IntelligenceEvent, ...]: ...


class SourcePollRecorder(Protocol):
    def put(self, poll: SourcePollRecord) -> bool: ...


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
        as_of = require_utc(as_of)
        with self._lock:
            events = [
                item
                for item in self._events.values()
                if symbol in item.symbols and item.observed_at <= as_of
            ]
        latest_by_locator: dict[tuple[str, str], IntelligenceEvent] = {}
        for event in events:
            locator = (event.source, event.url or event.evidence_id)
            current = latest_by_locator.get(locator)
            if current is None or (event.observed_at, event.evidence_id) > (
                current.observed_at,
                current.evidence_id,
            ):
                latest_by_locator[locator] = event
        return tuple(
            sorted(
                latest_by_locator.values(),
                key=lambda item: (item.event_time, item.evidence_id),
            )
        )

    def exact(
        self,
        *,
        evidence_ids: tuple[str, ...],
        as_of: datetime,
    ) -> tuple[IntelligenceEvent, ...]:
        as_of = require_utc(as_of)
        if tuple(sorted(set(evidence_ids))) != evidence_ids:
            raise ValueError("evidence_ids 必须唯一且排序")
        with self._lock:
            missing = tuple(
                evidence_id
                for evidence_id in evidence_ids
                if evidence_id not in self._events or self._events[evidence_id].observed_at > as_of
            )
            if missing:
                raise ValueError("缺少截至 as_of 可见的事件: " + ", ".join(missing))
            return tuple(self._events[evidence_id] for evidence_id in evidence_ids)


@dataclass(frozen=True, slots=True)
class CollectionResult:
    read_count: int
    normalized_count: int
    inserted_count: int
    failed_source_ids: tuple[str, ...] = ()


class InformationCollector:
    def __init__(
        self,
        sources: tuple[IntelligenceSource, ...],
        normalizer: EventNormalizer,
        store: EventStore,
        *,
        poll_recorder: SourcePollRecorder | None = None,
        coverage_bindings: dict[str, tuple[str, CausalDomain]] | None = None,
        poll_interval_seconds: int = 60,
        clock=lambda: datetime.now(UTC),
    ) -> None:
        source_ids = [item.source_id for item in sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("IntelligenceSource source_id 必须唯一")
        bindings = coverage_bindings or {}
        unknown = tuple(sorted(set(bindings) - set(source_ids)))
        if unknown:
            raise ValueError(
                "coverage binding 引用了未知 IntelligenceSource: " + ", ".join(unknown)
            )
        stream_ids = tuple(item[0] for item in bindings.values())
        if len(stream_ids) != len(set(stream_ids)):
            raise ValueError("coverage binding source_stream_id 必须唯一")
        if bool(bindings) != (poll_recorder is not None):
            raise ValueError("coverage binding 与 poll recorder 必须同时配置")
        if poll_interval_seconds < 1:
            raise ValueError("coverage poll interval 必须为正数")
        self._sources = sources
        self._normalizer = normalizer
        self._store = store
        self._poll_recorder = poll_recorder
        self._coverage_bindings = bindings
        self._poll_interval_seconds = poll_interval_seconds
        self._clock = clock

    def collect(self, *, observed_at: datetime) -> CollectionResult:
        read_count = normalized_count = inserted_count = 0
        failed_source_ids: list[str] = []
        observation_time = require_utc(observed_at)
        reads: dict[
            Future[tuple[RawIntelligenceItem, ...]],
            tuple[IntelligenceSource, datetime],
        ] = {}
        with ThreadPoolExecutor(
            max_workers=max(1, len(self._sources)),
            thread_name_prefix="information-source",
        ) as executor:
            for source in self._sources:
                started_at = max(require_utc(self._clock()), observation_time)
                reads[executor.submit(source.read, observed_at=observation_time)] = (
                    source,
                    started_at,
                )
            for future in as_completed(reads):
                source, started_at = reads[future]
                source_read = source_inserted = 0
                latest_publication_at: datetime | None = None
                try:
                    items = future.result()
                    source_read = len(items)
                    latest_publication_at = max(
                        (item.event_time for item in items),
                        default=None,
                    )
                    for raw in items:
                        read_count += 1
                        event = self._normalizer.normalize(raw)
                        if event is None:
                            continue
                        normalized_count += 1
                        if self._store.put(event):
                            inserted_count += 1
                            source_inserted += 1
                    self._record_source_poll(
                        source_id=source.source_id,
                        status=(
                            SourcePollStatus.CHANGED
                            if source_inserted
                            else SourcePollStatus.UNCHANGED
                        ),
                        started_at=started_at,
                        latest_publication_at=latest_publication_at,
                        observation_count=source_read,
                        new_fact_count=source_inserted,
                    )
                except Exception as exc:
                    logger.exception("information source failed: %s", source.source_id)
                    failed_source_ids.append(source.source_id)
                    self._record_source_poll(
                        source_id=source.source_id,
                        status=SourcePollStatus.FAILED,
                        started_at=started_at,
                        error_class=type(exc).__name__,
                    )
        return CollectionResult(
            read_count,
            normalized_count,
            inserted_count,
            tuple(sorted(failed_source_ids)),
        )

    def _record_source_poll(
        self,
        *,
        source_id: str,
        status: SourcePollStatus,
        started_at: datetime,
        latest_publication_at: datetime | None = None,
        observation_count: int = 0,
        new_fact_count: int = 0,
        error_class: str | None = None,
    ) -> None:
        binding = self._coverage_bindings.get(source_id)
        if binding is None:
            return
        assert self._poll_recorder is not None
        stream_id, domain = binding
        completed_at = max(started_at, require_utc(self._clock()))
        try:
            self._poll_recorder.put(
                build_source_poll_record(
                    source_stream_id=stream_id,
                    domain=domain,
                    status=status,
                    started_at=started_at,
                    completed_at=completed_at,
                    poll_interval_seconds=self._poll_interval_seconds,
                    latest_publication_at=latest_publication_at,
                    observation_count=observation_count,
                    new_fact_count=new_fact_count,
                    error_class=error_class,
                )
            )
        except Exception:
            logger.exception("information source coverage write failed: %s", source_id)


@dataclass(slots=True)
class InformationCollectorHealth:
    collection_count: int = 0
    read_count: int = 0
    inserted_count: int = 0
    last_success_at: datetime | None = None
    last_error_class: str | None = None
    failed_source_ids: tuple[str, ...] = ()


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
            observed_at = require_utc(self._clock())
            self.health.collection_count += 1
            try:
                result = await asyncio.to_thread(
                    self._collector.collect,
                    observed_at=observed_at,
                )
                self.health.read_count += result.read_count
                self.health.inserted_count += result.inserted_count
                self.health.last_success_at = observed_at
                self.health.failed_source_ids = result.failed_source_ids
                self.health.last_error_class = (
                    "SOURCE_READ_FAILED" if result.failed_source_ids else None
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.health.last_error_class != type(exc).__name__:
                    logger.exception("information collector failed")
                self.health.last_error_class = type(exc).__name__
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._interval_seconds)
