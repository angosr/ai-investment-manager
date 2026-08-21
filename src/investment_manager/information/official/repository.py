from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, insert, select
from sqlalchemy.engine import Connection, Engine

from investment_manager.information.official.metrics import OfficialMetricSnapshot
from investment_manager.information.official.public_calendar import (
    FED_PUBLIC_CALENDAR_URL,
    FedChairPublicEventRecord,
    build_fed_chair_calendar_revision,
    build_fed_chair_cancellation,
    parse_fed_chair_calendar,
)
from investment_manager.information.official.records import (
    FED_FOMC_CALENDAR_URL,
    FED_MONETARY_RSS_URL,
    FED_SOURCE_ID,
    CalendarEventStatus,
    FedMonetaryReleaseRecord,
    FomcMeetingRecord,
    MarketCalendarEventRevision,
    OfficialRecordKind,
    build_fomc_calendar_revision,
    parse_fed_monetary_rss,
    parse_fomc_calendar,
)
from investment_manager.information.official.records import (
    OfficialRecord as BaseOfficialRecord,
)
from investment_manager.information.raw_payload import build_raw_source_payload
from investment_manager.information.raw_repository import SqlRawSourcePayloadStore
from investment_manager.information.tables import (
    market_calendar_event_revisions,
    raw_source_payloads,
    source_observations,
)
from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.platform.locking import advisory_xact_lock

OfficialRecord = BaseOfficialRecord | FedChairPublicEventRecord | OfficialMetricSnapshot
CalendarOfficialRecord = FomcMeetingRecord | FedChairPublicEventRecord


@dataclass(frozen=True, slots=True)
class OfficialRecordWrite:
    record: OfficialRecord
    inserted: bool
    calendar_revision: MarketCalendarEventRevision | None = None


class SqlOfficialInformationStore:
    """Single append-only boundary for first-seen official records and revisions."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def put(self, record: OfficialRecord) -> OfficialRecordWrite:
        observation = record.observation
        with self._engine.begin() as connection:
            raw_payload_exists = connection.execute(
                select(raw_source_payloads.c.payload_id).where(
                    raw_source_payloads.c.payload_id == observation.payload_ref,
                    raw_source_payloads.c.source_id == observation.source_id,
                    raw_source_payloads.c.observed_at <= observation.observed_at,
                )
            ).scalar_one_or_none()
            if raw_payload_exists is None:
                raise ValueError("官方记录缺少截至 observed_at 可见的原始来源 payload")
            advisory_xact_lock(
                connection,
                "source_observation",
                observation.source_id,
                observation.source_record_id,
            )
            latest_payload = connection.execute(
                select(source_observations.c.payload)
                .where(
                    source_observations.c.source_id == observation.source_id,
                    source_observations.c.source_record_id == observation.source_record_id,
                )
                .order_by(source_observations.c.observed_at.desc())
                .limit(1)
                .with_for_update()
            ).scalar_one_or_none()
            if latest_payload is not None:
                latest = _record_from_payload(latest_payload)
                if latest.observation.payload_hash == observation.payload_hash:
                    revision = (
                        self._calendar_revision_for_observation(connection, latest)
                        if isinstance(latest, (FomcMeetingRecord, FedChairPublicEventRecord))
                        else None
                    )
                    return OfficialRecordWrite(
                        record=latest,
                        inserted=False,
                        calendar_revision=revision,
                    )
                if latest.observation.observed_at >= observation.observed_at:
                    raise ValueError("官方记录修订 observed_at 必须严格递增")

            connection.execute(
                insert(source_observations).values(
                    observation_id=observation.observation_id,
                    source_id=observation.source_id,
                    source_record_id=observation.source_record_id,
                    record_kind=record.kind.value,
                    source_tier=observation.source_tier.value,
                    source_published_at=observation.source_published_at,
                    observed_at=observation.observed_at,
                    payload_hash=observation.payload_hash,
                    payload=record.model_dump(mode="json"),
                )
            )
            revision = (
                self._append_calendar_revision(connection, record)
                if isinstance(record, (FomcMeetingRecord, FedChairPublicEventRecord))
                else None
            )
        return OfficialRecordWrite(
            record=record,
            inserted=True,
            calendar_revision=revision,
        )

    def observation(self, observation_id: str) -> OfficialRecord:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(source_observations.c.payload).where(
                    source_observations.c.observation_id == observation_id
                )
            ).scalar_one_or_none()
        if payload is None:
            raise KeyError(observation_id)
        return _record_from_payload(payload)

    def metric_history(
        self,
        *,
        source_id: str,
        source_record_id: str,
        as_of: datetime,
        limit: int = 400,
    ) -> tuple[OfficialMetricSnapshot, ...]:
        """Return bounded point-in-time revisions for latest-only metric feeds."""

        as_of = require_utc(as_of)
        if not source_id or not source_record_id or not 1 <= limit <= 5_000:
            raise ValueError("官方指标历史查询参数非法")
        with self._engine.connect() as connection:
            payloads = tuple(
                connection.execute(
                    select(source_observations.c.payload)
                    .where(
                        source_observations.c.source_id == source_id,
                        source_observations.c.source_record_id == source_record_id,
                        source_observations.c.observed_at <= as_of,
                    )
                    .order_by(source_observations.c.observed_at.desc())
                    .limit(limit)
                ).scalars()
            )
        records = tuple(_record_from_payload(payload) for payload in reversed(payloads))
        if not all(isinstance(item, OfficialMetricSnapshot) for item in records):
            raise ValueError("官方指标历史混入其他记录类型")
        return records

    def records_as_of(
        self,
        *,
        as_of: datetime,
        source_id: str | None = None,
    ) -> tuple[OfficialRecord, ...]:
        as_of = require_utc(as_of)
        conditions = [source_observations.c.observed_at <= as_of]
        if source_id is not None:
            conditions.append(source_observations.c.source_id == source_id)
        ranked = (
            select(
                source_observations.c.payload.label("payload"),
                func.row_number()
                .over(
                    partition_by=(
                        source_observations.c.source_id,
                        source_observations.c.source_record_id,
                    ),
                    order_by=(
                        source_observations.c.observed_at.desc(),
                        source_observations.c.observation_id.desc(),
                    ),
                )
                .label("revision_rank"),
            )
            .where(*conditions)
            .subquery()
        )
        with self._engine.connect() as connection:
            payloads = connection.execute(
                select(ranked.c.payload).where(ranked.c.revision_rank == 1)
            ).scalars()
            records = tuple(_record_from_payload(payload) for payload in payloads)
        return tuple(
            sorted(
                records,
                key=lambda item: (
                    item.observation.observed_at,
                    item.observation.source_id,
                    item.observation.source_record_id,
                ),
            )
        )

    def calendar_as_of(
        self,
        *,
        as_of: datetime,
        source_id: str | None = None,
    ) -> tuple[MarketCalendarEventRevision, ...]:
        as_of = require_utc(as_of)
        conditions = [market_calendar_event_revisions.c.observed_at <= as_of]
        if source_id is not None:
            conditions.append(market_calendar_event_revisions.c.source_id == source_id)
        ranked = (
            select(
                market_calendar_event_revisions.c.payload.label("payload"),
                func.row_number()
                .over(
                    partition_by=market_calendar_event_revisions.c.event_id,
                    order_by=(
                        market_calendar_event_revisions.c.observed_at.desc(),
                        market_calendar_event_revisions.c.revision_id.desc(),
                    ),
                )
                .label("revision_rank"),
            )
            .where(*conditions)
            .subquery()
        )
        with self._engine.connect() as connection:
            payloads = connection.execute(
                select(ranked.c.payload).where(ranked.c.revision_rank == 1)
            ).scalars()
            revisions = tuple(
                MarketCalendarEventRevision.model_validate(payload)
                for payload in payloads
            )
        return tuple(
            sorted(
                revisions,
                key=lambda item: (item.scheduled_release_at, item.event_id),
            )
        )

    @staticmethod
    def _calendar_revision_for_observation(
        connection: Connection,
        record: CalendarOfficialRecord,
    ) -> MarketCalendarEventRevision:
        payload = connection.execute(
            select(market_calendar_event_revisions.c.payload).where(
                market_calendar_event_revisions.c.source_observation_id
                == record.observation.observation_id
            )
        ).scalar_one()
        return MarketCalendarEventRevision.model_validate(payload)

    @staticmethod
    def _append_calendar_revision(
        connection: Connection,
        record: CalendarOfficialRecord,
    ) -> MarketCalendarEventRevision:
        event_id = _calendar_event_id(record)
        previous_payload = connection.execute(
            select(market_calendar_event_revisions.c.payload)
            .where(market_calendar_event_revisions.c.event_id == event_id)
            .order_by(market_calendar_event_revisions.c.observed_at.desc())
            .limit(1)
            .with_for_update()
        ).scalar_one_or_none()
        previous = (
            MarketCalendarEventRevision.model_validate(previous_payload)
            if previous_payload is not None
            else None
        )
        revision = (
            build_fomc_calendar_revision(record, previous=previous)
            if isinstance(record, FomcMeetingRecord)
            else build_fed_chair_calendar_revision(record, previous=previous)
        )
        connection.execute(
            insert(market_calendar_event_revisions).values(
                revision_id=revision.revision_id,
                event_id=revision.event_id,
                previous_revision_id=revision.previous_revision_id,
                source_observation_id=revision.source_observation_id,
                source_id=revision.source_id,
                source_record_id=revision.source_record_id,
                status=revision.status.value,
                scheduled_release_at=revision.scheduled_release_at,
                observed_at=revision.observed_at,
                content_hash=revision.content_hash,
                payload=revision.model_dump(mode="json"),
            )
        )
        return revision


class SqlFedOfficialInformationIngestor:
    """Persist first-seen raw Fed evidence before projecting official records."""

    def __init__(self, engine: Engine) -> None:
        self._raw = SqlRawSourcePayloadStore(engine)
        self._records = SqlOfficialInformationStore(engine)

    def ingest_calendar(
        self,
        html: str,
        *,
        observed_at: datetime,
        years: tuple[int, ...] | None = None,
    ) -> tuple[OfficialRecordWrite, ...]:
        content = html.encode("utf-8")
        raw = build_raw_source_payload(
            source_id=FED_SOURCE_ID,
            source_url=FED_FOMC_CALENDAR_URL,
            media_type="text/html",
            observed_at=observed_at,
            content=content,
        )
        self._raw.put(raw, content)
        return tuple(
            self._records.put(record)
            for record in parse_fomc_calendar(
                html,
                observed_at=observed_at,
                years=years,
            )
        )

    def ingest_monetary_rss(
        self,
        xml: str,
        *,
        observed_at: datetime,
    ) -> tuple[OfficialRecordWrite, ...]:
        content = xml.encode("utf-8")
        raw = build_raw_source_payload(
            source_id=FED_SOURCE_ID,
            source_url=FED_MONETARY_RSS_URL,
            media_type="application/rss+xml",
            observed_at=observed_at,
            content=content,
        )
        self._raw.put(raw, content)
        return tuple(
            self._records.put(record)
            for record in parse_fed_monetary_rss(xml, observed_at=observed_at)
        )

    def ingest_public_calendar(
        self,
        payload: str,
        *,
        observed_at: datetime,
        years: tuple[int, ...] | None = None,
    ) -> tuple[OfficialRecordWrite, ...]:
        observed_at = require_utc(observed_at)
        content = payload.encode("utf-8")
        raw = build_raw_source_payload(
            source_id=FED_SOURCE_ID,
            source_url=FED_PUBLIC_CALENDAR_URL,
            media_type="application/json",
            observed_at=observed_at,
            content=content,
        )
        self._raw.put(raw, content)
        snapshot = parse_fed_chair_calendar(
            payload,
            observed_at=observed_at,
            years=years,
        )
        current = tuple(
            record for record in snapshot.records if record.scheduled_at >= observed_at
        )
        current_ids = {record.observation.source_record_id for record in current}
        previous = tuple(
            record
            for record in self._records.records_as_of(
                as_of=observed_at,
                source_id=FED_SOURCE_ID,
            )
            if (
                isinstance(record, FedChairPublicEventRecord)
                and record.status == CalendarEventStatus.SCHEDULED
                and record.scheduled_at > observed_at
                and record.observation.observed_at < observed_at
                and record.calendar_year in snapshot.covered_years
                and record.observation.source_record_id not in current_ids
            )
        )
        cancellations = tuple(
            build_fed_chair_cancellation(
                record,
                observed_at=observed_at,
                payload_ref=raw.payload_id,
            )
            for record in previous
        )
        return tuple(
            self._records.put(record) for record in (*current, *cancellations)
        )


def _record_from_payload(payload: dict) -> OfficialRecord:
    kind = payload.get("kind")
    if kind == OfficialRecordKind.FOMC_MEETING.value:
        return FomcMeetingRecord.model_validate(payload)
    if kind == OfficialRecordKind.FED_MONETARY_RELEASE.value:
        return FedMonetaryReleaseRecord.model_validate(payload)
    if kind == OfficialRecordKind.FED_CHAIR_PUBLIC_EVENT.value:
        return FedChairPublicEventRecord.model_validate(payload)
    if kind == OfficialRecordKind.OFFICIAL_METRIC_SNAPSHOT.value:
        return OfficialMetricSnapshot.model_validate(payload)
    raise ValueError("未知官方记录类型")


def _calendar_event_id(record: CalendarOfficialRecord) -> str:
    observation = record.observation
    return stable_id(
        "market_calendar_event",
        observation.source_id,
        observation.source_record_id,
    )
