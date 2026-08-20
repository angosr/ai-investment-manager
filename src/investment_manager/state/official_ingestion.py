from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy.engine import Engine

from investment_manager.information.official.public_calendar import (
    FedChairPublicEventRecord,
)
from investment_manager.information.official.records import (
    FedMonetaryReleaseRecord,
    FomcMeetingRecord,
)
from investment_manager.information.official.repository import (
    OfficialRecordWrite,
    SqlFedOfficialInformationIngestor,
)
from investment_manager.kernel.time import require_utc
from investment_manager.state.facts import (
    OfficialFactProjectionPolicy,
    project_fed_chair_public_event_fact,
    project_fed_monetary_release_fact,
    project_fomc_calendar_fact,
)
from investment_manager.state.models import CanonicalFactRevision
from investment_manager.state.repository import SqlFactStateStore

logger = logging.getLogger(__name__)


class FedOfficialSource(Protocol):
    def fetch_calendar(self) -> str | None: ...

    def fetch_public_calendar(self) -> str | None: ...

    def fetch_monetary_rss(self) -> str | None: ...


@dataclass(frozen=True, slots=True)
class OfficialFactIngestionResult:
    records: tuple[OfficialRecordWrite, ...]
    new_fact_revisions: tuple[CanonicalFactRevision, ...]


class SqlFedFactIngestor:
    """The sole Fed raw-record-to-canonical-fact projection boundary."""

    def __init__(
        self,
        engine: Engine,
        policy: OfficialFactProjectionPolicy,
    ) -> None:
        self._official = SqlFedOfficialInformationIngestor(engine)
        self._facts = SqlFactStateStore(engine)
        self._policy = policy

    def ingest_calendar(
        self,
        html: str,
        *,
        observed_at: datetime,
        years: tuple[int, ...] | None = None,
    ) -> OfficialFactIngestionResult:
        writes = self._official.ingest_calendar(
            html,
            observed_at=observed_at,
            years=years,
        )
        return OfficialFactIngestionResult(
            records=writes,
            new_fact_revisions=self._project(writes),
        )

    def ingest_monetary_rss(
        self,
        xml: str,
        *,
        observed_at: datetime,
    ) -> OfficialFactIngestionResult:
        writes = self._official.ingest_monetary_rss(xml, observed_at=observed_at)
        return OfficialFactIngestionResult(
            records=writes,
            new_fact_revisions=self._project(writes),
        )

    def ingest_public_calendar(
        self,
        payload: str,
        *,
        observed_at: datetime,
        years: tuple[int, ...] | None = None,
    ) -> OfficialFactIngestionResult:
        writes = self._official.ingest_public_calendar(
            payload,
            observed_at=observed_at,
            years=years,
        )
        return OfficialFactIngestionResult(
            records=writes,
            new_fact_revisions=self._project(writes),
        )

    def _project(
        self,
        writes: tuple[OfficialRecordWrite, ...],
    ) -> tuple[CanonicalFactRevision, ...]:
        projected: list[CanonicalFactRevision] = []
        for write in writes:
            candidate = self._candidate(write, previous=None)
            previous = self._facts.latest_fact(candidate.fact_id)
            # An unchanged source replay can repair a missing first projection,
            # but must not rewrite an existing fact merely because the deployed
            # projector version changed. Normal revisions require a newly stored
            # source observation with a later visibility time.
            if previous is not None and not write.inserted:
                continue
            if previous is not None and previous.revision_hash == candidate.revision_hash:
                continue
            fact = (
                candidate
                if previous is None
                else self._candidate(write, previous=previous)
            )
            stored = self._facts.put_fact(fact)
            if previous is None or stored.revision_id != previous.revision_id:
                projected.append(stored)
        return tuple(projected)

    def _candidate(
        self,
        write: OfficialRecordWrite,
        *,
        previous: CanonicalFactRevision | None,
    ) -> CanonicalFactRevision:
        record = write.record
        if isinstance(record, FomcMeetingRecord):
            if write.calendar_revision is None:
                raise ValueError("FOMC 官方记录缺少 Calendar revision")
            return project_fomc_calendar_fact(
                write.calendar_revision,
                policy=self._policy,
                previous=previous,
            )
        if isinstance(record, FedChairPublicEventRecord):
            if write.calendar_revision is None:
                raise ValueError("Fed Chair 官方记录缺少 Calendar revision")
            return project_fed_chair_public_event_fact(
                record,
                write.calendar_revision,
                policy=self._policy,
                previous=previous,
            )
        if isinstance(record, FedMonetaryReleaseRecord):
            return project_fed_monetary_release_fact(
                record,
                policy=self._policy,
                previous=previous,
            )
        raise TypeError(f"不支持的 Fed 官方记录类型: {type(record).__name__}")


@dataclass(slots=True)
class FedOfficialCollectorHealth:
    calendar_poll_count: int = 0
    public_calendar_poll_count: int = 0
    monetary_poll_count: int = 0
    new_fact_revision_count: int = 0
    publication_count: int = 0
    last_calendar_success_at: datetime | None = None
    last_public_calendar_success_at: datetime | None = None
    last_monetary_success_at: datetime | None = None
    calendar_error_class: str | None = None
    public_calendar_error_class: str | None = None
    monetary_error_class: str | None = None
    publication_error_class: str | None = None


class FedOfficialCollectorService:
    """Poll pinned first-party feeds and project them into canonical facts."""

    def __init__(
        self,
        *,
        source: FedOfficialSource,
        ingestor: SqlFedFactIngestor,
        publish_recent: Callable[[datetime], None],
        monetary_poll_seconds: int,
        calendar_poll_seconds: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if monetary_poll_seconds < 1 or calendar_poll_seconds < 1:
            raise ValueError("Fed official polling interval 必须为正数")
        self._source = source
        self._ingestor = ingestor
        self._publish_recent = publish_recent
        self._monetary_poll_seconds = monetary_poll_seconds
        self._calendar_poll_seconds = calendar_poll_seconds
        self._clock = clock
        self.health = FedOfficialCollectorHealth()

    async def run(self, stop: asyncio.Event) -> None:
        next_calendar_at: datetime | None = None
        next_monetary_at: datetime | None = None
        while not stop.is_set():
            now = require_utc(self._clock())
            if next_calendar_at is None or now >= next_calendar_at:
                next_calendar_at = now + timedelta(seconds=self._calendar_poll_seconds)
                await self._poll("calendar")
                await self._poll("public_calendar")
            if next_monetary_at is None or now >= next_monetary_at:
                next_monetary_at = now + timedelta(seconds=self._monetary_poll_seconds)
                await self._poll("monetary")
            try:
                await asyncio.to_thread(
                    self._publish_recent,
                    require_utc(self._clock()),
                )
                self.health.publication_count += 1
                self.health.publication_error_class = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_error(exc, "publication")
            now = require_utc(self._clock())
            delay = max(
                0.1,
                min(
                    (next_calendar_at - now).total_seconds(),
                    (next_monetary_at - now).total_seconds(),
                ),
            )
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)

    async def _poll(self, kind: str) -> None:
        counter_field = {
            "calendar": "calendar_poll_count",
            "public_calendar": "public_calendar_poll_count",
            "monetary": "monetary_poll_count",
        }.get(kind)
        if counter_field is None:
            raise ValueError(f"未知 Fed official collector kind: {kind}")
        setattr(self.health, counter_field, getattr(self.health, counter_field) + 1)
        try:
            result = await asyncio.to_thread(self._collect, kind)
            setattr(
                self.health,
                f"last_{kind}_success_at",
                require_utc(self._clock()),
            )
            setattr(self.health, f"{kind}_error_class", None)
            self.health.new_fact_revision_count += len(result.new_fact_revisions)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_error(exc, kind)

    def _collect(self, kind: str) -> OfficialFactIngestionResult:
        if kind == "calendar":
            content = self._source.fetch_calendar()
            if content is None:
                return OfficialFactIngestionResult(records=(), new_fact_revisions=())
            observed_at = require_utc(self._clock())
            result = self._ingestor.ingest_calendar(
                content,
                observed_at=observed_at,
                years=(observed_at.year, observed_at.year + 1),
            )
        elif kind == "public_calendar":
            content = self._source.fetch_public_calendar()
            if content is None:
                return OfficialFactIngestionResult(records=(), new_fact_revisions=())
            observed_at = require_utc(self._clock())
            result = self._ingestor.ingest_public_calendar(
                content,
                observed_at=observed_at,
                years=(observed_at.year, observed_at.year + 1),
            )
        elif kind == "monetary":
            content = self._source.fetch_monetary_rss()
            if content is None:
                return OfficialFactIngestionResult(records=(), new_fact_revisions=())
            result = self._ingestor.ingest_monetary_rss(
                content,
                observed_at=require_utc(self._clock()),
            )
        else:
            raise ValueError(f"未知 Fed official collector kind: {kind}")
        if not result.records:
            raise ValueError(f"Fed official {kind} 响应没有可解析记录")
        return result

    def _record_error(self, exc: Exception, component: str) -> None:
        field = f"{component}_error_class"
        previous = getattr(self.health, field)
        if previous != type(exc).__name__:
            logger.exception("Fed official collector %s failed", component)
        setattr(self.health, field, type(exc).__name__)
