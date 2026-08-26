from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from html.parser import HTMLParser
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx

from investment_manager.information.models import SourceObservation, SourceTier
from investment_manager.information.official.economic_actuals import (
    EconomicReleaseActualRecord,
    EconomicReleaseMetric,
    EconomicReleaseUnit,
    EconomicReleaseValue,
    economic_actual_semantic_payload,
    economic_calendar_event_id,
)
from investment_manager.information.official.economic_calendar import (
    EconomicReleaseEventRecord,
    EconomicReleaseKind,
)
from investment_manager.information.official.records import CalendarEventStatus
from investment_manager.information.raw_payload import build_raw_source_payload
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc

BEA_RELEASE_RSS_URL = "https://apps.bea.gov/rss/rss.xml"
BLS_API_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
_USER_AGENT = "investment-manager-economic-actuals/1.0 (+https://localhost/)"


@dataclass(frozen=True, slots=True)
class EconomicReleaseActualDocument:
    source_url: str
    media_type: str
    content: bytes


class HttpEconomicReleaseActualSource:
    """Fetch actual values only for a due first-party calendar obligation."""

    def __init__(
        self,
        *,
        timeout_seconds: int,
        maximum_bytes: int = 5_000_000,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if timeout_seconds < 1 or maximum_bytes < 1:
            raise ValueError("经济发布实际值 source timeout/size 必须为正数")
        self._timeout_seconds = timeout_seconds
        self._maximum_bytes = maximum_bytes
        self._transport = transport

    def fetch(
        self,
        event: EconomicReleaseEventRecord,
        *,
        observed_at: datetime,
    ) -> EconomicReleaseActualDocument | None:
        observed_at = require_utc(observed_at)
        if event.status != CalendarEventStatus.SCHEDULED:
            return None
        if observed_at < event.scheduled_at:
            raise ValueError("不能在经济发布时间前读取实际值")
        if event.release_kind in {EconomicReleaseKind.US_GDP, EconomicReleaseKind.US_PCE}:
            return self._fetch_bea(event)
        return self._fetch_bls(event, observed_at=observed_at)

    def _fetch_bea(
        self,
        event: EconomicReleaseEventRecord,
    ) -> EconomicReleaseActualDocument | None:
        rss = self._get(
            BEA_RELEASE_RSS_URL,
            accept="application/xml,text/xml,*/*",
        )
        try:
            root = ElementTree.fromstring(rss)
        except ElementTree.ParseError as exc:
            raise ValueError("BEA release RSS XML 非法") from exc
        link = None
        for item in root.findall("./channel/item"):
            if (item.findtext("title") or "").strip() != event.title:
                continue
            candidate = (item.findtext("link") or "").strip()
            parsed = urlparse(candidate)
            if parsed.scheme == "https" and parsed.hostname == "www.bea.gov":
                link = candidate
                break
        if link is None:
            return None
        content = self._get(link, accept="text/html")
        return EconomicReleaseActualDocument(
            source_url=link,
            media_type="text/html",
            content=content,
        )

    def _fetch_bls(
        self,
        event: EconomicReleaseEventRecord,
        *,
        observed_at: datetime,
    ) -> EconomicReleaseActualDocument | None:
        series = (
            ("CUSR0000SA0", "CUSR0000SA0L1E")
            if event.release_kind == EconomicReleaseKind.US_CPI
            else ("CES0000000001", "LNS14000000", "CES0500000003")
        )
        payload = {
            "seriesid": series,
            "startyear": str(observed_at.year - 1),
            "endyear": str(observed_at.year),
        }
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        }
        with httpx.Client(
            timeout=self._timeout_seconds,
            follow_redirects=False,
            transport=self._transport,
        ) as client:
            response = client.post(BLS_API_URL, headers=headers, json=payload)
        response.raise_for_status()
        if str(response.url) != BLS_API_URL:
            raise ValueError("BLS API 响应 URL 与固定请求不一致")
        content = response.content
        if not content or len(content) > self._maximum_bytes:
            raise ValueError("BLS API 响应为空或超过大小上限")
        return EconomicReleaseActualDocument(
            source_url=BLS_API_URL,
            media_type="application/json",
            content=content,
        )

    def _get(self, url: str, *, accept: str) -> bytes:
        with httpx.Client(
            timeout=self._timeout_seconds,
            follow_redirects=False,
            transport=self._transport,
        ) as client:
            response = client.get(
                url,
                headers={
                    "Accept": accept,
                    "User-Agent": _USER_AGENT,
                },
            )
        response.raise_for_status()
        if str(response.url) != url:
            raise ValueError("经济发布实际值响应 URL 与固定请求不一致")
        if not response.content or len(response.content) > self._maximum_bytes:
            raise ValueError("经济发布实际值响应为空或超过大小上限")
        return response.content


def parse_economic_release_actual(
    event: EconomicReleaseEventRecord,
    document: EconomicReleaseActualDocument,
    *,
    observed_at: datetime,
) -> EconomicReleaseActualRecord | None:
    observed_at = require_utc(observed_at)
    if event.release_kind == EconomicReleaseKind.US_PCE:
        parsed = _parse_bea_pce(document.content, expected_title=event.title)
    elif event.release_kind == EconomicReleaseKind.US_GDP:
        parsed = _parse_bea_gdp(document.content, expected_title=event.title)
    elif event.release_kind == EconomicReleaseKind.US_CPI:
        parsed = _parse_bls_cpi(document.content, scheduled_at=event.scheduled_at)
    else:
        parsed = _parse_bls_employment(
            document.content,
            scheduled_at=event.scheduled_at,
        )
    if parsed is None:
        return None
    period, vintage, values = parsed
    raw = build_raw_source_payload(
        source_id=event.observation.source_id,
        source_url=document.source_url,
        media_type=document.media_type,
        observed_at=observed_at,
        content=document.content,
    )
    calendar_event_id = economic_calendar_event_id(event)
    record_id = f"economic-release-actual:{calendar_event_id}"
    ordered = tuple(sorted(values, key=lambda item: item.name.value))
    draft = EconomicReleaseActualRecord.model_construct(
        observation=SourceObservation.model_construct(
            observation_id="pending",
            source_id=event.observation.source_id,
            source_tier=SourceTier.FIRST_PARTY,
            source_record_id=record_id,
            observed_at=observed_at,
            source_published_at=event.scheduled_at,
            payload_hash="0" * 64,
            payload_ref=raw.payload_id,
        ),
        calendar_event_id=calendar_event_id,
        release_kind=event.release_kind,
        scheduled_at=event.scheduled_at,
        period=period,
        vintage=vintage,
        title=event.title,
        values=ordered,
        source_url=document.source_url,
    )
    digest = content_hash(economic_actual_semantic_payload(draft))
    observation = SourceObservation(
        **draft.observation.model_dump(exclude={"observation_id", "payload_hash"}),
        payload_hash=digest,
        observation_id=stable_id(
            "source_observation",
            event.observation.source_id,
            record_id,
            digest,
            observed_at.isoformat(),
        ),
    )
    return EconomicReleaseActualRecord(
        **draft.model_dump(exclude={"observation"}),
        observation=observation,
    )


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self.text: list[str] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        self.text.append(data)
        if self._cell is not None:
            self._cell.append(data)


def _html(content: bytes) -> tuple[str, tuple[tuple[str, ...], ...]]:
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("BEA release HTML 不是 UTF-8") from exc
    parser = _TableParser()
    parser.feed(decoded)
    return " ".join(" ".join(parser.text).split()), tuple(tuple(row) for row in parser.rows)


def _parse_bea_pce(
    content: bytes,
    *,
    expected_title: str,
) -> tuple[str, str, tuple[EconomicReleaseValue, ...]] | None:
    text, rows = _html(content)
    if expected_title not in text:
        raise ValueError("BEA PCE 页面标题与日历事件不一致")
    labels = {
        "Current-dollar personal income": EconomicReleaseMetric.PERSONAL_INCOME_MOM_PCT,
        "Current-dollar DPI": EconomicReleaseMetric.DISPOSABLE_PERSONAL_INCOME_MOM_PCT,
        "Current-dollar PCE": EconomicReleaseMetric.NOMINAL_PCE_MOM_PCT,
        "Real PCE": EconomicReleaseMetric.REAL_PCE_MOM_PCT,
        "PCE price index": EconomicReleaseMetric.PCE_PRICE_MOM_PCT,
        "PCE price index excluding food and energy": EconomicReleaseMetric.CORE_PCE_PRICE_MOM_PCT,
    }
    values: list[EconomicReleaseValue] = []
    period = expected_title.rsplit(",", 1)[-1].strip()
    for row in rows:
        if len(row) < 2:
            continue
        metric = labels.get(row[0])
        if metric is None:
            continue
        values.append(_value(metric, _decimal(row[-1]), EconomicReleaseUnit.PERCENT))
    yoy = re.search(
        r"From the same month one year ago, the PCE price index for \w+ "
        r"(increased|decreased) ([+-]?\d+(?:\.\d+)?) percent.*?"
        r"Excluding food and energy, the PCE price index "
        r"(increased|decreased) ([+-]?\d+(?:\.\d+)?) percent from one year ago",
        text,
        re.IGNORECASE,
    )
    saving = re.search(r"personal saving rate[^.]*?was ([+-]?\d+(?:\.\d+)?) percent", text, re.I)
    if yoy is not None:
        values.extend(
            (
                _value(
                    EconomicReleaseMetric.PCE_PRICE_YOY_PCT,
                    _signed_decimal(yoy.group(1), yoy.group(2)),
                    EconomicReleaseUnit.PERCENT,
                ),
                _value(
                    EconomicReleaseMetric.CORE_PCE_PRICE_YOY_PCT,
                    _signed_decimal(yoy.group(3), yoy.group(4)),
                    EconomicReleaseUnit.PERCENT,
                ),
            )
        )
    if saving is not None:
        values.append(
            _value(
                EconomicReleaseMetric.PERSONAL_SAVING_RATE_PCT,
                _decimal(saving.group(1)),
                EconomicReleaseUnit.PERCENT,
            )
        )
    required = {
        EconomicReleaseMetric.PCE_PRICE_MOM_PCT,
        EconomicReleaseMetric.CORE_PCE_PRICE_MOM_PCT,
        EconomicReleaseMetric.PCE_PRICE_YOY_PCT,
        EconomicReleaseMetric.CORE_PCE_PRICE_YOY_PCT,
        EconomicReleaseMetric.REAL_PCE_MOM_PCT,
    }
    if not required <= {item.name for item in values}:
        return None
    return period, "official-release-first-visible", tuple(values)


def _parse_bea_gdp(
    content: bytes,
    *,
    expected_title: str,
) -> tuple[str, str, tuple[EconomicReleaseValue, ...]] | None:
    text, _ = _html(content)
    if expected_title not in text:
        raise ValueError("BEA GDP 页面标题与日历事件不一致")
    current = re.search(
        r"Real gross domestic product \(GDP\) (?:increased|decreased) at an annual rate of "
        r"([+-]?\d+(?:\.\d+)?) percent",
        text,
        re.IGNORECASE,
    )
    if current is None:
        return None
    sign = Decimal("-1") if "decreased" in current.group(0).casefold() else Decimal("1")
    values = [
        _value(
            EconomicReleaseMetric.REAL_GDP_QOQ_ANNUALIZED_PCT,
            sign * _decimal(current.group(1)),
            EconomicReleaseUnit.PERCENT,
        )
    ]
    prior = re.search(
        r"In the (?:advance|previous) estimate, the (?:increase|decrease) in real GDP was "
        r"([+-]?\d+(?:\.\d+)?) percent",
        text,
        re.IGNORECASE,
    )
    if prior is not None:
        prior_sign = Decimal("-1") if "decrease" in prior.group(0).casefold() else Decimal("1")
        values.append(
            _value(
                EconomicReleaseMetric.PRIOR_GDP_VINTAGE_QOQ_ANNUALIZED_PCT,
                prior_sign * _decimal(prior.group(1)),
                EconomicReleaseUnit.PERCENT,
            )
        )
    elif re.search(
        r"Real GDP (?:increased|decreased) at the same rate as in the "
        r"(?:advance|previous) estimate",
        text,
        re.IGNORECASE,
    ):
        values.append(
            _value(
                EconomicReleaseMetric.PRIOR_GDP_VINTAGE_QOQ_ANNUALIZED_PCT,
                sign * _decimal(current.group(1)),
                EconomicReleaseUnit.PERCENT,
            )
        )
    period_match = re.search(r"in the ([^.]+? quarter of \d{4})", text, re.I)
    period = period_match.group(1) if period_match is not None else expected_title
    vintage = expected_title.split(")", 1)[0].split("(", 1)[-1]
    return period, vintage, tuple(values)


def _parse_bls_cpi(
    content: bytes,
    *,
    scheduled_at: datetime,
) -> tuple[str, str, tuple[EconomicReleaseValue, ...]] | None:
    series = _bls_series(content, ("CUSR0000SA0", "CUSR0000SA0L1E"))
    periods = {_period_key(rows[0]) for rows in series.values()}
    if periods != {_expected_bls_period(scheduled_at)}:
        return None
    headline = series["CUSR0000SA0"]
    core = series["CUSR0000SA0L1E"]
    period = headline[0]["periodName"] + " " + headline[0]["year"]
    return (
        period,
        "official-api-first-visible",
        (
            _value(
                EconomicReleaseMetric.HEADLINE_CPI_MOM_PCT,
                _pct_change(headline[0], headline[1]),
                EconomicReleaseUnit.PERCENT,
            ),
            _value(
                EconomicReleaseMetric.HEADLINE_CPI_YOY_PCT,
                _pct_change(headline[0], _same_month_prior_year(headline)),
                EconomicReleaseUnit.PERCENT,
            ),
            _value(
                EconomicReleaseMetric.CORE_CPI_MOM_PCT,
                _pct_change(core[0], core[1]),
                EconomicReleaseUnit.PERCENT,
            ),
            _value(
                EconomicReleaseMetric.CORE_CPI_YOY_PCT,
                _pct_change(core[0], _same_month_prior_year(core)),
                EconomicReleaseUnit.PERCENT,
            ),
        ),
    )


def _parse_bls_employment(
    content: bytes,
    *,
    scheduled_at: datetime,
) -> tuple[str, str, tuple[EconomicReleaseValue, ...]] | None:
    series = _bls_series(content, ("CES0000000001", "LNS14000000", "CES0500000003"))
    periods = {_period_key(rows[0]) for rows in series.values()}
    if periods != {_expected_bls_period(scheduled_at)}:
        return None
    payroll = series["CES0000000001"]
    unemployment = series["LNS14000000"]
    earnings = series["CES0500000003"]
    period = payroll[0]["periodName"] + " " + payroll[0]["year"]
    return (
        period,
        "official-api-first-visible",
        (
            _value(
                EconomicReleaseMetric.NONFARM_PAYROLL_CHANGE_THOUSANDS,
                _decimal(payroll[0]["value"]) - _decimal(payroll[1]["value"]),
                EconomicReleaseUnit.THOUSANDS,
            ),
            _value(
                EconomicReleaseMetric.UNEMPLOYMENT_RATE_PCT,
                _decimal(unemployment[0]["value"]),
                EconomicReleaseUnit.PERCENT,
            ),
            _value(
                EconomicReleaseMetric.AVERAGE_HOURLY_EARNINGS_MOM_PCT,
                _pct_change(earnings[0], earnings[1]),
                EconomicReleaseUnit.PERCENT,
            ),
            _value(
                EconomicReleaseMetric.AVERAGE_HOURLY_EARNINGS_YOY_PCT,
                _pct_change(earnings[0], _same_month_prior_year(earnings)),
                EconomicReleaseUnit.PERCENT,
            ),
        ),
    )


def _bls_series(content: bytes, expected: tuple[str, ...]) -> dict[str, list[dict]]:
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("BLS economic release JSON 非法") from exc
    if document.get("status") != "REQUEST_SUCCEEDED":
        raise ValueError("BLS economic release API 请求失败")
    rows = {
        item.get("seriesID"): [
            row
            for row in item.get("data", [])
            if isinstance(row, dict)
            and re.fullmatch(r"M(?:0[1-9]|1[0-2])", str(row.get("period", "")))
        ]
        for item in document.get("Results", {}).get("series", [])
        if isinstance(item, dict)
    }
    if set(rows) != set(expected) or any(len(item) < 13 for item in rows.values()):
        raise ValueError("BLS economic release API 缺少所需序列或历史")
    return {
        key: sorted(
            rows[key],
            key=lambda row: (int(row["year"]), int(str(row["period"])[1:])),
            reverse=True,
        )
        for key in expected
    }


def _expected_bls_period(scheduled_at: datetime) -> tuple[str, str]:
    """Both selected BLS releases report the immediately preceding month."""

    scheduled_at = require_utc(scheduled_at)
    if scheduled_at.month == 1:
        return str(scheduled_at.year - 1), "M12"
    return str(scheduled_at.year), f"M{scheduled_at.month - 1:02d}"


def _same_month_prior_year(rows: list[dict]) -> dict:
    current = rows[0]
    wanted = (str(int(current["year"]) - 1), current["period"])
    for row in rows[1:]:
        if (row.get("year"), row.get("period")) == wanted:
            return row
    raise ValueError("BLS 序列缺少同比基期")


def _period_key(row: dict) -> tuple[str, str]:
    return str(row.get("year")), str(row.get("period"))


def _pct_change(current: dict, previous: dict) -> Decimal:
    denominator = _decimal(previous["value"])
    if denominator == 0:
        raise ValueError("经济序列变化率基期不能为零")
    return (((_decimal(current["value"]) / denominator) - 1) * 100).quantize(
        Decimal("0.1"),
        rounding=ROUND_HALF_UP,
    )


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value).replace(",", "").strip())
    except Exception as exc:
        raise ValueError("经济发布指标不是有效数值") from exc


def _signed_decimal(direction: str, value: object) -> Decimal:
    parsed = _decimal(value)
    return -abs(parsed) if direction.casefold() == "decreased" else abs(parsed)


def _value(
    name: EconomicReleaseMetric,
    value: Decimal,
    unit: EconomicReleaseUnit,
) -> EconomicReleaseValue:
    return EconomicReleaseValue(name=name, value=value, unit=unit)
