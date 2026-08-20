from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, time
from html import unescape
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator

from investment_manager.information.models import SourceObservation, SourceTier
from investment_manager.information.official.records import (
    FED_SOURCE_ID,
    CalendarEventStatus,
    MarketCalendarEventRevision,
    OfficialRecordKind,
    calendar_semantic_payload,
    validate_official_record_observation,
)
from investment_manager.information.raw_payload import build_raw_source_payload
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel

FED_PUBLIC_CALENDAR_URL = "https://www.federalreserve.gov/json/calendar.json"
_EASTERN = ZoneInfo("America/New_York")
_CALENDAR_MONTH = re.compile(r"^(20\d{2})-(0[1-9]|1[0-2])$", re.ASCII)
_CALENDAR_TIME = re.compile(
    r"^(1[0-2]|[1-9]):([0-5]\d)\s+([ap])\.m\.$",
    re.IGNORECASE,
)
_BOARD_CHAIR_TITLE = re.compile(
    r"\s-\sChair(?:man|woman)?\s+\S",
    re.IGNORECASE,
)


class FedChairPublicEventRecord(FrozenModel):
    observation: SourceObservation
    kind: Literal[OfficialRecordKind.FED_CHAIR_PUBLIC_EVENT] = (
        OfficialRecordKind.FED_CHAIR_PUBLIC_EVENT
    )
    status: CalendarEventStatus
    scheduled_at: datetime
    event_category: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=1_000)
    description: str = Field(default="", max_length=2_000)
    location: str = Field(default="", max_length=2_000)
    event_link: str | None = Field(default=None, max_length=2_000)
    live_url: str | None = Field(default=None, max_length=2_000)
    source_url: Literal[FED_PUBLIC_CALENDAR_URL] = FED_PUBLIC_CALENDAR_URL

    _utc_scheduled_at = field_validator("scheduled_at")(require_utc)

    @property
    def calendar_year(self) -> int:
        return self.scheduled_at.astimezone(_EASTERN).year

    @model_validator(mode="after")
    def identity_and_schedule_are_consistent(self):
        if self.observation.source_id != FED_SOURCE_ID:
            raise ValueError("Fed Chair 日历记录必须引用 Federal Reserve observation")
        if _BOARD_CHAIR_TITLE.search(self.title) is None:
            raise ValueError("Fed Chair 日历记录 title 不属于 Board Chair")
        expected_record_id = _source_record_id(
            year=self.calendar_year,
            event_category=self.event_category,
            title=self.title,
            description=self.description,
            location=self.location,
        )
        if self.observation.source_record_id != expected_record_id:
            raise ValueError("Fed Chair 日历 source_record_id 与逻辑事件身份不一致")
        validate_official_record_observation(
            self.observation,
            _record_payload(self),
        )
        return self


class FedChairCalendarSnapshot(FrozenModel):
    covered_years: tuple[int, ...] = Field(min_length=1)
    records: tuple[FedChairPublicEventRecord, ...]

    @model_validator(mode="after")
    def coverage_and_records_are_consistent(self):
        if tuple(sorted(set(self.covered_years))) != self.covered_years:
            raise ValueError("Fed public calendar covered_years 必须唯一且排序")
        record_ids = tuple(item.observation.source_record_id for item in self.records)
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("Fed public calendar Board Chair 逻辑事件身份冲突")
        if any(
            item.scheduled_at.astimezone(_EASTERN).year not in self.covered_years
            for item in self.records
        ):
            raise ValueError("Fed public calendar 记录超出已声明覆盖年份")
        return self


def build_fed_chair_calendar_revision(
    record: FedChairPublicEventRecord,
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
    candidate = MarketCalendarEventRevision.model_construct(
        event_id=event_id,
        revision_id="pending",
        previous_revision_id=previous.revision_id if previous is not None else None,
        event_type=OfficialRecordKind.FED_CHAIR_PUBLIC_EVENT,
        status=record.status,
        source_id=observation.source_id,
        source_record_id=observation.source_record_id,
        source_observation_id=observation.observation_id,
        event_start_at=record.scheduled_at,
        event_end_at=record.scheduled_at,
        scheduled_release_at=record.scheduled_at,
        observed_at=observation.observed_at,
        risk_factors=("US_MONETARY_POLICY",),
        has_projection_materials=False,
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


def parse_fed_chair_calendar(
    payload: str,
    *,
    observed_at: datetime,
    years: tuple[int, ...] | None = None,
) -> FedChairCalendarSnapshot:
    """Parse the Fed public calendar and retain only Board Chair public events."""

    observed_at = require_utc(observed_at)
    try:
        document = json.loads(payload.lstrip("\ufeff"))
    except json.JSONDecodeError as exc:
        raise ValueError("Fed public calendar JSON 非法") from exc
    raw_events = document.get("events") if isinstance(document, dict) else None
    if not isinstance(raw_events, list) or not raw_events:
        raise ValueError("Fed public calendar 缺少非空 events")
    allowed_years = frozenset(years) if years is not None else None
    covered_years: set[int] = set()
    parsed: list[FedChairPublicEventRecord] = []
    raw_payload_ref = build_raw_source_payload(
        source_id=FED_SOURCE_ID,
        source_url=FED_PUBLIC_CALENDAR_URL,
        media_type="application/json",
        observed_at=observed_at,
        content=payload.encode("utf-8"),
    ).payload_id
    for raw in raw_events:
        if not isinstance(raw, dict):
            raise ValueError("Fed public calendar event 必须是对象")
        month_text = _optional_text(raw, "month")
        if not month_text:
            # The official feed currently carries one empty sentinel row.
            continue
        month_match = _CALENDAR_MONTH.fullmatch(month_text)
        if month_match is None:
            raise ValueError(f"Fed public calendar month 非法: {month_text}")
        year = int(month_match.group(1))
        month = int(month_match.group(2))
        covered_years.add(year)
        if allowed_years is not None and year not in allowed_years:
            continue
        title = _required_text(raw, "title")
        if _BOARD_CHAIR_TITLE.search(title) is None:
            continue
        day_tokens = tuple(item.strip() for item in _required_text(raw, "days").split(","))
        if len(day_tokens) != 1 or not day_tokens[0].isdigit():
            raise ValueError("Fed Chair public event 必须具有唯一明确日期")
        try:
            local_date = date(year, month, int(day_tokens[0]))
        except ValueError as exc:
            raise ValueError("Fed Chair public event 日期不存在") from exc
        local_time = _parse_time(_required_text(raw, "time"))
        scheduled_at = datetime.combine(
            local_date,
            local_time,
            tzinfo=_EASTERN,
        ).astimezone(UTC)
        event_category = _required_text(raw, "type")
        description = _optional_text(raw, "description")
        location = _optional_text(raw, "location")
        source_record_id = _source_record_id(
            year=year,
            event_category=event_category,
            title=title,
            description=description,
            location=location,
        )
        values = _record_payload_values(
            source_record_id=source_record_id,
            status=CalendarEventStatus.SCHEDULED,
            scheduled_at=scheduled_at,
            event_category=event_category,
            title=title,
            description=description,
            location=location,
            event_link=_optional_text(raw, "link") or None,
            live_url=_optional_text(raw, "live") or None,
        )
        payload_hash = content_hash(values)
        observation_id = stable_id(
            "source_observation",
            FED_SOURCE_ID,
            source_record_id,
            payload_hash,
            observed_at.isoformat(),
        )
        parsed.append(
            FedChairPublicEventRecord(
                observation=SourceObservation(
                    observation_id=observation_id,
                    source_id=FED_SOURCE_ID,
                    source_tier=SourceTier.FIRST_PARTY,
                    source_record_id=source_record_id,
                    observed_at=observed_at,
                    payload_hash=payload_hash,
                    payload_ref=raw_payload_ref,
                ),
                status=CalendarEventStatus.SCHEDULED,
                scheduled_at=scheduled_at,
                event_category=event_category,
                title=title,
                description=description,
                location=location,
                event_link=values["event_link"],
                live_url=values["live_url"],
            )
        )
    effective_coverage = (
        covered_years & allowed_years if allowed_years is not None else covered_years
    )
    if not effective_coverage:
        raise ValueError("Fed public calendar 未覆盖目标年份")
    return FedChairCalendarSnapshot(
        covered_years=tuple(sorted(effective_coverage)),
        records=tuple(sorted(parsed, key=lambda item: (item.scheduled_at, item.title))),
    )


def build_fed_chair_cancellation(
    record: FedChairPublicEventRecord,
    *,
    observed_at: datetime,
    payload_ref: str,
) -> FedChairPublicEventRecord:
    observed_at = require_utc(observed_at)
    if record.status != CalendarEventStatus.SCHEDULED:
        raise ValueError("只有已安排的 Fed Chair event 可以取消")
    if observed_at <= record.observation.observed_at:
        raise ValueError("Fed Chair event 取消观察时间必须严格递增")
    values = {
        **_record_payload(record),
        "status": CalendarEventStatus.CANCELLED.value,
    }
    payload_hash = content_hash(values)
    observation_id = stable_id(
        "source_observation",
        FED_SOURCE_ID,
        record.observation.source_record_id,
        payload_hash,
        observed_at.isoformat(),
    )
    return FedChairPublicEventRecord(
        observation=SourceObservation(
            observation_id=observation_id,
            source_id=FED_SOURCE_ID,
            source_tier=SourceTier.FIRST_PARTY,
            source_record_id=record.observation.source_record_id,
            observed_at=observed_at,
            payload_hash=payload_hash,
            payload_ref=payload_ref,
        ),
        status=CalendarEventStatus.CANCELLED,
        scheduled_at=record.scheduled_at,
        event_category=record.event_category,
        title=record.title,
        description=record.description,
        location=record.location,
        event_link=record.event_link,
        live_url=record.live_url,
    )


def _record_payload(record: FedChairPublicEventRecord) -> dict:
    return _record_payload_values(
        source_record_id=record.observation.source_record_id,
        status=record.status,
        scheduled_at=record.scheduled_at,
        event_category=record.event_category,
        title=record.title,
        description=record.description,
        location=record.location,
        event_link=record.event_link,
        live_url=record.live_url,
    )


def _record_payload_values(
    *,
    source_record_id: str,
    status: CalendarEventStatus,
    scheduled_at: datetime,
    event_category: str,
    title: str,
    description: str,
    location: str,
    event_link: str | None,
    live_url: str | None,
) -> dict:
    return {
        "source_record_id": source_record_id,
        "status": status.value,
        "scheduled_at": scheduled_at.isoformat(),
        "event_category": event_category,
        "title": title,
        "description": description,
        "location": location,
        "event_link": event_link,
        "live_url": live_url,
    }


def _parse_time(value: str) -> time:
    match = _CALENDAR_TIME.fullmatch(value)
    if match is None:
        raise ValueError(f"Fed public calendar time 非法: {value}")
    hour = int(match.group(1)) % 12
    if match.group(3).casefold() == "p":
        hour += 12
    return time(hour, int(match.group(2)))


def _required_text(mapping: dict, name: str) -> str:
    value = _optional_text(mapping, name)
    if not value:
        raise ValueError(f"Fed public calendar event 缺少 {name}")
    return value


def _optional_text(mapping: dict, name: str) -> str:
    raw = mapping.get(name)
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError(f"Fed public calendar event {name} 必须是字符串")
    return " ".join(unescape(raw).split())


def _source_record_id(
    *,
    year: int,
    event_category: str,
    title: str,
    description: str,
    location: str,
) -> str:
    # Schedule fields are deliberately excluded so a time/date change is a
    # revision of one logical event. Topic and venue distinguish same-title
    # congressional appearances and fail closed on an ambiguous collision.
    return stable_id(
        "fed_chair_public_event",
        year,
        event_category.casefold(),
        title.casefold(),
        description.casefold(),
        location.casefold(),
    )
