from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import insert, select
from sqlalchemy.engine import Connection, Engine

from quant_core.domain import _require_utc
from quant_core.ids import stable_id
from quant_core.official_information import (
    FedMonetaryReleaseRecord,
    FomcMeetingRecord,
    MarketCalendarEventRevision,
    OfficialRecord,
    OfficialRecordKind,
    build_fomc_calendar_revision,
)
from quant_core.persistence import (
    market_calendar_event_revisions,
    source_observations,
)
from quant_core.sql_locking import advisory_xact_lock


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
                    return OfficialRecordWrite(record=latest, inserted=False)
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
                if isinstance(record, FomcMeetingRecord)
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

    def records_as_of(
        self,
        *,
        as_of: datetime,
        source_id: str | None = None,
    ) -> tuple[OfficialRecord, ...]:
        as_of = _require_utc(as_of)
        conditions = [source_observations.c.observed_at <= as_of]
        if source_id is not None:
            conditions.append(source_observations.c.source_id == source_id)
        with self._engine.connect() as connection:
            payloads = connection.execute(
                select(source_observations.c.payload)
                .where(*conditions)
                .order_by(
                    source_observations.c.source_id,
                    source_observations.c.source_record_id,
                    source_observations.c.observed_at.desc(),
                )
            ).scalars()
            latest: dict[tuple[str, str], OfficialRecord] = {}
            for payload in payloads:
                record = _record_from_payload(payload)
                observation = record.observation
                latest.setdefault(
                    (observation.source_id, observation.source_record_id),
                    record,
                )
        return tuple(
            sorted(
                latest.values(),
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
        as_of = _require_utc(as_of)
        conditions = [market_calendar_event_revisions.c.observed_at <= as_of]
        if source_id is not None:
            conditions.append(market_calendar_event_revisions.c.source_id == source_id)
        with self._engine.connect() as connection:
            payloads = connection.execute(
                select(market_calendar_event_revisions.c.payload)
                .where(*conditions)
                .order_by(
                    market_calendar_event_revisions.c.event_id,
                    market_calendar_event_revisions.c.observed_at.desc(),
                )
            ).scalars()
            latest_by_event: dict[str, MarketCalendarEventRevision] = {}
            for payload in payloads:
                revision = MarketCalendarEventRevision.model_validate(payload)
                latest_by_event.setdefault(revision.event_id, revision)
        return tuple(
            sorted(
                latest_by_event.values(),
                key=lambda item: (item.scheduled_release_at, item.event_id),
            )
        )

    @staticmethod
    def _append_calendar_revision(
        connection: Connection,
        record: FomcMeetingRecord,
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
        revision = build_fomc_calendar_revision(record, previous=previous)
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


def _record_from_payload(payload: dict) -> OfficialRecord:
    kind = payload.get("kind")
    if kind == OfficialRecordKind.FOMC_MEETING.value:
        return FomcMeetingRecord.model_validate(payload)
    if kind == OfficialRecordKind.FED_MONETARY_RELEASE.value:
        return FedMonetaryReleaseRecord.model_validate(payload)
    raise ValueError("未知官方记录类型")


def _calendar_event_id(record: FomcMeetingRecord) -> str:
    observation = record.observation
    return stable_id(
        "market_calendar_event",
        observation.source_id,
        observation.source_record_id,
    )
