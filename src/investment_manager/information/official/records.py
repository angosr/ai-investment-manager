from __future__ import annotations

import re
from datetime import UTC, date, datetime, time
from email.utils import parsedate_to_datetime
from enum import StrEnum
from html.parser import HTMLParser
from typing import ClassVar, Literal
from urllib.parse import urlparse
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator

from investment_manager.information.models import SourceObservation, SourceTier
from investment_manager.information.raw_payload import build_raw_source_payload
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel

FED_SOURCE_ID = "federal-reserve"
FED_MONETARY_RSS_URL = "https://www.federalreserve.gov/feeds/press_monetary.xml"
FED_FOMC_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
_EASTERN = ZoneInfo("America/New_York")
# The Fed's regular-meeting contract publishes the statement at 14:00 Eastern
# on day two; ZoneInfo owns EST/EDT conversion rather than a fixed UTC offset.
_FOMC_STATEMENT_TIME = time(14)
_YEAR_HEADING = re.compile(r"\b(20\d{2})\s+FOMC Meetings\b")
_DATE_RANGE = re.compile(r"^(\d{1,2})(?:\s*-\s*(\d{1,2}))?\*?$", re.ASCII)
_NOTATION_VOTE = re.compile(r"\(\s*notation\s+vote\s*\)", re.IGNORECASE)
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
    ECONOMIC_RELEASE_ACTUAL = "ECONOMIC_RELEASE_ACTUAL"
    ECONOMIC_RELEASE_EVENT = "ECONOMIC_RELEASE_EVENT"
    FOMC_MEETING = "FOMC_MEETING"
    FED_CHAIR_PUBLIC_EVENT = "FED_CHAIR_PUBLIC_EVENT"
    FED_MONETARY_RELEASE = "FED_MONETARY_RELEASE"
    FEDERAL_REGISTER_RULEMAKING = "FEDERAL_REGISTER_RULEMAKING"
    OFFICIAL_METRIC_SNAPSHOT = "OFFICIAL_METRIC_SNAPSHOT"
    TREASURY_BUYBACK_OPERATION = "TREASURY_BUYBACK_OPERATION"
    TREASURY_BUYBACK_RESULT = "TREASURY_BUYBACK_RESULT"


class CalendarEventStatus(StrEnum):
    SCHEDULED = "SCHEDULED"
    CANCELLED = "CANCELLED"


class FomcMeetingRecord(FrozenModel):
    observation: SourceObservation
    kind: Literal[OfficialRecordKind.FOMC_MEETING] = OfficialRecordKind.FOMC_MEETING
    meeting_start: date
    meeting_end: date
    statement_at: datetime
    has_projection_materials: bool
    source_url: Literal[FED_FOMC_CALENDAR_URL] = FED_FOMC_CALENDAR_URL

    _utc_statement_at = field_validator("statement_at")(require_utc)

    @model_validator(mode="after")
    def identity_and_schedule_are_consistent(self):
        if self.meeting_end < self.meeting_start:
            raise ValueError("FOMC meeting_end 不能早于 meeting_start")
        if self.statement_at.astimezone(_EASTERN).date() != self.meeting_end:
            raise ValueError("FOMC statement_at 必须落在会议结束日")
        if self.observation.source_id != FED_SOURCE_ID:
            raise ValueError("FOMC record 必须引用 Federal Reserve observation")
        _validate_record_observation(self, self.observation)
        return self


class FedMonetaryReleaseRecord(FrozenModel):
    observation: SourceObservation
    kind: Literal[OfficialRecordKind.FED_MONETARY_RELEASE] = OfficialRecordKind.FED_MONETARY_RELEASE
    title: str = Field(min_length=1, max_length=1_000)
    summary: str = Field(default="", max_length=4_000)
    policy_state: str | None = Field(default=None, min_length=1, max_length=2_000)
    document_url: str | None = Field(default=None, min_length=1, max_length=2_000)
    source_url: str = Field(min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def identity_and_visibility_are_consistent(self):
        if self.observation.source_id != FED_SOURCE_ID:
            raise ValueError("Fed release 必须引用 Federal Reserve observation")
        if self.observation.source_published_at is None:
            raise ValueError("Fed release observation 必须包含来源发布时间")
        for candidate in (self.source_url, self.document_url):
            if candidate is None:
                continue
            parsed = urlparse(candidate)
            if parsed.scheme != "https" or parsed.hostname not in {
                "federalreserve.gov",
                "www.federalreserve.gov",
            }:
                raise ValueError("Fed RSS 记录必须引用 federalreserve.gov HTTPS 页面")
        _validate_record_observation(self, self.observation)
        return self


OfficialRecord = FomcMeetingRecord | FedMonetaryReleaseRecord


def fed_policy_document_eligible(record: FedMonetaryReleaseRecord) -> bool:
    title = record.title.casefold()
    return title.startswith("minutes of the federal open market committee") or title == (
        "federal reserve issues fomc statement"
    )


def enrich_fed_monetary_release(
    record: FedMonetaryReleaseRecord,
    html: str,
    *,
    document_url: str,
    observed_at: datetime,
) -> FedMonetaryReleaseRecord:
    """Attach a bounded, source-faithful policy state from the linked Fed page.

    This extractor selects explicit sentences; it does not infer stance or asset
    direction.  A changed Fed page becomes a new observation of the same logical
    release and remains point-in-time auditable through its own raw payload.
    """

    if not fed_policy_document_eligible(record):
        raise ValueError("Fed release 不是 FOMC 政策文件")
    observed_at = require_utc(observed_at)
    raw = build_raw_source_payload(
        source_id=FED_SOURCE_ID,
        source_url=document_url,
        media_type="text/html",
        observed_at=observed_at,
        content=html.encode("utf-8"),
    )
    policy_state = _fed_policy_state(html)
    published_at = record.observation.source_published_at
    if published_at is None:
        raise ValueError("Fed release observation 必须包含来源发布时间")
    identity = {
        "guid": record.observation.source_record_id,
        "title": record.title,
        "summary": record.summary,
        "policy_state": policy_state,
        "document_url": document_url,
        "published_at": published_at.isoformat(),
        "link": record.source_url,
    }
    payload_hash = content_hash(identity)
    observation = SourceObservation(
        observation_id=stable_id(
            "source_observation",
            FED_SOURCE_ID,
            record.observation.source_record_id,
            payload_hash,
            observed_at.isoformat(),
        ),
        source_id=FED_SOURCE_ID,
        source_tier=SourceTier.FIRST_PARTY,
        source_record_id=record.observation.source_record_id,
        observed_at=observed_at,
        source_published_at=published_at,
        payload_hash=payload_hash,
        payload_ref=raw.payload_id,
    )
    return FedMonetaryReleaseRecord(
        observation=observation,
        title=record.title,
        summary=record.summary,
        policy_state=policy_state,
        document_url=document_url,
        source_url=record.source_url,
    )


class MarketCalendarEventRevision(FrozenModel):
    event_id: str
    revision_id: str
    previous_revision_id: str | None = None
    event_type: Literal[
        OfficialRecordKind.ECONOMIC_RELEASE_EVENT,
        OfficialRecordKind.FOMC_MEETING,
        OfficialRecordKind.FED_CHAIR_PUBLIC_EVENT,
        OfficialRecordKind.TREASURY_BUYBACK_OPERATION,
    ] = OfficialRecordKind.FOMC_MEETING
    status: CalendarEventStatus
    source_id: str
    source_record_id: str
    source_observation_id: str
    event_start_at: datetime
    event_end_at: datetime
    scheduled_release_at: datetime
    observed_at: datetime
    risk_factors: tuple[str, ...] = Field(min_length=1)
    has_projection_materials: bool
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    _utc_event_start = field_validator("event_start_at")(require_utc)
    _utc_event_end = field_validator("event_end_at")(require_utc)
    _utc_release = field_validator("scheduled_release_at")(require_utc)
    _utc_observed = field_validator("observed_at")(require_utc)

    @model_validator(mode="after")
    def identity_and_window_are_consistent(self):
        if self.event_start_at > self.event_end_at or (
            self.event_type == OfficialRecordKind.FOMC_MEETING
            and self.event_start_at == self.event_end_at
        ):
            raise ValueError("日历事件窗口非法")
        if not self.event_start_at <= self.scheduled_release_at <= self.event_end_at:
            raise ValueError("scheduled_release_at 必须位于事件窗口内")
        if self.previous_revision_id == self.revision_id:
            raise ValueError("日历修订不能引用自身")
        if tuple(sorted(set(self.risk_factors))) != self.risk_factors:
            raise ValueError("日历 risk_factors 必须唯一且排序")
        if self.event_id != stable_id(
            "market_calendar_event", self.source_id, self.source_record_id
        ):
            raise ValueError("日历 event_id 与来源逻辑身份不一致")
        if self.content_hash != content_hash(calendar_semantic_payload(self)):
            raise ValueError("日历 content_hash 与语义内容不一致")
        expected_revision_id = stable_id(
            "market_calendar_revision",
            self.event_id,
            self.source_observation_id,
            self.content_hash,
        )
        if self.revision_id != expected_revision_id:
            raise ValueError("日历 revision_id 与来源观测和内容不一致")
        return self


def build_fomc_calendar_revision(
    record: FomcMeetingRecord,
    *,
    previous: MarketCalendarEventRevision | None = None,
) -> MarketCalendarEventRevision:
    observation = record.observation
    event_id = stable_id(
        "market_calendar_event", observation.source_id, observation.source_record_id
    )
    if previous is not None:
        if previous.event_id != event_id:
            raise ValueError("前序日历修订不属于同一事件")
        if previous.observed_at >= observation.observed_at:
            raise ValueError("日历修订观察时间必须严格递增")
    event_start_at = datetime.combine(record.meeting_start, time.min, tzinfo=_EASTERN).astimezone(
        UTC
    )
    candidate = MarketCalendarEventRevision.model_construct(
        event_id=event_id,
        revision_id="pending",
        previous_revision_id=previous.revision_id if previous is not None else None,
        status=CalendarEventStatus.SCHEDULED,
        source_id=observation.source_id,
        source_record_id=observation.source_record_id,
        source_observation_id=observation.observation_id,
        event_start_at=event_start_at,
        event_end_at=record.statement_at,
        scheduled_release_at=record.statement_at,
        observed_at=observation.observed_at,
        risk_factors=("US_MONETARY_POLICY",),
        has_projection_materials=record.has_projection_materials,
        content_hash="pending",
    )
    semantic_hash = content_hash(calendar_semantic_payload(candidate))
    if previous is not None and previous.content_hash == semantic_hash:
        raise ValueError("相同日历语义不得创建新修订")
    return MarketCalendarEventRevision(
        **candidate.model_dump(exclude={"revision_id", "content_hash"}),
        content_hash=semantic_hash,
        revision_id=stable_id(
            "market_calendar_revision",
            event_id,
            observation.observation_id,
            semantic_hash,
        ),
    )


def parse_fomc_calendar(
    html: str,
    *,
    observed_at: datetime,
    years: tuple[int, ...] | None = None,
) -> tuple[FomcMeetingRecord, ...]:
    """Parse the Fed calendar without assigning asset relevance or trigger priority."""

    observed_at = require_utc(observed_at)
    raw_payload_ref = build_raw_source_payload(
        source_id=FED_SOURCE_ID,
        source_url=FED_FOMC_CALENDAR_URL,
        media_type="text/html",
        observed_at=observed_at,
        content=html.encode("utf-8"),
    ).payload_id
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
        # A notation-vote row documents a special release, not a regular
        # meeting governed by the 14:00 Eastern statement-time contract.
        if _NOTATION_VOTE.search(raw.date_text):
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
            observed_at.isoformat(),
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
                    payload_ref=raw_payload_ref,
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
    observed_at = require_utc(observed_at)
    raw_payload_ref = build_raw_source_payload(
        source_id=FED_SOURCE_ID,
        source_url=FED_MONETARY_RSS_URL,
        media_type="application/rss+xml",
        observed_at=observed_at,
        content=xml.encode("utf-8"),
    ).payload_id
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
        observation_id = stable_id(
            "source_observation",
            FED_SOURCE_ID,
            guid,
            payload_hash,
            observed_at.isoformat(),
        )
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
                    payload_ref=raw_payload_ref,
                ),
                title=title,
                summary=summary,
                source_url=link,
            )
        )
    if not records:
        raise ValueError("Fed monetary RSS 不含 item")
    return tuple(records)


def _fed_policy_state(html: str) -> str:
    parser = _FedPolicyTextParser()
    parser.feed(html)
    parser.close()
    text = _clean_text(parser.parts)
    sentences = tuple(
        item.strip()
        for item in re.split(r'(?<=[.!?])(?:["”])?\s+', text)
        if item.strip()
    )
    selectors = (
        (
            "action",
            (
                "the committee decided to maintain the target range",
                "the committee decided to raise the target range",
                "the committee decided to lower the target range",
            ),
            1,
        ),
        (
            "expectations",
            (
                "market was fully pricing",
                "market priced in",
                "median respondent to the desk survey",
                "market-implied expected path",
            ),
            2,
        ),
        (
            "constraints",
            (
                "inflation remained elevated",
                "labor market conditions remained",
                "economic activity had continued",
            ),
            2,
        ),
        (
            "path",
            (
                "policy tightening would likely be necessary",
                "policy easing would likely be appropriate",
                "more restrictive policy stance",
            ),
            1,
        ),
        (
            "division",
            (
                "voted against the decision",
                "participants favored an increase",
                "participants favored a decrease",
                "members voted against",
            ),
            1,
        ),
        (
            "balance_sheet",
            (
                "reserve management purchases",
                "balance sheet policy",
                "reserves in the system appeared",
            ),
            1,
        ),
    )
    fields: list[tuple[str, str]] = []
    for name, markers, maximum in selectors:
        matches: list[str] = []
        for marker in markers:
            for sentence in sentences:
                marker_offset = sentence.casefold().find(marker)
                if marker_offset < 0:
                    continue
                # Strip embedded statement boilerplate and minutes section headings,
                # but preserve the actor/time qualifiers in expectations and paths.
                start = marker_offset if name in {"action", "constraints"} else 0
                compact = sentence[start : start + 320].rstrip()
                if start:
                    compact = compact[0].upper() + compact[1:]
                if compact not in matches:
                    matches.append(compact)
                break
            if len(matches) >= maximum:
                break
        if matches:
            fields.append((name, " ".join(matches)))
    if not any(name == "action" for name, _ in fields):
        raise ValueError("Fed FOMC 文件缺少可验证政策行动")
    return _bounded_policy_state(fields, maximum_characters=1_200)


def _bounded_policy_state(
    fields: list[tuple[str, str]], *, maximum_characters: int
) -> str:
    """Bound all semantic slots fairly instead of chopping off the final slot."""

    label_cost = sum(len(name) + 1 for name, _ in fields) + 2 * (len(fields) - 1)
    remaining = maximum_characters - label_cost
    if remaining < len(fields):
        raise ValueError("Fed policy state 字符预算不足")
    limits = [0] * len(fields)
    active = {index for index, (_, value) in enumerate(fields) if value}
    while remaining and active:
        share = max(1, remaining // len(active))
        for index in tuple(sorted(active)):
            if not remaining:
                break
            room = len(fields[index][1]) - limits[index]
            take = min(room, share, remaining)
            limits[index] += take
            remaining -= take
            if limits[index] == len(fields[index][1]):
                active.remove(index)
    values = []
    for (name, value), limit in zip(fields, limits, strict=True):
        compact = value[:limit].rstrip()
        if limit < len(value) and " " in compact:
            compact = compact.rsplit(" ", 1)[0]
        values.append(f"{name}={compact}")
    return "; ".join(values)


class _FedPolicyTextParser(HTMLParser):
    _IGNORED: ClassVar[frozenset[str]] = frozenset(
        {"script", "style", "nav", "header", "footer", "noscript"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.casefold() in self._IGNORED:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in self._IGNORED and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth and data.strip():
            self.parts.append(data)


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


def calendar_semantic_payload(revision: MarketCalendarEventRevision) -> dict:
    return {
        "event_id": revision.event_id,
        "event_type": revision.event_type.value,
        "status": revision.status.value,
        "source_id": revision.source_id,
        "source_record_id": revision.source_record_id,
        "event_start_at": revision.event_start_at.isoformat(),
        "event_end_at": revision.event_end_at.isoformat(),
        "scheduled_release_at": revision.scheduled_release_at.isoformat(),
        "risk_factors": revision.risk_factors,
        "has_projection_materials": revision.has_projection_materials,
    }


def _validate_record_observation(
    record: OfficialRecord,
    observation: SourceObservation,
) -> None:
    validate_official_record_observation(
        observation,
        _official_record_payload(record),
    )


def validate_official_record_observation(
    observation: SourceObservation,
    payload: dict,
) -> None:
    expected_payload_hash = content_hash(payload)
    if observation.payload_hash != expected_payload_hash:
        raise ValueError("官方记录 payload_hash 与解析内容不一致")
    expected_id = stable_id(
        "source_observation",
        observation.source_id,
        observation.source_record_id,
        observation.payload_hash,
        observation.observed_at.isoformat(),
    )
    if observation.observation_id != expected_id:
        raise ValueError("官方记录 observation_id 与来源、内容和观察时间不一致")
    if not observation.payload_ref.startswith("raw_source_payload_"):
        raise ValueError("官方记录 payload_ref 必须引用原始来源 payload")


def _official_record_payload(record: OfficialRecord) -> dict:
    if isinstance(record, FomcMeetingRecord):
        return {
            "source_record_id": record.observation.source_record_id,
            "meeting_start": record.meeting_start.isoformat(),
            "meeting_end": record.meeting_end.isoformat(),
            "statement_at": record.statement_at.isoformat(),
            "has_projection_materials": record.has_projection_materials,
        }
    published_at = record.observation.source_published_at
    if published_at is None:
        raise ValueError("Fed release observation 必须包含来源发布时间")
    return {
        "guid": record.observation.source_record_id,
        "title": record.title,
        "summary": record.summary,
        **({"policy_state": record.policy_state} if record.policy_state else {}),
        **({"document_url": record.document_url} if record.document_url else {}),
        "published_at": published_at.isoformat(),
        "link": record.source_url,
    }


def _clean_text(parts: list[str]) -> str:
    return " ".join(" ".join(parts).split())
