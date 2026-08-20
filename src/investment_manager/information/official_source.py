from __future__ import annotations

import httpx

from investment_manager.information.official import (
    FED_FOMC_CALENDAR_URL,
    FED_MONETARY_RSS_URL,
)


class HttpFedOfficialSource:
    """Read only pinned Federal Reserve endpoints with conditional requests."""

    def __init__(
        self,
        *,
        timeout_seconds: int,
        maximum_bytes: int = 5_000_000,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if timeout_seconds < 1 or maximum_bytes < 1:
            raise ValueError("Fed official source timeout/size 必须为正数")
        self._timeout_seconds = timeout_seconds
        self._maximum_bytes = maximum_bytes
        self._transport = transport
        self._validators: dict[str, dict[str, str]] = {}

    def fetch_calendar(self) -> str | None:
        return self._fetch(FED_FOMC_CALENDAR_URL)

    def fetch_monetary_rss(self) -> str | None:
        return self._fetch(FED_MONETARY_RSS_URL)

    def _fetch(self, url: str) -> str | None:
        headers = {
            "Accept": "text/html, application/rss+xml, application/xml;q=0.9",
            "User-Agent": "investment-manager-official-source/1.0",
            **self._validators.get(url, {}),
        }
        with httpx.Client(
            timeout=self._timeout_seconds,
            follow_redirects=False,
            transport=self._transport,
        ) as client:
            response = client.get(url, headers=headers)
        if response.status_code == 304:
            return None
        response.raise_for_status()
        if str(response.url) != url:
            raise ValueError("Fed official source 响应 URL 与固定端点不一致")
        content = response.content
        if len(content) > self._maximum_bytes:
            raise ValueError("Fed official source 响应超过大小上限")
        validators = {}
        if etag := response.headers.get("etag"):
            validators["If-None-Match"] = etag
        if modified := response.headers.get("last-modified"):
            validators["If-Modified-Since"] = modified
        self._validators[url] = validators
        return content.decode(response.encoding or "utf-8")
