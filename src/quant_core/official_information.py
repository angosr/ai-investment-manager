from __future__ import annotations

import re
from datetime import UTC, date, datetime, time
from email.utils import parsedate_to_datetime
from enum import StrEnum
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urlparse
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator

from quant_core.asset_management import SourceObservation, SourceTier
from quant_core.domain import FrozenModel, _require_utc
from quant_core.ids import content_hash, stable_id

FED_SOURCE_ID = "federal-reserve"
FED_MONETARY_RSS_URL = "https://www.federalreserve.gov/feeds/press_monetary.xml"
FED_FOMC_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
_EASTERN = ZoneInfo("America/New_York")
# The Fed's regular-meeting contract publishes the statement at 14:00 Eastern
# on day two; ZoneInfo owns EST/EDT conversion rather than a fixed UTC offset.
_FOMC_STATEMENT_TIME = time(14)
_YEAR_HEADING = re.compile(r"\b(20\d{2})\s+FOMC Meetings\b")
_DATE_RANGE = re.compile(r"^(\d{1,2})(?:\s*-\s*(\d{1,2}))?\*?$", re.ASCII)
_MONTHS = {
    name: number
    for number, name in enumerate(
        (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ),
        start=1,
    )
}
_MONTH_ALIASES = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Sept": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
    **_MONTHS,
}


class OfficialRecordKind(StrEnum):
    FOMC_MEETING = "FOMC_MEETING"
    FED_MONETARY_RELEASE = "FED_MONETARY_RELEASE"


class FomcMeetingRecord(FrozenModel):
    observation: SourceObservation
    kind: Literal[OfficialRecordKind.FOMC_MEETING] = OfficialRecordKind.FOMC_MEETING
    meeting_start: date
    meeting_end: date
    statement_at: datetime
    has_projection_materials: bool
    source_url: Literal[FED_FOMC_CALENDAR_URL] = FED_FOMC_CALENDAR_URL

    _utc_statement_at = field_validator("statement_at")(_require_utc)

    @model_validator(mode="after")
    def identity_and_schedule_are_consistent(self):
        if self.meeting_end < self.meeting_start:
            raise ValueError("FOMC meeting_end 不能早于 meeting_start")
        if self.statement_at.astimezone(_EASTERN).date() != self.meeting_end:
            raise ValueError("FOMC statement_at 必须落在会议结束日")
        if self.observation.source_id != FED_SOURCE_ID:
            raise ValueError("FOMC record 必须引用 Federal Reserve observation")
        return self


class FedMonetaryReleaseRecord(FrozenModel):
    observation: SourceObservation
    kind: Literal[OfficialRecordKind.FED_MONETARY_RELEASE] = OfficialRecordKind.FED_MONETARY_RELEASE
    title: str = Field(min_length=1, max_length=1_000)
    summary: str = Field(default="", max_length=4_000)
    source_url: str = Field(min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def identity_and_visibility_are_consistent(self):
        if self.observation.source_id != FED_SOURCE_ID:
            raise ValueError("Fed release 必须引用 Federal Reserve observation")
        if self.observation.source_published_at is None:
            raise ValueError("Fed release observation 必须包含来源发布时间")
        parsed = urlparse(self.source_url)
        if parsed.scheme != "https" or parsed.hostname not in {
            "federalreserve.gov",
            "www.federalreserve.gov",
        }:
            raise ValueError("Fed RSS 记录必须引用 federalreserve.gov HTTPS 页面")
        return self


def parse_fomc_calendar(
    html: str,
    *,
    observed_at: datetime,
    years: tuple[int, ...] | None = None,
) -> tuple[FomcMeetingRecord, ...]:
    """Parse the Fed calendar without assigning asset relevance or trigger priority."""

    observed_at = _require_utc(observed_at)
    parser = _FomcCalendarHtmlParser()
    parser.feed(html)
    parser.close()
    allowed_years = frozenset(years) if years is not None else None
    ordinal_by_year: dict[int, int] = {}
    records: list[FomcMeetingRecord] = []
    for raw in parser.meetings:
        year = raw.year
        if allowed_years is not None and year not in allowed_years:
            continue
        ordinal_by_year[year] = ordinal_by_year.get(year, 0) + 1
        ordinal = ordinal_by_year[year]
        meeting_start, meeting_end = _meeting_dates(year, raw.month, raw.date_text)
        statement_at = datetime.combine(
            meeting_end, _FOMC_STATEMENT_TIME, tzinfo=_EASTERN
        ).astimezone(UTC)
        source_record_id = f"fomc-regular-{year}-{ordinal:02d}"
        identity = {
            "source_record_id": source_record_id,
            "meeting_start": meeting_start.isoformat(),
            "meeting_end": meeting_end.isoformat(),
            "statement_at": statement_at.isoformat(),
            "has_projection_materials": raw.has_projection_materials,
        }
        payload_hash = content_hash(identity)
        observation_id = stable_id(
            "source_observation",
            FED_SOURCE_ID,
            source_record_id,
            payload_hash,
        )
        records.append(
            FomcMeetingRecord(
                observation=SourceObservation(
                    observation_id=observation_id,
                    source_id=FED_SOURCE_ID,
                    source_tier=SourceTier.FIRST_PARTY,
                    source_record_id=source_record_id,
                    observed_at=observed_at,
                    payload_hash=payload_hash,
                    payload_ref=f"sha256:{payload_hash}",
                ),
                meeting_start=meeting_start,
                meeting_end=meeting_end,
                statement_at=statement_at,
                has_projection_materials=raw.has_projection_materials,
            )
        )
    if not records:
        raise ValueError("Fed FOMC calendar 未解析到目标会议")
    return tuple(records)


def parse_fed_monetary_rss(
    xml: str,
    *,
    observed_at: datetime,
) -> tuple[FedMonetaryReleaseRecord, ...]:
    observed_at = _require_utc(observed_at)
    try:
        root = ElementTree.fromstring(xml.lstrip("\ufeff"))
    except ElementTree.ParseError as exc:
        raise ValueError("Fed monetary RSS XML 非法") from exc
    records: list[FedMonetaryReleaseRecord] = []
    for item in root.findall("./channel/item"):
        title = _required_xml_text(item, "title")
        link = _required_xml_text(item, "link")
        guid = _required_xml_text(item, "guid")
        published_at = _parse_rss_time(_required_xml_text(item, "pubDate"))
        summary = (item.findtext("description") or "").strip()
        identity = {
            "guid": guid,
            "title": title,
            "summary": summary,
            "published_at": published_at.isoformat(),
            "link": link,
        }
        payload_hash = content_hash(identity)
        observation_id = stable_id("source_observation", FED_SOURCE_ID, guid, payload_hash)
        records.append(
            FedMonetaryReleaseRecord(
                observation=SourceObservation(
                    observation_id=observation_id,
                    source_id=FED_SOURCE_ID,
                    source_tier=SourceTier.FIRST_PARTY,
                    source_record_id=guid,
                    observed_at=observed_at,
                    source_published_at=published_at,
                    payload_hash=payload_hash,
                    payload_ref=f"sha256:{payload_hash}",
                ),
                title=title,
                summary=summary,
                source_url=link,
            )
        )
    if not records:
        raise ValueError("Fed monetary RSS 不含 item")
    return tuple(records)


class _RawFomcMeeting:
    def __init__(self, year: int, month: str, date_text: str, row_text: str) -> None:
        self.year = year
        self.month = month
        self.date_text = date_text
        self.has_projection_materials = "Projection Materials" in row_text


class _FomcCalendarHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meetings: list[_RawFomcMeeting] = []
        self._depth = 0
        self._heading_depth: int | None = None
        self._heading_text: list[str] = []
        self._current_year: int | None = None
        self._row_depth: int | None = None
        self._row_text: list[str] = []
        self._month_depth: int | None = None
        self._month_text: list[str] = []
        self._date_depth: int | None = None
        self._date_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in {"div", "h4"}:
            return
        self._depth += 1
        classes = set(dict(attrs).get("class", "").split())
        if tag == "h4":
            self._heading_depth = self._depth
            self._heading_text = []
        if tag == "div" and "fomc-meeting" in classes:
            self._row_depth = self._depth
            self._row_text = []
            self._month_text = []
            self._date_text = []
        if self._row_depth is not None and tag == "div":
            if "fomc-meeting__month" in classes:
                self._month_depth = self._depth
            if "fomc-meeting__date" in classes:
                self._date_depth = self._depth

    def handle_data(self, data: str) -> None:
        if self._heading_depth is not None:
            self._heading_text.append(data)
        if self._row_depth is not None:
            self._row_text.append(data)
        if self._month_depth is not None:
            self._month_text.append(data)
        if self._date_depth is not None:
            self._date_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag not in {"div", "h4"}:
            return
        if tag == "h4" and self._heading_depth == self._depth:
            match = _YEAR_HEADING.search(_clean_text(self._heading_text))
            if match is not None:
                self._current_year = int(match.group(1))
            self._heading_depth = None
        if tag == "div" and self._month_depth == self._depth:
            self._month_depth = None
        if tag == "div" and self._date_depth == self._depth:
            self._date_depth = None
        if tag == "div" and self._row_depth == self._depth:
            if self._current_year is None:
                raise ValueError("Fed FOMC meeting 缺少年份标题")
            self.meetings.append(
                _RawFomcMeeting(
                    self._current_year,
                    _clean_text(self._month_text),
                    _clean_text(self._date_text),
                    _clean_text(self._row_text),
                )
            )
            self._row_depth = None
            self._month_depth = None
            self._date_depth = None
        self._depth -= 1


def _meeting_dates(year: int, month_text: str, date_text: str) -> tuple[date, date]:
    month_parts = [item.strip() for item in month_text.split("/")]
    try:
        months = tuple(_MONTH_ALIASES[item] for item in month_parts)
    except KeyError as exc:
        raise ValueError(f"未知 FOMC 月份: {month_text}") from exc
    if len(months) not in {1, 2}:
        raise ValueError("FOMC 月份范围非法")
    match = _DATE_RANGE.fullmatch(date_text)
    if match is None:
        raise ValueError(f"FOMC 日期范围非法: {date_text}")
    start_day = int(match.group(1))
    end_day = int(match.group(2) or start_day)
    start_month = months[0]
    if len(months) == 2:
        end_month = months[1]
    else:
        end_month = start_month + 1 if end_day < start_day else start_month
    end_year = year
    if end_month == 13:
        end_month = 1
        end_year += 1
    try:
        return date(year, start_month, start_day), date(end_year, end_month, end_day)
    except ValueError as exc:
        raise ValueError("FOMC 日期不存在") from exc


def _required_xml_text(item: ElementTree.Element, name: str) -> str:
    value = (item.findtext(name) or "").strip()
    if not value:
        raise ValueError(f"Fed monetary RSS item 缺少 {name}")
    return value


def _parse_rss_time(value: str) -> datetime:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Fed monetary RSS pubDate 非法") from exc
    if parsed.tzinfo is None:
        raise ValueError("Fed monetary RSS pubDate 缺少时区")
    return parsed.astimezone(UTC)


def _clean_text(parts: list[str]) -> str:
    return " ".join(" ".join(parts).split())
