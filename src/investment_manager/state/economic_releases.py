from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy.engine import Engine

from investment_manager.information.coverage import build_source_poll_record
from investment_manager.information.models import (
    CausalDomain,
    SourcePollRecord,
    SourcePollStatus,
)
from investment_manager.information.official.economic_actual_source import (
    EconomicReleaseActualDocument,
    parse_economic_release_actual,
)
from investment_manager.information.official.economic_actuals import (
    EconomicReleaseActualRecord,
)
from investment_manager.information.official.economic_calendar import (
    CalendarEventStatus,
    EconomicReleaseEventRecord,
)
from investment_manager.information.official.repository import (
    SqlStructuredInformationStore,
    StructuredRecordWrite,
)
from investment_manager.information.raw_payload import build_raw_source_payload
from investment_manager.information.raw_repository import SqlRawSourcePayloadStore
from investment_manager.kernel.time import require_utc
from investment_manager.state.facts import (
    OfficialFactProjectionPolicy,
    economic_release_actual_fact_id,
    project_economic_release_actual_fact,
    project_economic_release_unavailable_fact,
)
from investment_manager.state.models import CanonicalFactRevision, FactRevisionStatus
from investment_manager.state.official_ingestion import SourcePollAuditError
from investment_manager.state.repository import SqlFactStateStore

logger = logging.getLogger(__name__)


class EconomicReleaseActualSource(Protocol):
    def fetch(
        self,
        event: EconomicReleaseEventRecord,
        *,
        observed_at: datetime,
    ) -> EconomicReleaseActualDocument | None: ...


class SourcePollRecorder(Protocol):
    def put(self, poll: SourcePollRecord) -> bool: ...


@dataclass(frozen=True, slots=True)
class EconomicReleaseActualIngestionResult:
    record: StructuredRecordWrite | None
    fact: CanonicalFactRevision | None


class SqlEconomicReleaseActualFactIngestor:
    """Persist one due release actual or its explicit unavailable terminal."""

    def __init__(
        self,
        engine: Engine,
        *,
        policy: OfficialFactProjectionPolicy,
    ) -> None:
        self._raw = SqlRawSourcePayloadStore(engine)
        self._records = SqlStructuredInformationStore(engine)
        self._facts = SqlFactStateStore(engine)
        self._policy = policy

    def latest_resolution(
        self,
        event: EconomicReleaseEventRecord,
    ) -> CanonicalFactRevision | None:
        return self._facts.latest_fact(economic_release_actual_fact_id(event))

    def ingest(
        self,
        event: EconomicReleaseEventRecord,
        document: EconomicReleaseActualDocument,
        *,
        observed_at: datetime,
    ) -> EconomicReleaseActualIngestionResult:
        observed_at = require_utc(observed_at)
        record = parse_economic_release_actual(
            event,
            document,
            observed_at=observed_at,
        )
        if record is None:
            return EconomicReleaseActualIngestionResult(record=None, fact=None)
        raw = build_raw_source_payload(
            source_id=record.observation.source_id,
            source_url=document.source_url,
            media_type=document.media_type,
            observed_at=observed_at,
            content=document.content,
        )
        if raw.payload_id != record.observation.payload_ref:
            raise ValueError("经济发布实际值原始 payload identity 不一致")
        self._raw.put(raw, document.content)
        write = self._records.put(record)
        if not isinstance(write.record, EconomicReleaseActualRecord):
            raise ValueError("经济发布实际值存储返回了错误记录类型")
        previous = self._facts.latest_fact(economic_release_actual_fact_id(event))
        candidate = project_economic_release_actual_fact(
            write.record,
            policy=self._policy,
            previous=previous,
        )
        if previous is not None and previous.revision_hash == candidate.revision_hash:
            return EconomicReleaseActualIngestionResult(record=write, fact=None)
        if (
            not write.inserted
            and previous is not None
            and previous.status == FactRevisionStatus.ACTIVE
        ):
            return EconomicReleaseActualIngestionResult(record=write, fact=None)
        return EconomicReleaseActualIngestionResult(
            record=write,
            fact=self._facts.put_fact(candidate),
        )

    def mark_unavailable(
        self,
        event: EconomicReleaseEventRecord,
        *,
        observed_at: datetime,
    ) -> CanonicalFactRevision | None:
        observed_at = require_utc(observed_at)
        previous = self._facts.latest_fact(economic_release_actual_fact_id(event))
        if previous is not None and previous.status in {
            FactRevisionStatus.ACTIVE,
            FactRevisionStatus.UNAVAILABLE,
        }:
            return None
        fact = project_economic_release_unavailable_fact(
            event,
            observed_at=observed_at,
            policy=self._policy,
            previous=previous,
        )
        return self._facts.put_fact(fact)


@dataclass(slots=True)
class EconomicReleaseActualCollectorHealth:
    poll_count: int = 0
    available_count: int = 0
    unavailable_count: int = 0
    last_success_at: datetime | None = None
    last_error_by_event: dict[str, str] = field(default_factory=dict)


class EconomicReleaseActualCollectorService:
    """Resolve calendar-backed actual-value obligations before waking analysis."""

    def __init__(
        self,
        *,
        source: EconomicReleaseActualSource,
        ingestor: SqlEconomicReleaseActualFactIngestor,
        records: SqlStructuredInformationStore,
        publish_recent: Callable[[datetime], None],
        poll_seconds: int,
        deadline_seconds: int,
        recovery_lookback_seconds: int,
        poll_recorder: SourcePollRecorder | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if (
            poll_seconds < 1
            or deadline_seconds < poll_seconds
            or recovery_lookback_seconds < deadline_seconds
        ):
            raise ValueError("经济发布实际值轮询/截止/恢复窗口非法")
        self._source = source
        self._ingestor = ingestor
        self._records = records
        self._publish_recent = publish_recent
        self._poll_seconds = poll_seconds
        self._deadline_seconds = deadline_seconds
        self._recovery_lookback_seconds = recovery_lookback_seconds
        self._poll_recorder = poll_recorder
        self._clock = clock
        self.health = EconomicReleaseActualCollectorHealth()

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self._poll_due()
            try:
                await asyncio.to_thread(self._publish_recent, require_utc(self._clock()))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("economic release actual trigger publication failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._poll_seconds)

    async def _poll_due(self) -> None:
        now = require_utc(self._clock())
        earliest = now - timedelta(seconds=self._recovery_lookback_seconds)
        events: list[EconomicReleaseEventRecord] = []
        for item in self._records.records_as_of(as_of=now):
            if (
                not isinstance(item, EconomicReleaseEventRecord)
                or item.status != CalendarEventStatus.SCHEDULED
                or not earliest <= item.scheduled_at <= now
            ):
                continue
            resolution = self._ingestor.latest_resolution(item)
            if resolution is not None and resolution.status == FactRevisionStatus.ACTIVE:
                continue
            if (
                resolution is not None
                and resolution.status == FactRevisionStatus.UNAVAILABLE
                and resolution.observed_at + timedelta(seconds=self._deadline_seconds) > now
            ):
                # A bounded late retry remains possible without hammering the
                # source or creating a second mutable obligation ledger.
                continue
            events.append(item)
        for event in events:
            await self._poll_event(event)

    async def _poll_event(self, event: EconomicReleaseEventRecord) -> None:
        started_at = require_utc(self._clock())
        self.health.poll_count += 1
        stream_id = f"economic-release-actual-{event.release_kind.value.lower()}"
        result = EconomicReleaseActualIngestionResult(record=None, fact=None)
        document: EconomicReleaseActualDocument | None = None
        error_class = None
        try:
            document = await asyncio.to_thread(
                self._source.fetch,
                event,
                observed_at=started_at,
            )
            if document is not None:
                result = await asyncio.to_thread(
                    self._ingestor.ingest,
                    event,
                    document,
                    observed_at=require_utc(self._clock()),
                )
            completed_at = max(require_utc(self._clock()), started_at)
            deadline = event.scheduled_at + timedelta(seconds=self._deadline_seconds)
            if result.fact is None and completed_at >= deadline:
                unavailable = await asyncio.to_thread(
                    self._ingestor.mark_unavailable,
                    event,
                    observed_at=completed_at,
                )
                if unavailable is not None:
                    result = EconomicReleaseActualIngestionResult(
                        record=result.record,
                        fact=unavailable,
                    )
                    self.health.unavailable_count += 1
            if result.fact is not None and result.fact.status == FactRevisionStatus.ACTIVE:
                self.health.available_count += 1
            self.health.last_success_at = completed_at
            self.health.last_error_by_event.pop(event.observation.source_record_id, None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            completed_at = max(require_utc(self._clock()), started_at)
            error_class = type(exc).__name__
            if isinstance(exc, SourcePollAuditError):
                raise
            deadline = event.scheduled_at + timedelta(seconds=self._deadline_seconds)
            if document is None and completed_at >= deadline:
                try:
                    unavailable = await asyncio.to_thread(
                        self._ingestor.mark_unavailable,
                        event,
                        observed_at=completed_at,
                    )
                except Exception:
                    logger.exception(
                        "economic release unavailable terminal could not be stored: %s",
                        event.title,
                    )
                else:
                    if unavailable is not None:
                        self.health.unavailable_count += 1
            if (
                self.health.last_error_by_event.get(event.observation.source_record_id)
                != error_class
            ):
                logger.exception("economic release actual collector failed: %s", event.title)
            self.health.last_error_by_event[event.observation.source_record_id] = error_class
        self._record_poll(
            stream_id=stream_id,
            started_at=started_at,
            completed_at=completed_at,
            result=result,
            error_class=error_class,
        )

    def _record_poll(
        self,
        *,
        stream_id: str,
        started_at: datetime,
        completed_at: datetime,
        result: EconomicReleaseActualIngestionResult,
        error_class: str | None,
    ) -> None:
        if self._poll_recorder is None:
            return
        record = None if result.record is None else result.record.record
        poll = build_source_poll_record(
            source_stream_id=stream_id,
            domain=CausalDomain.MONETARY_INFLATION,
            status=(
                SourcePollStatus.FAILED
                if error_class is not None
                else SourcePollStatus.CHANGED
                if result.fact is not None
                else SourcePollStatus.UNCHANGED
            ),
            started_at=started_at,
            completed_at=completed_at,
            poll_interval_seconds=self._poll_seconds,
            latest_publication_at=(
                None if record is None else record.observation.source_published_at
            ),
            observation_count=0 if record is None else 1,
            new_fact_count=0 if result.fact is None else 1,
            error_class=error_class,
        )
        try:
            self._poll_recorder.put(poll)
        except Exception as exc:
            raise SourcePollAuditError("经济发布实际值轮询事实无法持久化") from exc


__all__ = [
    "EconomicReleaseActualCollectorService",
    "SqlEconomicReleaseActualFactIngestor",
]
