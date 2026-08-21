from __future__ import annotations

import re
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Literal
from xml.etree import ElementTree
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

TREASURY_BUYBACK_SOURCE_ID = "us-treasury-buybacks"
TREASURY_BUYBACK_STREAM_ID = "treasury-buyback-schedule"
TREASURY_BUYBACK_URL = (
    "https://home.treasury.gov/system/files/221/Tentative-Buyback-Schedule.xml"
)
_EASTERN = ZoneInfo("America/New_York")
_VERSION_SUFFIX = re.compile(r"\s+V\d+$", re.IGNORECASE)


class TreasuryBuybackOperationRecord(FrozenModel):
    observation: SourceObservation
    kind: Literal[OfficialRecordKind.TREASURY_BUYBACK_OPERATION] = (
        OfficialRecordKind.TREASURY_BUYBACK_OPERATION
    )
    status: CalendarEventStatus
    calendar_cycle: str = Field(min_length=1, max_length=200)
    operation_start_at: datetime
    operation_end_at: datetime
    settlement_date: date
    operation_type: str = Field(min_length=1, max_length=120)
    security_type: str = Field(min_length=1, max_length=120)
    maturity_bucket: str = Field(min_length=1, max_length=200)
    maturity_start: date
    maturity_end: date
    minimum_purchase_usd_m: Decimal = Field(ge=0)
    maximum_purchase_usd_m: Decimal = Field(gt=0)
    source_url: Literal[TREASURY_BUYBACK_URL] = TREASURY_BUYBACK_URL

    _utc_start = field_validator("operation_start_at")(require_utc)
    _utc_end = field_validator("operation_end_at")(require_utc)

    @model_validator(mode="after")
    def identity_and_window_are_consistent(self):
        if self.operation_end_at <= self.operation_start_at:
            raise ValueError("Treasury buyback operation 时间窗口非法")
        if self.maturity_end < self.maturity_start:
            raise ValueError("Treasury buyback maturity 范围非法")
        if self.minimum_purchase_usd_m > self.maximum_purchase_usd_m:
            raise ValueError("Treasury buyback 最低金额不能高于最高金额")
        if self.observation.source_id != TREASURY_BUYBACK_SOURCE_ID:
            raise ValueError("Treasury buyback 必须引用固定财政部来源")
        if self.observation.source_record_id != stable_id(
            "treasury_buyback_operation",
            self.calendar_cycle.casefold(),
            self.operation_start_at.isoformat(),
        ):
            raise ValueError("Treasury buyback source_record_id 与操作身份不一致")
        validate_official_record_observation(
            self.observation,
            treasury_buyback_semantic_payload(self),
        )
        return self


class TreasuryBuybackCalendarSnapshot(FrozenModel):
    calendar_cycle: str = Field(min_length=1, max_length=200)
    covered_start: date
    covered_end: date
    records: tuple[TreasuryBuybackOperationRecord, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def coverage_and_records_are_consistent(self):
        if self.covered_end < self.covered_start:
            raise ValueError("Treasury buyback calendar 覆盖区间非法")
        record_ids = tuple(item.observation.source_record_id for item in self.records)
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("Treasury buyback calendar 逻辑事件身份冲突")
        if any(item.calendar_cycle != self.calendar_cycle for item in self.records):
            raise ValueError("Treasury buyback record 不属于声明的日历周期")
        return self


def parse_treasury_buyback_calendar(
    content: bytes,
    *,
    observed_at: datetime,
) -> TreasuryBuybackCalendarSnapshot:
    observed_at = require_utc(observed_at)
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise ValueError("Treasury buyback calendar XML 非法") from exc
    if root.tag != "BuyBackCalendar":
        raise ValueError("Treasury buyback calendar 根节点非法")
    calendar_name = _required_text(root, "BuybackCalendarName")
    calendar_cycle = _VERSION_SUFFIX.sub("", calendar_name).strip()
    covered_start = _xml_date(root, "StartDate")
    covered_end = _xml_date(root, "EndDate")
    raw = build_raw_source_payload(
        source_id=TREASURY_BUYBACK_SOURCE_ID,
        source_url=TREASURY_BUYBACK_URL,
        media_type="application/xml",
        observed_at=observed_at,
        content=content,
    )
    records: list[TreasuryBuybackOperationRecord] = []
    for node in root.findall("BuybackCalendarDate"):
        operation_date = _xml_date(node, "OperationDate")
        operation_start_at = _eastern_at(
            operation_date,
            _xml_time(node, "OperationStartTimeEasternUS"),
        )
        operation_end_at = _eastern_at(
            operation_date,
            _xml_time(node, "OperationEndTimeEasternUS"),
        )
        source_record_id = stable_id(
            "treasury_buyback_operation",
            calendar_cycle.casefold(),
            operation_start_at.isoformat(),
        )
        values = {
            "source_record_id": source_record_id,
            "status": CalendarEventStatus.SCHEDULED.value,
            "calendar_cycle": calendar_cycle,
            "operation_start_at": operation_start_at.isoformat(),
            "operation_end_at": operation_end_at.isoformat(),
            "settlement_date": _xml_date(node, "SettlementDate").isoformat(),
            "operation_type": _required_text(node, "OperationType"),
            "security_type": _required_text(node, "SecurityType"),
            "maturity_bucket": _required_text(node, "PurchaseBucketName"),
            "maturity_start": _xml_date(node, "MaturityDateRangeStart").isoformat(),
            "maturity_end": _xml_date(node, "MaturityDateRangeEnd").isoformat(),
            "minimum_purchase_usd_m": _usd_m(node, "MinimumPurchaseAmountDollars"),
            "maximum_purchase_usd_m": _usd_m(node, "MaximumPurchaseAmountDollars"),
        }
        payload_hash = content_hash(values)
        observation = SourceObservation(
            observation_id=stable_id(
                "source_observation",
                TREASURY_BUYBACK_SOURCE_ID,
                source_record_id,
                payload_hash,
                observed_at.isoformat(),
            ),
            source_id=TREASURY_BUYBACK_SOURCE_ID,
            source_tier=SourceTier.FIRST_PARTY,
            source_record_id=source_record_id,
            observed_at=observed_at,
            payload_hash=payload_hash,
            payload_ref=raw.payload_id,
        )
        records.append(
            TreasuryBuybackOperationRecord(
                observation=observation,
                status=CalendarEventStatus.SCHEDULED,
                calendar_cycle=calendar_cycle,
                operation_start_at=operation_start_at,
                operation_end_at=operation_end_at,
                settlement_date=date.fromisoformat(values["settlement_date"]),
                operation_type=values["operation_type"],
                security_type=values["security_type"],
                maturity_bucket=values["maturity_bucket"],
                maturity_start=date.fromisoformat(values["maturity_start"]),
                maturity_end=date.fromisoformat(values["maturity_end"]),
                minimum_purchase_usd_m=values["minimum_purchase_usd_m"],
                maximum_purchase_usd_m=values["maximum_purchase_usd_m"],
            )
        )
    if not records:
        raise ValueError("Treasury buyback calendar 不含操作")
    return TreasuryBuybackCalendarSnapshot(
        calendar_cycle=calendar_cycle,
        covered_start=covered_start,
        covered_end=covered_end,
        records=tuple(records),
    )


def build_treasury_buyback_calendar_revision(
    record: TreasuryBuybackOperationRecord,
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
            raise ValueError("前序 Treasury buyback 修订不属于同一事件")
        if previous.observed_at >= observation.observed_at:
            raise ValueError("Treasury buyback 修订观察时间必须严格递增")
    candidate = MarketCalendarEventRevision.model_construct(
        event_id=event_id,
        revision_id="pending",
        previous_revision_id=previous.revision_id if previous else None,
        event_type=OfficialRecordKind.TREASURY_BUYBACK_OPERATION,
        status=record.status,
        source_id=observation.source_id,
        source_record_id=observation.source_record_id,
        source_observation_id=observation.observation_id,
        event_start_at=record.operation_start_at,
        event_end_at=record.operation_end_at,
        scheduled_release_at=record.operation_start_at,
        observed_at=observation.observed_at,
        risk_factors=("US_FISCAL_LIQUIDITY",),
        has_projection_materials=False,
        content_hash="pending",
    )
    semantic_hash = content_hash(calendar_semantic_payload(candidate))
    if previous is not None and previous.content_hash == semantic_hash:
        # Amount/bucket details belong to the canonical fact, while this
        # calendar projection only owns timing/status. Reuse the timing
        # revision when the source updates economic details without moving it.
        return previous
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


def build_treasury_buyback_cancellation(
    record: TreasuryBuybackOperationRecord,
    *,
    observed_at: datetime,
    payload_ref: str,
) -> TreasuryBuybackOperationRecord:
    observed_at = require_utc(observed_at)
    if record.status != CalendarEventStatus.SCHEDULED:
        raise ValueError("只有已安排的 Treasury buyback operation 可以取消")
    if observed_at <= record.observation.observed_at:
        raise ValueError("Treasury buyback 取消观察时间必须严格递增")
    values = {
        **treasury_buyback_semantic_payload(record),
        "status": CalendarEventStatus.CANCELLED.value,
    }
    payload_hash = content_hash(values)
    observation = SourceObservation(
        observation_id=stable_id(
            "source_observation",
            TREASURY_BUYBACK_SOURCE_ID,
            record.observation.source_record_id,
            payload_hash,
            observed_at.isoformat(),
        ),
        source_id=TREASURY_BUYBACK_SOURCE_ID,
        source_tier=SourceTier.FIRST_PARTY,
        source_record_id=record.observation.source_record_id,
        observed_at=observed_at,
        payload_hash=payload_hash,
        payload_ref=payload_ref,
    )
    return TreasuryBuybackOperationRecord(
        **record.model_dump(exclude={"observation", "status"}),
        observation=observation,
        status=CalendarEventStatus.CANCELLED,
    )


def treasury_buyback_semantic_payload(record: TreasuryBuybackOperationRecord) -> dict:
    return {
        "source_record_id": record.observation.source_record_id,
        "status": record.status.value,
        "calendar_cycle": record.calendar_cycle,
        "operation_start_at": record.operation_start_at.isoformat(),
        "operation_end_at": record.operation_end_at.isoformat(),
        "settlement_date": record.settlement_date.isoformat(),
        "operation_type": record.operation_type,
        "security_type": record.security_type,
        "maturity_bucket": record.maturity_bucket,
        "maturity_start": record.maturity_start.isoformat(),
        "maturity_end": record.maturity_end.isoformat(),
        "minimum_purchase_usd_m": record.minimum_purchase_usd_m,
        "maximum_purchase_usd_m": record.maximum_purchase_usd_m,
    }


def _required_text(node: ElementTree.Element, name: str) -> str:
    value = " ".join((node.findtext(name) or "").split())
    if not value:
        raise ValueError(f"Treasury buyback calendar 缺少 {name}")
    return value


def _xml_date(node: ElementTree.Element, name: str) -> date:
    try:
        return date.fromisoformat(_required_text(node, name))
    except ValueError as exc:
        raise ValueError(f"Treasury buyback {name} 日期非法") from exc


def _xml_time(node: ElementTree.Element, name: str) -> time:
    try:
        return time.fromisoformat(_required_text(node, name))
    except ValueError as exc:
        raise ValueError(f"Treasury buyback {name} 时间非法") from exc


def _eastern_at(day: date, local_time: time) -> datetime:
    return datetime.combine(day, local_time, tzinfo=_EASTERN).astimezone(UTC)


def _usd_m(node: ElementTree.Element, name: str) -> Decimal:
    try:
        return Decimal(_required_text(node, name)) / Decimal("1000000")
    except InvalidOperation as exc:
        raise ValueError(f"Treasury buyback {name} 金额非法") from exc
