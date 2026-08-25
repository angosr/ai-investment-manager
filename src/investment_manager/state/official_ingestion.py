from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy.engine import Engine

from investment_manager.information.coverage import build_source_poll_record
from investment_manager.information.models import CausalDomain, SourcePollRecord, SourcePollStatus
from investment_manager.information.official.public_calendar import (
    FedChairPublicEventRecord,
)
from investment_manager.information.official.records import (
    CalendarEventStatus,
    FedMonetaryReleaseRecord,
    FomcMeetingRecord,
    fed_policy_document_eligible,
)
from investment_manager.information.official.regulation import (
    FEDERAL_REGISTER_RULEMAKING_STREAM_ID,
    FederalRegisterRulemakingRecord,
)
from investment_manager.information.official.repository import (
    SqlFederalRegisterInformationIngestor,
    SqlFedOfficialInformationIngestor,
    SqlTreasuryBuybackInformationIngestor,
    StructuredRecordWrite,
)
from investment_manager.information.official.source import (
    FedPolicyDocument,
    OfficialRegulatoryDocument,
)
from investment_manager.information.official.treasury_buybacks import (
    TREASURY_BUYBACK_STREAM_ID,
    TreasuryBuybackOperationRecord,
    TreasuryBuybackResultRecord,
)
from investment_manager.kernel.time import require_utc
from investment_manager.state.facts import (
    OfficialFactProjectionPolicy,
    project_fed_chair_public_event_fact,
    project_fed_monetary_release_fact,
    project_federal_register_rulemaking_fact,
    project_fomc_calendar_fact,
    project_treasury_buyback_operation_fact,
    project_treasury_buyback_result_fact,
)
from investment_manager.state.models import CanonicalFactRevision
from investment_manager.state.repository import SqlFactStateStore

logger = logging.getLogger(__name__)


class FedOfficialSource(Protocol):
    def fetch_calendar(self) -> str | None: ...

    def fetch_public_calendar(self) -> str | None: ...

    def fetch_monetary_rss(self) -> str | None: ...

    def fetch_monetary_document(self, url: str) -> FedPolicyDocument | None: ...


class TreasuryBuybackSource(Protocol):
    def fetch_calendar(self) -> bytes | None: ...

    def fetch_result(
        self,
        scheduled: TreasuryBuybackOperationRecord,
    ) -> bytes | None: ...


class FederalRegisterSource(Protocol):
    def fetch(self, *, observed_at: datetime) -> OfficialRegulatoryDocument | None: ...


class SourcePollRecorder(Protocol):
    def put(self, poll: SourcePollRecord) -> bool: ...


class SourcePollAuditError(RuntimeError):
    """A successful source read is not auditable until its poll fact is durable."""


_FED_STREAMS = {
    "calendar": "fed-fomc-calendar",
    "public_calendar": "fed-chair-calendar",
    "monetary": "fed-monetary-releases",
}


@dataclass(frozen=True, slots=True)
class OfficialFactIngestionResult:
    records: tuple[StructuredRecordWrite, ...]
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
        # FOMC statements and minutes are decision evidence only after the linked
        # first-party document has been parsed.  Keep the RSS observation in the
        # append-only source ledger, but never let its title-only representation
        # replace an already enriched policy fact during collector restarts.
        projectable = tuple(
            write
            for write in writes
            if not (
                isinstance(write.record, FedMonetaryReleaseRecord)
                and fed_policy_document_eligible(write.record)
                and write.record.policy_state is None
            )
        )
        return OfficialFactIngestionResult(
            records=writes,
            new_fact_revisions=self._project(projectable),
        )

    def ingest_monetary_document(
        self,
        record: FedMonetaryReleaseRecord,
        html: str,
        *,
        document_url: str,
        observed_at: datetime,
    ) -> OfficialFactIngestionResult:
        write = self._official.ingest_monetary_document(
            record,
            html,
            document_url=document_url,
            observed_at=observed_at,
        )
        return OfficialFactIngestionResult(
            records=(write,),
            new_fact_revisions=self._project((write,)),
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
        writes: tuple[StructuredRecordWrite, ...],
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
        write: StructuredRecordWrite,
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


class SqlFederalRegisterFactIngestor:
    """Project relevant official rulemaking into the canonical fact ledger."""

    def __init__(self, engine: Engine, policy: OfficialFactProjectionPolicy) -> None:
        self._official = SqlFederalRegisterInformationIngestor(engine)
        self._facts = SqlFactStateStore(engine)
        self._policy = policy

    def ingest(
        self,
        document: OfficialRegulatoryDocument,
        *,
        observed_at: datetime,
    ) -> OfficialFactIngestionResult:
        if document.stream_id != FEDERAL_REGISTER_RULEMAKING_STREAM_ID:
            raise ValueError("Federal Register document stream identity 不一致")
        writes = self._official.ingest(
            document.content,
            source_url=document.source_url,
            observed_at=observed_at,
        )
        projected: list[CanonicalFactRevision] = []
        for write in writes:
            record = write.record
            if not isinstance(record, FederalRegisterRulemakingRecord):
                raise TypeError("Federal Register ingestor 收到非规则制定记录")
            candidate = project_federal_register_rulemaking_fact(
                record,
                policy=self._policy,
            )
            previous = self._facts.latest_fact(candidate.fact_id)
            if previous is not None and not write.inserted:
                continue
            if previous is not None and previous.revision_hash == candidate.revision_hash:
                continue
            fact = (
                candidate
                if previous is None
                else project_federal_register_rulemaking_fact(
                    record,
                    policy=self._policy,
                    previous=previous,
                )
            )
            stored = self._facts.put_fact(fact)
            if previous is None or stored.revision_id != previous.revision_id:
                projected.append(stored)
        return OfficialFactIngestionResult(
            records=writes,
            new_fact_revisions=tuple(projected),
        )


@dataclass(slots=True)
class RegulatoryOfficialCollectorHealth:
    poll_count: int = 0
    new_fact_revision_count: int = 0
    publication_count: int = 0
    last_success_at: datetime | None = None
    error_class: str | None = None
    publication_error_class: str | None = None


class RegulatoryOfficialCollectorService:
    """Poll one bounded official rulemaking stream and publish material revisions."""

    def __init__(
        self,
        *,
        source: FederalRegisterSource,
        ingestor: SqlFederalRegisterFactIngestor,
        publish_recent: Callable[[datetime], None],
        poll_seconds: int,
        poll_recorder: SourcePollRecorder | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if poll_seconds < 1:
            raise ValueError("官方监管轮询周期必须为正数")
        self._source = source
        self._ingestor = ingestor
        self._publish_recent = publish_recent
        self._poll_seconds = poll_seconds
        self._poll_recorder = poll_recorder
        self._clock = clock
        self.health = RegulatoryOfficialCollectorHealth()

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self._poll()
            await self._publish()
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._poll_seconds)

    async def _publish(self) -> None:
        try:
            await asyncio.to_thread(self._publish_recent, require_utc(self._clock()))
            self.health.publication_count += 1
            self.health.publication_error_class = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.health.publication_error_class != type(exc).__name__:
                logger.exception("official regulation trigger publication failed")
            self.health.publication_error_class = type(exc).__name__

    async def _poll(self) -> None:
        started_at = require_utc(self._clock())
        self.health.poll_count += 1
        try:
            document = await asyncio.to_thread(
                self._source.fetch,
                observed_at=started_at,
            )
            result = (
                OfficialFactIngestionResult(records=(), new_fact_revisions=())
                if document is None
                else await asyncio.to_thread(
                    self._ingestor.ingest,
                    document,
                    observed_at=require_utc(self._clock()),
                )
            )
            completed_at = max(require_utc(self._clock()), started_at)
            self._record_poll(
                status=(
                    SourcePollStatus.CHANGED
                    if any(item.inserted for item in result.records)
                    else SourcePollStatus.UNCHANGED
                ),
                started_at=started_at,
                completed_at=completed_at,
                result=result,
            )
            self.health.new_fact_revision_count += len(result.new_fact_revisions)
            self.health.last_success_at = completed_at
            self.health.error_class = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.health.error_class != type(exc).__name__:
                logger.exception("official regulation collector failed")
            self.health.error_class = type(exc).__name__
            if isinstance(exc, SourcePollAuditError):
                raise
            self._record_poll(
                status=SourcePollStatus.FAILED,
                started_at=started_at,
                completed_at=max(require_utc(self._clock()), started_at),
                error_class=type(exc).__name__,
            )

    def _record_poll(
        self,
        *,
        status: SourcePollStatus,
        started_at: datetime,
        completed_at: datetime,
        result: OfficialFactIngestionResult | None = None,
        error_class: str | None = None,
    ) -> None:
        if self._poll_recorder is None:
            return
        records = () if result is None else result.records
        poll = build_source_poll_record(
            source_stream_id=FEDERAL_REGISTER_RULEMAKING_STREAM_ID,
            domain=CausalDomain.REGULATION_LEGISLATION,
            status=status,
            started_at=started_at,
            completed_at=completed_at,
            poll_interval_seconds=self._poll_seconds,
            latest_publication_at=max(
                (
                    item.record.observation.source_published_at
                    for item in records
                    if item.record.observation.source_published_at is not None
                ),
                default=None,
            ),
            observation_count=len(records),
            new_fact_count=(
                0 if result is None else len(result.new_fact_revisions)
            ),
            error_class=error_class,
        )
        try:
            self._poll_recorder.put(poll)
        except Exception as exc:
            raise SourcePollAuditError(
                "Federal Register 来源轮询事实无法持久化"
            ) from exc


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
        poll_recorder: SourcePollRecorder | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if monetary_poll_seconds < 1 or calendar_poll_seconds < 1:
            raise ValueError("Fed official polling interval 必须为正数")
        self._source = source
        self._ingestor = ingestor
        self._publish_recent = publish_recent
        self._monetary_poll_seconds = monetary_poll_seconds
        self._calendar_poll_seconds = calendar_poll_seconds
        self._poll_recorder = poll_recorder
        self._clock = clock
        self._monetary_records: tuple[FedMonetaryReleaseRecord, ...] = ()
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
        started_at = require_utc(self._clock())
        setattr(self.health, counter_field, getattr(self.health, counter_field) + 1)
        try:
            result = await asyncio.to_thread(self._collect, kind)
            completed_at = require_utc(self._clock())
            self._record_poll(
                kind=kind,
                status=(
                    SourcePollStatus.CHANGED
                    if any(item.inserted for item in result.records)
                    else SourcePollStatus.UNCHANGED
                ),
                started_at=started_at,
                completed_at=completed_at,
                result=result,
            )
            setattr(
                self.health,
                f"last_{kind}_success_at",
                completed_at,
            )
            setattr(self.health, f"{kind}_error_class", None)
            self.health.new_fact_revision_count += len(result.new_fact_revisions)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_error(exc, kind)
            if isinstance(exc, SourcePollAuditError):
                raise
            self._record_poll(
                kind=kind,
                status=SourcePollStatus.FAILED,
                started_at=started_at,
                completed_at=max(require_utc(self._clock()), started_at),
                error_class=type(exc).__name__,
            )

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
            records: list[StructuredRecordWrite] = []
            facts: list[CanonicalFactRevision] = []
            if content is not None:
                rss_result = self._ingestor.ingest_monetary_rss(
                    content,
                    observed_at=require_utc(self._clock()),
                )
                records.extend(rss_result.records)
                facts.extend(rss_result.new_fact_revisions)
                self._monetary_records = tuple(
                    item.record
                    for item in rss_result.records
                    if isinstance(item.record, FedMonetaryReleaseRecord)
                )
            cutoff = require_utc(self._clock()) - timedelta(days=14)
            refreshed: list[FedMonetaryReleaseRecord] = []
            for record in self._monetary_records:
                published_at = record.observation.source_published_at
                if (
                    record.policy_state is not None
                    or published_at is None
                    or published_at < cutoff
                    or not fed_policy_document_eligible(record)
                ):
                    refreshed.append(record)
                    continue
                document = self._source.fetch_monetary_document(record.source_url)
                if document is None:
                    refreshed.append(record)
                    continue
                document_result = self._ingestor.ingest_monetary_document(
                    record,
                    document.content,
                    document_url=document.source_url,
                    observed_at=max(
                        require_utc(self._clock()),
                        record.observation.observed_at + timedelta(microseconds=1),
                    ),
                )
                records.extend(document_result.records)
                facts.extend(document_result.new_fact_revisions)
                enriched = document_result.records[0].record
                if not isinstance(enriched, FedMonetaryReleaseRecord):
                    raise TypeError("Fed 政策原文返回错误记录类型")
                refreshed.append(enriched)
            self._monetary_records = tuple(refreshed)
            result = OfficialFactIngestionResult(
                records=tuple(records),
                new_fact_revisions=tuple(facts),
            )
            if content is None and not result.records:
                return result
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

    def _record_poll(
        self,
        *,
        kind: str,
        status: SourcePollStatus,
        started_at: datetime,
        completed_at: datetime,
        result: OfficialFactIngestionResult | None = None,
        error_class: str | None = None,
    ) -> None:
        if self._poll_recorder is None:
            return
        records = () if result is None else result.records
        publications = tuple(
            item.record.observation.source_published_at
            or item.record.observation.observed_at
            for item in records
        )
        poll = build_source_poll_record(
            source_stream_id=_FED_STREAMS[kind],
            domain=CausalDomain.MONETARY_INFLATION,
            status=status,
            started_at=started_at,
            completed_at=completed_at,
            poll_interval_seconds=(
                self._monetary_poll_seconds
                if kind == "monetary"
                else self._calendar_poll_seconds
            ),
            latest_publication_at=max(publications, default=None),
            observation_count=len(records),
            new_fact_count=(
                0 if result is None else len(result.new_fact_revisions)
            ),
            error_class=error_class,
        )
        try:
            self._poll_recorder.put(poll)
        except Exception as exc:
            raise SourcePollAuditError(
                f"Fed official {kind} 来源轮询事实无法持久化"
            ) from exc


class SqlTreasuryBuybackFactIngestor:
    """Project the official Treasury buyback calendar into durable facts."""

    def __init__(
        self,
        engine: Engine,
        policy: OfficialFactProjectionPolicy,
    ) -> None:
        self._official = SqlTreasuryBuybackInformationIngestor(engine)
        self._facts = SqlFactStateStore(engine)
        self._policy = policy

    def ingest_calendar(
        self,
        content: bytes,
        *,
        observed_at: datetime,
    ) -> OfficialFactIngestionResult:
        writes = self._official.ingest_calendar(content, observed_at=observed_at)
        projected: list[CanonicalFactRevision] = []
        for write in writes:
            record = write.record
            if not isinstance(record, TreasuryBuybackOperationRecord):
                raise TypeError("Treasury buyback ingestor 收到非回购记录")
            if write.calendar_revision is None:
                raise ValueError("Treasury buyback 官方记录缺少 Calendar revision")
            candidate = project_treasury_buyback_operation_fact(
                record,
                write.calendar_revision,
                policy=self._policy,
            )
            previous = self._facts.latest_fact(candidate.fact_id)
            if previous is not None and not write.inserted:
                continue
            if previous is not None and previous.revision_hash == candidate.revision_hash:
                continue
            fact = (
                candidate
                if previous is None
                else project_treasury_buyback_operation_fact(
                    record,
                    write.calendar_revision,
                    policy=self._policy,
                    previous=previous,
                )
            )
            stored = self._facts.put_fact(fact)
            if previous is None or stored.revision_id != previous.revision_id:
                projected.append(stored)
        return OfficialFactIngestionResult(
            records=writes,
            new_fact_revisions=tuple(projected),
        )

    def ingest_result(
        self,
        content: bytes,
        *,
        scheduled: TreasuryBuybackOperationRecord,
        observed_at: datetime,
    ) -> OfficialFactIngestionResult:
        write = self._official.ingest_result(
            content,
            scheduled=scheduled,
            observed_at=observed_at,
        )
        if not isinstance(write.record, TreasuryBuybackResultRecord):
            raise TypeError("Treasury buyback result ingestor 收到非结果记录")
        candidate = project_treasury_buyback_result_fact(
            write.record,
            policy=self._policy,
        )
        previous = self._facts.latest_fact(candidate.fact_id)
        if not write.inserted or (
            previous is not None and previous.revision_hash == candidate.revision_hash
        ):
            return OfficialFactIngestionResult(records=(write,), new_fact_revisions=())
        fact = (
            candidate
            if previous is None
            else project_treasury_buyback_result_fact(
                write.record,
                policy=self._policy,
                previous=previous,
            )
        )
        stored = self._facts.put_fact(fact)
        return OfficialFactIngestionResult(
            records=(write,),
            new_fact_revisions=(stored,),
        )


@dataclass(slots=True)
class TreasuryBuybackCollectorHealth:
    poll_count: int = 0
    new_fact_revision_count: int = 0
    publication_count: int = 0
    last_success_at: datetime | None = None
    error_class: str | None = None
    publication_error_class: str | None = None


class TreasuryBuybackCollectorService:
    """Poll the official buyback calendar and keep event wakeups synchronized."""

    def __init__(
        self,
        *,
        source: TreasuryBuybackSource,
        ingestor: SqlTreasuryBuybackFactIngestor,
        publish_recent: Callable[[datetime], None],
        poll_seconds: int,
        result_lookback_seconds: int,
        poll_recorder: SourcePollRecorder | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if poll_seconds < 1 or result_lookback_seconds < 1:
            raise ValueError("Treasury buyback polling/result lookback 必须为正数")
        self._source = source
        self._ingestor = ingestor
        self._publish_recent = publish_recent
        self._poll_seconds = poll_seconds
        self._result_lookback_seconds = result_lookback_seconds
        self._poll_recorder = poll_recorder
        self._clock = clock
        self._known_operations: dict[str, TreasuryBuybackOperationRecord] = {}
        self.health = TreasuryBuybackCollectorHealth()

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self._poll()
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._poll_seconds)

    async def _poll(self) -> None:
        started_at = require_utc(self._clock())
        self.health.poll_count += 1
        try:
            content = await asyncio.to_thread(self._source.fetch_calendar)
            calendar_result = (
                OfficialFactIngestionResult(records=(), new_fact_revisions=())
                if content is None
                else await asyncio.to_thread(
                    self._ingestor.ingest_calendar,
                    content,
                    observed_at=require_utc(self._clock()),
                )
            )
            for write in calendar_result.records:
                if isinstance(write.record, TreasuryBuybackOperationRecord):
                    self._known_operations[
                        write.record.observation.source_record_id
                    ] = write.record
            result_records = list(calendar_result.records)
            result_facts = list(calendar_result.new_fact_revisions)
            result_window_start = started_at - timedelta(
                seconds=self._result_lookback_seconds
            )
            for scheduled in sorted(
                self._known_operations.values(),
                key=lambda item: item.operation_end_at,
            ):
                if not (
                    scheduled.status == CalendarEventStatus.SCHEDULED
                    and result_window_start <= scheduled.operation_end_at <= started_at
                ):
                    continue
                result_content = await asyncio.to_thread(
                    self._source.fetch_result,
                    scheduled,
                )
                if result_content is None:
                    continue
                result_part = await asyncio.to_thread(
                    self._ingestor.ingest_result,
                    result_content,
                    scheduled=scheduled,
                    observed_at=require_utc(self._clock()),
                )
                result_records.extend(result_part.records)
                result_facts.extend(result_part.new_fact_revisions)
            result = OfficialFactIngestionResult(
                records=tuple(result_records),
                new_fact_revisions=tuple(result_facts),
            )
            completed_at = max(require_utc(self._clock()), started_at)
            self._record_poll(
                status=(
                    SourcePollStatus.CHANGED
                    if any(item.inserted for item in result.records)
                    else SourcePollStatus.UNCHANGED
                ),
                started_at=started_at,
                completed_at=completed_at,
                result=result,
            )
            self.health.new_fact_revision_count += len(result.new_fact_revisions)
            self.health.last_success_at = completed_at
            self.health.error_class = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.health.error_class != type(exc).__name__:
                logger.exception("Treasury buyback collector failed")
            self.health.error_class = type(exc).__name__
            if isinstance(exc, SourcePollAuditError):
                raise
            self._record_poll(
                status=SourcePollStatus.FAILED,
                started_at=started_at,
                completed_at=max(require_utc(self._clock()), started_at),
                error_class=type(exc).__name__,
            )
            return
        try:
            await asyncio.to_thread(self._publish_recent, completed_at)
            self.health.publication_count += 1
            self.health.publication_error_class = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.health.publication_error_class != type(exc).__name__:
                logger.exception("Treasury buyback fact publication failed")
            self.health.publication_error_class = type(exc).__name__

    def _record_poll(
        self,
        *,
        status: SourcePollStatus,
        started_at: datetime,
        completed_at: datetime,
        result: OfficialFactIngestionResult | None = None,
        error_class: str | None = None,
    ) -> None:
        if self._poll_recorder is None:
            return
        records = () if result is None else result.records
        poll = build_source_poll_record(
            source_stream_id=TREASURY_BUYBACK_STREAM_ID,
            domain=CausalDomain.FISCAL_DEBT,
            status=status,
            started_at=started_at,
            completed_at=completed_at,
            poll_interval_seconds=self._poll_seconds,
            latest_publication_at=max(
                (item.record.observation.observed_at for item in records),
                default=None,
            ),
            valid_until=max(
                (
                    item.record.operation_end_at
                    for item in records
                    if isinstance(item.record, TreasuryBuybackOperationRecord)
                    and item.record.status == CalendarEventStatus.SCHEDULED
                ),
                default=None,
            ),
            observation_count=len(records),
            new_fact_count=(
                0 if result is None else len(result.new_fact_revisions)
            ),
            error_class=error_class,
        )
        try:
            self._poll_recorder.put(poll)
        except Exception as exc:
            raise SourcePollAuditError(
                "Treasury buyback 来源轮询事实无法持久化"
            ) from exc
