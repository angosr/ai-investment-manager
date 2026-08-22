from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

from investment_manager.information.official.metrics import (
    FED_BROAD_DOLLAR_STREAM_ID,
    IBIT_HOLDINGS_STREAM_ID,
    NYFED_RATES_STREAM_ID,
    NYFED_RRP_STREAM_ID,
    NYFED_SOMA_STREAM_ID,
    TGA_STREAM_ID,
    TREASURY_YIELD_STREAM_ID,
)
from investment_manager.information.official.public_calendar import FED_PUBLIC_CALENDAR_URL
from investment_manager.information.official.records import (
    FED_FOMC_CALENDAR_URL,
    FED_MONETARY_RSS_URL,
)
from investment_manager.information.official.regulation import (
    FEDERAL_REGISTER_API_ROOT,
    FEDERAL_REGISTER_RULEMAKING_STREAM_ID,
)
from investment_manager.information.official.treasury_buybacks import (
    TREASURY_BUYBACK_URL,
    TreasuryBuybackOperationRecord,
    treasury_buyback_result_url,
)
from investment_manager.kernel.time import require_utc

_TGA_API_URL = (
    "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/"
    "v1/accounting/dts/operating_cash_balance"
)
_TREASURY_YIELD_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/"
    "interest-rates/pages/xml"
)
_FED_BROAD_DOLLAR_URL = (
    "https://www.federalreserve.gov/feeds/data/H10_H10_JRXWTFB_N.B.xml"
)
_NYFED_RRP_URL = (
    "https://markets.newyorkfed.org/api/rp/reverserepo/propositions/search.json"
)
_NYFED_SOMA_URL = "https://markets.newyorkfed.org/api/soma/summary.json"
_NYFED_RATES_URL = "https://markets.newyorkfed.org/api/rates/all/latest.json"
_IBIT_HOLDINGS_URL = (
    "https://www.ishares.com/us/products/333011/"
    "ishares-bitcoin-trust-etf/latest-holdings.csv"
)


@dataclass(frozen=True, slots=True)
class OfficialMetricDocument:
    stream_id: str
    source_url: str
    media_type: str
    content: bytes


@dataclass(frozen=True, slots=True)
class OfficialRegulatoryDocument:
    stream_id: str
    source_url: str
    content: bytes


class HttpFederalRegisterSource:
    """Poll a bounded SEC/CFTC catalog from the official Federal Register API."""

    def __init__(
        self,
        *,
        timeout_seconds: int,
        lookback_days: int = 7,
        maximum_bytes: int = 2_000_000,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if timeout_seconds < 1 or not 1 <= lookback_days <= 30 or maximum_bytes < 1:
            raise ValueError("Federal Register source 配置非法")
        self._timeout_seconds = timeout_seconds
        self._lookback_days = lookback_days
        self._maximum_bytes = maximum_bytes
        self._transport = transport
        self._validators_by_url: dict[str, dict[str, str]] = {}

    def fetch(self, *, observed_at: datetime) -> OfficialRegulatoryDocument | None:
        observed_at = require_utc(observed_at)
        start = (observed_at.date() - timedelta(days=self._lookback_days)).isoformat()
        url = str(
            httpx.URL(
                FEDERAL_REGISTER_API_ROOT,
                params=(
                    ("per_page", "100"),
                    ("order", "newest"),
                    ("conditions[publication_date][gte]", start),
                    (
                        "conditions[agencies][]",
                        "commodity-futures-trading-commission",
                    ),
                    (
                        "conditions[agencies][]",
                        "securities-and-exchange-commission",
                    ),
                ),
            )
        )
        headers = {
            "Accept": "application/json",
            "User-Agent": "investment-manager-official-regulation/1.0",
            **self._validators_by_url.get(url, {}),
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
            raise ValueError("Federal Register 响应 URL 与固定请求不一致")
        content = response.content
        if not content or len(content) > self._maximum_bytes:
            raise ValueError("Federal Register 响应为空或超过大小上限")
        validators: dict[str, str] = {}
        if etag := response.headers.get("etag"):
            validators["If-None-Match"] = etag
        if modified := response.headers.get("last-modified"):
            validators["If-Modified-Since"] = modified
        self._validators_by_url = {url: validators}
        return OfficialRegulatoryDocument(
            stream_id=FEDERAL_REGISTER_RULEMAKING_STREAM_ID,
            source_url=url,
            content=content,
        )


class HttpOfficialMetricSource:
    """Fetch a small fixed catalog of public first-party macro data feeds."""

    stream_ids = (
        FED_BROAD_DOLLAR_STREAM_ID,
        IBIT_HOLDINGS_STREAM_ID,
        NYFED_RATES_STREAM_ID,
        NYFED_RRP_STREAM_ID,
        NYFED_SOMA_STREAM_ID,
        TGA_STREAM_ID,
        TREASURY_YIELD_STREAM_ID,
    )

    def __init__(
        self,
        *,
        timeout_seconds: int,
        maximum_bytes: int = 1_000_000,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if timeout_seconds < 1 or maximum_bytes < 1:
            raise ValueError("官方指标 source timeout/size 必须为正数")
        self._timeout_seconds = timeout_seconds
        self._maximum_bytes = maximum_bytes
        self._transport = transport
        self._validators: dict[str, dict[str, str]] = {}

    def fetch(
        self,
        stream_id: str,
        *,
        observed_at: datetime,
    ) -> OfficialMetricDocument | None:
        observed_at = require_utc(observed_at)
        url, media_type = self._request(stream_id, observed_at=observed_at)
        headers = {
            "Accept": media_type,
            "User-Agent": "investment-manager-official-metrics/1.0",
            **self._validators.get(url, {}),
        }
        with httpx.Client(
            timeout=self._timeout_seconds,
            follow_redirects=False,
            transport=self._transport,
        ) as client:
            response = client.get(url, headers=headers)
            if response.status_code == 406 and media_type == "application/xml":
                response = client.get(
                    url,
                    headers={**headers, "Accept": "application/xml, text/xml, */*;q=0.8"},
                )
        if response.status_code == 304:
            return None
        response.raise_for_status()
        if str(response.url) != url:
            raise ValueError("官方指标响应 URL 与固定请求不一致")
        content = response.content
        if not content or len(content) > self._maximum_bytes:
            raise ValueError("官方指标响应为空或超过大小上限")
        validators: dict[str, str] = {}
        if etag := response.headers.get("etag"):
            validators["If-None-Match"] = etag
        if modified := response.headers.get("last-modified"):
            validators["If-Modified-Since"] = modified
        self._validators[url] = validators
        return OfficialMetricDocument(
            stream_id=stream_id,
            source_url=url,
            media_type=media_type,
            content=content,
        )

    @staticmethod
    def _request(stream_id: str, *, observed_at: datetime) -> tuple[str, str]:
        if stream_id == TGA_STREAM_ID:
            start = (observed_at.date() - timedelta(days=370)).isoformat()
            url = httpx.URL(
                _TGA_API_URL,
                params={
                    "filter": f"record_date:gte:{start}",
                    "sort": "-record_date,-account_type",
                    "page[size]": "5000",
                },
            )
            return str(url), "application/json"
        if stream_id == TREASURY_YIELD_STREAM_ID:
            url = httpx.URL(
                _TREASURY_YIELD_URL,
                params={
                    "data": "daily_treasury_yield_curve",
                    "field_tdr_date_value": observed_at.strftime("%Y"),
                },
            )
            return str(url), "application/xml"
        if stream_id == FED_BROAD_DOLLAR_STREAM_ID:
            return _FED_BROAD_DOLLAR_URL, "application/xml"
        if stream_id == IBIT_HOLDINGS_STREAM_ID:
            return _IBIT_HOLDINGS_URL, "text/csv"
        if stream_id == NYFED_RRP_STREAM_ID:
            start = (observed_at.date() - timedelta(days=370)).isoformat()
            url = httpx.URL(
                _NYFED_RRP_URL,
                params={
                    "startDate": start,
                    "endDate": observed_at.date().isoformat(),
                    "type": "all",
                    "sort": "postDt:-1,eventNum:-1",
                    "format": "json",
                },
            )
            return str(url), "application/json"
        if stream_id == NYFED_SOMA_STREAM_ID:
            return _NYFED_SOMA_URL, "application/json"
        if stream_id == NYFED_RATES_STREAM_ID:
            return _NYFED_RATES_URL, "application/json"
        raise ValueError(f"未知官方指标流: {stream_id}")


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

    def fetch_public_calendar(self) -> str | None:
        return self._fetch(FED_PUBLIC_CALENDAR_URL)

    def _fetch(self, url: str) -> str | None:
        if url == FED_FOMC_CALENDAR_URL:
            accept = "text/html, application/xhtml+xml;q=0.9"
        elif url == FED_PUBLIC_CALENDAR_URL:
            accept = "application/json"
        else:
            accept = "application/rss+xml, application/xml, text/xml;q=0.9"
        headers = {
            "Accept": accept,
            "User-Agent": "investment-manager-official-source/1.0",
            **self._validators.get(url, {}),
        }
        with httpx.Client(
            timeout=self._timeout_seconds,
            follow_redirects=False,
            transport=self._transport,
        ) as client:
            response = client.get(url, headers=headers)
            if response.status_code == 406:
                response = client.get(
                    url,
                    headers={
                        **headers,
                        "Accept": f"{accept}, */*;q=0.8",
                    },
                )
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


class HttpTreasuryBuybackSource:
    """Read the pinned Treasury tentative buyback calendar safely."""

    def __init__(
        self,
        *,
        timeout_seconds: int,
        maximum_bytes: int = 1_000_000,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if timeout_seconds < 1 or maximum_bytes < 1:
            raise ValueError("Treasury buyback source timeout/size 必须为正数")
        self._timeout_seconds = timeout_seconds
        self._maximum_bytes = maximum_bytes
        self._transport = transport
        self._validators: dict[str, dict[str, str]] = {}

    def fetch_calendar(self) -> bytes | None:
        headers = {
            "Accept": "application/xml, text/xml;q=0.9",
            "User-Agent": "investment-manager-treasury-buybacks/1.0",
            **self._validators.get(TREASURY_BUYBACK_URL, {}),
        }
        with httpx.Client(
            timeout=self._timeout_seconds,
            follow_redirects=False,
            transport=self._transport,
        ) as client:
            response = client.get(TREASURY_BUYBACK_URL, headers=headers)
        if response.status_code == 304:
            return None
        response.raise_for_status()
        if str(response.url) != TREASURY_BUYBACK_URL:
            raise ValueError("Treasury buyback source 响应 URL 与固定端点不一致")
        content = response.content
        if not content or len(content) > self._maximum_bytes:
            raise ValueError("Treasury buyback source 响应为空或超过大小上限")
        validators: dict[str, str] = {}
        if etag := response.headers.get("etag"):
            validators["If-None-Match"] = etag
        if modified := response.headers.get("last-modified"):
            validators["If-Modified-Since"] = modified
        self._validators[TREASURY_BUYBACK_URL] = validators
        return content

    def fetch_result(
        self,
        scheduled: TreasuryBuybackOperationRecord,
    ) -> bytes | None:
        url = treasury_buyback_result_url(scheduled.operation_start_at)
        headers = {
            "Accept": "application/xml, text/xml;q=0.9",
            "User-Agent": "investment-manager-treasury-buybacks/1.0",
            **self._validators.get(url, {}),
        }
        with httpx.Client(
            timeout=self._timeout_seconds,
            follow_redirects=False,
            transport=self._transport,
        ) as client:
            response = client.get(url, headers=headers)
        if response.status_code in {304, 404}:
            return None
        response.raise_for_status()
        if str(response.url) != url:
            raise ValueError("Treasury buyback result 响应 URL 与操作身份不一致")
        content = response.content
        if not content or len(content) > self._maximum_bytes:
            raise ValueError("Treasury buyback result 响应为空或超过大小上限")
        validators: dict[str, str] = {}
        if etag := response.headers.get("etag"):
            validators["If-None-Match"] = etag
        if modified := response.headers.get("last-modified"):
            validators["If-Modified-Since"] = modified
        self._validators[url] = validators
        return content
