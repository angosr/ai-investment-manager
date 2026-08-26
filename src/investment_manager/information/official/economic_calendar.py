from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator

from investment_manager.information.models import SourceObservation, SourceTier
from investment_manager.information.official.records import (
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

BLS_CALENDAR_SOURCE_ID = "bureau-of-labor-statistics"
BLS_CALENDAR_STREAM_ID = "bls-economic-release-calendar"
BLS_CALENDAR_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
BEA_CALENDAR_SOURCE_ID = "bureau-of-economic-analysis"
BEA_CALENDAR_STREAM_ID = "bea-economic-release-calendar"
BEA_CALENDAR_URL = "https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics"

_EASTERN = ZoneInfo("America/New_York")
_SOURCE_BY_URL = {
    BLS_CALENDAR_URL: BLS_CALENDAR_SOURCE_ID,
    BEA_CALENDAR_URL: BEA_CALENDAR_SOURCE_ID,
}


class EconomicReleaseKind(StrEnum):
    US_CPI = "US_CPI"
    US_EMPLOYMENT = "US_EMPLOYMENT"
    US_GDP = "US_GDP"
    US_PCE = "US_PCE"


_RISK_FACTORS = {
    EconomicReleaseKind.US_CPI: ("US_INFLATION",),
    EconomicReleaseKind.US_EMPLOYMENT: ("US_EMPLOYMENT",),
    EconomicReleaseKind.US_GDP: ("US_GROWTH",),
    EconomicReleaseKind.US_PCE: ("US_INFLATION",),
}


class EconomicReleaseEventRecord(FrozenModel):
    """One decision-relevant event from a first-party economic release calendar."""

    observation: SourceObservation
    kind: Literal[OfficialRecordKind.ECONOMIC_RELEASE_EVENT] = (
        OfficialRecordKind.ECONOMIC_RELEASE_EVENT
    )
    status: CalendarEventStatus
    release_kind: EconomicReleaseKind
    scheduled_at: datetime
    title: str = Field(min_length=1, max_length=1_000)
    source_url: str = Field(min_length=1, max_length=2_000)

    _utc_scheduled_at = field_validator("scheduled_at")(require_utc)

    @model_validator(mode="after")
    def identity_and_source_are_consistent(self):
        expected_source = _SOURCE_BY_URL.get(self.source_url)
        if expected_source is None or self.observation.source_id != expected_source:
            raise ValueError("经济发布日历记录来源非法")
        if self.observation.source_tier != SourceTier.FIRST_PARTY:
            raise ValueError("经济发布日历必须是一手来源")
        parsed = urlparse(self.source_url)
        if parsed.scheme != "https" or parsed.hostname not in {
            "www.bls.gov",
            "www.bea.gov",
        }:
            raise ValueError("经济发布日历必须引用固定官方 HTTPS 地址")
        validate_official_record_observation(
            self.observation,
            economic_release_semantic_payload(self),
        )
        return self


class EconomicReleaseCalendarSnapshot(FrozenModel):
    source_id: str
    covered_years: tuple[int, ...] = Field(min_length=1)
    records: tuple[EconomicReleaseEventRecord, ...]

    @model_validator(mode="after")
    def coverage_and_records_are_consistent(self):
        if tuple(sorted(set(self.covered_years))) != self.covered_years:
            raise ValueError("经济发布日历 covered_years 必须唯一且排序")
        if any(item.observation.source_id != self.source_id for item in self.records):
            raise ValueError("经济发布日历快照混入其他来源")
        record_ids = tuple(item.observation.source_record_id for item in self.records)
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("经济发布日历逻辑事件身份冲突")
        return self


def parse_economic_release_calendar(
    content: bytes,
    *,
    source_url: str,
    observed_at: datetime,
) -> EconomicReleaseCalendarSnapshot:
    """Parse a bounded ICS contract and retain only four market-relevant releases."""

    observed_at = require_utc(observed_at)
    source_id = _SOURCE_BY_URL.get(source_url)
    if source_id is None:
        raise ValueError("不支持的经济发布日历 URL")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("经济发布日历不是 UTF-8") from exc
    events = _parse_ics_events(text)
    if not events:
        raise ValueError("经济发布日历缺少 VEVENT")
    raw = build_raw_source_payload(
        source_id=source_id,
        source_url=source_url,
        media_type="text/calendar",
        observed_at=observed_at,
        content=content,
    )
    covered_years: set[int] = set()
    records: list[EconomicReleaseEventRecord] = []
    for event in events:
        scheduled_at = _event_time(event)
        covered_years.add(scheduled_at.year)
        title = _unescape_ics_text(_required_property(event, "SUMMARY"))
        release_kind = _release_kind(source_id, title)
        if release_kind is None:
            continue
        source_record_id = _required_property(event, "UID")
        status = (
            CalendarEventStatus.CANCELLED
            if _property(event, "STATUS").upper() == "CANCELLED"
            else CalendarEventStatus.SCHEDULED
        )
        values = _semantic_values(
            source_record_id=source_record_id,
            status=status,
            release_kind=release_kind,
            scheduled_at=scheduled_at,
            title=title,
            source_url=source_url,
        )
        payload_hash = content_hash(values)
        records.append(
            EconomicReleaseEventRecord(
                observation=SourceObservation(
                    observation_id=stable_id(
                        "source_observation",
                        source_id,
                        source_record_id,
                        payload_hash,
                        observed_at.isoformat(),
                    ),
                    source_id=source_id,
                    source_tier=SourceTier.FIRST_PARTY,
                    source_record_id=source_record_id,
                    observed_at=observed_at,
                    payload_hash=payload_hash,
                    payload_ref=raw.payload_id,
                ),
                status=status,
                release_kind=release_kind,
                scheduled_at=scheduled_at,
                title=title,
                source_url=source_url,
            )
        )
    if not covered_years:
        raise ValueError("经济发布日历没有可识别年份")
    return EconomicReleaseCalendarSnapshot(
        source_id=source_id,
        covered_years=tuple(sorted(covered_years)),
        records=tuple(
            sorted(records, key=lambda item: (item.scheduled_at, item.release_kind.value))
        ),
    )


def build_economic_release_calendar_revision(
    record: EconomicReleaseEventRecord,
    *,
    previous: MarketCalendarEventRevision | None = None,
) -> MarketCalendarEventRevision:
    observation = record.observation
    event_id = stable_id(
        "market_calendar_event",
        observation.source_id,
        observation.source_record_id,
    )
    if previous is not None:
        if previous.event_id != event_id:
            raise ValueError("前序经济日历修订不属于同一事件")
        if previous.observed_at >= observation.observed_at:
            raise ValueError("经济日历修订观察时间必须严格递增")
    candidate = MarketCalendarEventRevision.model_construct(
        event_id=event_id,
        revision_id="pending",
        previous_revision_id=previous.revision_id if previous is not None else None,
        event_type=OfficialRecordKind.ECONOMIC_RELEASE_EVENT,
        status=record.status,
        source_id=observation.source_id,
        source_record_id=observation.source_record_id,
        source_observation_id=observation.observation_id,
        event_start_at=record.scheduled_at,
        event_end_at=record.scheduled_at,
        scheduled_release_at=record.scheduled_at,
        observed_at=observation.observed_at,
        risk_factors=_RISK_FACTORS[record.release_kind],
        has_projection_materials=False,
        content_hash="pending",
    )
    semantic_hash = content_hash(calendar_semantic_payload(candidate))
    if previous is not None and previous.content_hash == semantic_hash:
        raise ValueError("相同经济日历语义不得创建新修订")
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


def build_economic_release_cancellation(
    record: EconomicReleaseEventRecord,
    *,
    observed_at: datetime,
    payload_ref: str,
) -> EconomicReleaseEventRecord:
    observed_at = require_utc(observed_at)
    if record.status != CalendarEventStatus.SCHEDULED:
        raise ValueError("只有已安排的经济发布可以取消")
    if observed_at <= record.observation.observed_at:
        raise ValueError("经济发布取消观察时间必须严格递增")
    values = _semantic_values(
        source_record_id=record.observation.source_record_id,
        status=CalendarEventStatus.CANCELLED,
        release_kind=record.release_kind,
        scheduled_at=record.scheduled_at,
        title=record.title,
        source_url=record.source_url,
    )
    payload_hash = content_hash(values)
    return EconomicReleaseEventRecord(
        observation=SourceObservation(
            observation_id=stable_id(
                "source_observation",
                record.observation.source_id,
                record.observation.source_record_id,
                payload_hash,
                observed_at.isoformat(),
            ),
            source_id=record.observation.source_id,
            source_tier=SourceTier.FIRST_PARTY,
            source_record_id=record.observation.source_record_id,
            observed_at=observed_at,
            payload_hash=payload_hash,
            payload_ref=payload_ref,
        ),
        status=CalendarEventStatus.CANCELLED,
        release_kind=record.release_kind,
        scheduled_at=record.scheduled_at,
        title=record.title,
        source_url=record.source_url,
    )


def economic_release_semantic_payload(record: EconomicReleaseEventRecord) -> dict:
    return _semantic_values(
        source_record_id=record.observation.source_record_id,
        status=record.status,
        release_kind=record.release_kind,
        scheduled_at=record.scheduled_at,
        title=record.title,
        source_url=record.source_url,
    )


def _semantic_values(
    *,
    source_record_id: str,
    status: CalendarEventStatus,
    release_kind: EconomicReleaseKind,
    scheduled_at: datetime,
    title: str,
    source_url: str,
) -> dict:
    return {
        "source_record_id": source_record_id,
        "status": status.value,
        "release_kind": release_kind.value,
        "scheduled_at": scheduled_at.isoformat(),
        "title": title,
        "source_url": source_url,
    }


def _release_kind(source_id: str, title: str) -> EconomicReleaseKind | None:
    normalized = " ".join(title.split())
    if source_id == BLS_CALENDAR_SOURCE_ID:
        return {
            "Consumer Price Index": EconomicReleaseKind.US_CPI,
            "Employment Situation": EconomicReleaseKind.US_EMPLOYMENT,
        }.get(normalized)
    if normalized.startswith("Personal Income and Outlays,"):
        return EconomicReleaseKind.US_PCE
    if normalized.startswith("GDP ("):
        return EconomicReleaseKind.US_GDP
    return None


def _parse_ics_events(text: str) -> tuple[dict[str, tuple[str, str]], ...]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw.startswith((" ", "\t")):
            if not lines:
                raise ValueError("经济发布日历首行不能折叠")
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    events: list[dict[str, tuple[str, str]]] = []
    current: dict[str, tuple[str, str]] | None = None
    for line in lines:
        if line == "BEGIN:VEVENT":
            if current is not None:
                raise ValueError("经济发布日历 VEVENT 不得嵌套")
            current = {}
            continue
        if line == "END:VEVENT":
            if current is None:
                raise ValueError("经济发布日历 VEVENT 边界非法")
            events.append(current)
            current = None
            continue
        if current is None or not line:
            continue
        try:
            head, value = line.split(":", 1)
        except ValueError as exc:
            raise ValueError("经济发布日历属性缺少冒号") from exc
        name, _, parameters = head.partition(";")
        canonical = name.upper()
        if canonical in {"DTSTART", "STATUS", "SUMMARY", "UID"}:
            if canonical in current:
                raise ValueError(f"经济发布日历 {canonical} 重复")
            current[canonical] = (parameters, value.strip())
    if current is not None:
        raise ValueError("经济发布日历 VEVENT 未闭合")
    return tuple(events)


def _event_time(event: dict[str, tuple[str, str]]) -> datetime:
    parameters, value = event.get("DTSTART", ("", ""))
    if not value:
        raise ValueError("经济发布日历事件缺少 DTSTART")
    try:
        if value.endswith("Z"):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        timezone = None
        for item in parameters.split(";"):
            key, _, candidate = item.partition("=")
            if key.upper() == "TZID":
                timezone = candidate
        if timezone not in {"US-Eastern", "America/New_York"}:
            raise ValueError("经济发布日历本地时间缺少 US Eastern TZID")
        return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=_EASTERN).astimezone(UTC)
    except ValueError as exc:
        if str(exc).startswith("经济发布日历"):
            raise
        raise ValueError("经济发布日历 DTSTART 非法") from exc


def _property(event: dict[str, tuple[str, str]], name: str) -> str:
    return event.get(name, ("", ""))[1].strip()


def _required_property(event: dict[str, tuple[str, str]], name: str) -> str:
    value = _property(event, name)
    if not value:
        raise ValueError(f"经济发布日历事件缺少 {name}")
    return value


def _unescape_ics_text(value: str) -> str:
    return " ".join(
        value.replace("\\n", " ")
        .replace("\\N", " ")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .split()
    )
