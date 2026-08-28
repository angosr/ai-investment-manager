"""Pinned first-party publication indexes exposed only as intelligence facts."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from investment_manager.information.collector import RawIntelligenceItem
from investment_manager.information.official.document import (
    MAXIMUM_OFFICIAL_DOCUMENT_CHARACTERS,
    build_official_decision_excerpt,
    parse_official_html_document,
)
from investment_manager.information.policy import OfficialPublicationFeed
from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.time import require_utc


class _IndexLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        values = dict(attrs)
        href = (values.get("href") or "").strip()
        if href:
            self.links.append(href)


class OfficialPublicationSource:
    """Poll one fixed .gov index and fetch only matching first-party entries."""

    def __init__(
        self,
        feed: OfficialPublicationFeed,
        *,
        maximum_age_seconds: int,
        timeout_seconds: int = 15,
        maximum_index_bytes: int = 2_000_000,
        maximum_entry_bytes: int = 2_000_000,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if (
            min(
                maximum_age_seconds,
                timeout_seconds,
                maximum_index_bytes,
                maximum_entry_bytes,
            )
            < 1
        ):
            raise ValueError("official publication age/timeout/size 必须为正数")
        self.source_id = f"official-publication:{feed.stream_id}"
        self.source_stream_id = feed.stream_id
        self.causal_domain = feed.domain
        self._feed = feed
        self._maximum_age = timedelta(seconds=maximum_age_seconds)
        self._timeout_seconds = timeout_seconds
        self._maximum_index_bytes = maximum_index_bytes
        self._maximum_entry_bytes = maximum_entry_bytes
        self._transport = transport
        self._entry_pattern = re.compile(feed.entry_path_pattern)
        self._validators: dict[str, str] = {}
        self._items_by_url: dict[str, RawIntelligenceItem] = {}

    def read(self, *, observed_at: datetime) -> tuple[RawIntelligenceItem, ...]:
        observed_at = require_utc(observed_at)
        headers = {
            "Accept": "text/html",
            "User-Agent": "investment-manager-official-publications/1.0",
            **self._validators,
        }
        with httpx.Client(
            timeout=self._timeout_seconds,
            follow_redirects=False,
            transport=self._transport,
        ) as client:
            response = client.get(self._feed.index_url, headers=headers)
            if response.status_code != 304:
                response.raise_for_status()
                self._require_exact_response(response, self._feed.index_url, kind="index")
                if not response.content or len(response.content) > self._maximum_index_bytes:
                    raise ValueError("official publication index 响应为空或超过大小上限")
                self._validators = {
                    name: value
                    for name, value in (
                        ("If-None-Match", response.headers.get("etag")),
                        ("If-Modified-Since", response.headers.get("last-modified")),
                    )
                    if value
                }
                for url in self._entry_urls(response.text):
                    if url in self._items_by_url:
                        continue
                    entry = client.get(
                        url,
                        headers={
                            "Accept": "text/html",
                            "User-Agent": "investment-manager-official-publications/1.0",
                        },
                    )
                    entry.raise_for_status()
                    self._require_exact_response(entry, url, kind="entry")
                    if not entry.content or len(entry.content) > self._maximum_entry_bytes:
                        raise ValueError("official publication entry 响应为空或超过大小上限")
                    item = self._parse_entry(url, entry.text, observed_at=observed_at)
                    if item is not None:
                        self._items_by_url[url] = item
        self._items_by_url = {
            url: item
            for url, item in self._items_by_url.items()
            if observed_at - item.event_time <= self._maximum_age
        }
        return tuple(
            sorted(
                self._items_by_url.values(),
                key=lambda item: (item.event_time, item.source_item_id),
            )
        )

    def _entry_urls(self, content: str) -> tuple[str, ...]:
        parser = _IndexLinkParser()
        parser.feed(content)
        index = urlparse(self._feed.index_url)
        urls: list[str] = []
        for raw in parser.links:
            url = urljoin(self._feed.index_url + "/", raw)
            parsed = urlparse(url)
            canonical_path = parsed.path.rstrip("/") or "/"
            if (
                parsed.scheme != "https"
                or parsed.hostname != index.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or self._entry_pattern.fullmatch(canonical_path) is None
            ):
                continue
            canonical = parsed._replace(path=canonical_path, fragment="").geturl()
            if canonical not in urls:
                urls.append(canonical)
            if len(urls) >= self._feed.maximum_entries:
                break
        return tuple(urls)

    def _parse_entry(
        self,
        url: str,
        content: str,
        *,
        observed_at: datetime,
    ) -> RawIntelligenceItem | None:
        document = parse_official_html_document(content)
        title = document.title.strip()
        body = document.body.strip()
        if not title or not body:
            raise ValueError("official publication entry 缺少标题或正文")
        event_time = _publication_time(
            list(document.time_values),
            url=url,
            observed_at=observed_at,
        )
        if event_time > observed_at:
            raise ValueError("official publication 发布时间晚于系统观测时间")
        if observed_at - event_time > self._maximum_age:
            return None
        return RawIntelligenceItem(
            source_item_id=stable_id(
                "official_publication_item",
                self._feed.stream_id,
                url,
                event_time.isoformat(),
            ),
            source=f"official:{self._feed.stream_id}",
            acquisition_route="official-publication-v3",
            event_time=event_time,
            observed_at=observed_at,
            title=title[:1_000],
            body=body[:MAXIMUM_OFFICIAL_DOCUMENT_CHARACTERS],
            decision_excerpt=build_official_decision_excerpt(document),
            url=url,
            source_reliability=Decimal("1"),
            rank=0,
            directional_support_eligible=True,
        )

    @staticmethod
    def _require_exact_response(response: httpx.Response, expected: str, *, kind: str) -> None:
        if str(response.url).rstrip("/") != expected.rstrip("/"):
            raise ValueError(f"official publication {kind} 响应 URL 与固定请求不一致")


def _publication_time(
    candidates: list[str],
    *,
    url: str,
    observed_at: datetime,
) -> datetime:
    for value in candidates:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC)
    match = re.search(r"/(20\d{6})$", url)
    if match is not None:
        parsed = datetime.strptime(match.group(1), "%Y%m%d").replace(
            hour=12,
            tzinfo=UTC,
        )
        return min(parsed, observed_at)
    raise ValueError("official publication entry 缺少可验证发布时间")
