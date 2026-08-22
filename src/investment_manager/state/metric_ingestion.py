from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy.engine import Engine

from investment_manager.information.aggregated_flows import (
    ETF_AGGREGATE_FLOW_STREAM_ID,
    AggregatedEtfFlowDocument,
    AggregatedEtfFlowSnapshot,
    parse_aggregated_etf_flows,
)
from investment_manager.information.coverage import build_source_poll_record
from investment_manager.information.models import (
    CausalDomain,
    SourcePollRecord,
    SourcePollStatus,
)
from investment_manager.information.official.metrics import (
    ARKB_HOLDINGS_STREAM_ID,
    BITB_HOLDINGS_STREAM_ID,
    FED_BROAD_DOLLAR_STREAM_ID,
    IBIT_HOLDINGS_STREAM_ID,
    NYFED_RATES_STREAM_ID,
    NYFED_RRP_STREAM_ID,
    NYFED_SOMA_STREAM_ID,
    TGA_STREAM_ID,
    TREASURY_YIELD_STREAM_ID,
    parse_official_metric_document,
    with_official_metric_history,
)
from investment_manager.information.official.repository import (
    SqlStructuredInformationStore,
    StructuredRecordWrite,
)
from investment_manager.information.official.source import OfficialMetricDocument
from investment_manager.information.raw_payload import build_raw_source_payload
from investment_manager.information.raw_repository import SqlRawSourcePayloadStore
from investment_manager.kernel.time import require_utc
from investment_manager.state.facts import (
    OfficialFactProjectionPolicy,
    project_aggregated_etf_flow_fact,
    project_official_metric_fact,
)
from investment_manager.state.models import CanonicalFactRevision
from investment_manager.state.official_ingestion import SourcePollAuditError
from investment_manager.state.repository import SqlFactStateStore

logger = logging.getLogger(__name__)

OFFICIAL_METRIC_STREAM_DOMAINS = {
    ARKB_HOLDINGS_STREAM_ID: CausalDomain.INSTITUTIONAL_FLOWS,
    BITB_HOLDINGS_STREAM_ID: CausalDomain.INSTITUTIONAL_FLOWS,
    TGA_STREAM_ID: CausalDomain.FISCAL_DEBT,
    TREASURY_YIELD_STREAM_ID: CausalDomain.CROSS_ASSET_EXTERNAL,
    FED_BROAD_DOLLAR_STREAM_ID: CausalDomain.CROSS_ASSET_EXTERNAL,
    IBIT_HOLDINGS_STREAM_ID: CausalDomain.INSTITUTIONAL_FLOWS,
    NYFED_RRP_STREAM_ID: CausalDomain.MONETARY_INFLATION,
    NYFED_SOMA_STREAM_ID: CausalDomain.MONETARY_INFLATION,
    NYFED_RATES_STREAM_ID: CausalDomain.MONETARY_INFLATION,
}
SLOW_OFFICIAL_METRIC_STREAMS = {
    ARKB_HOLDINGS_STREAM_ID,
    BITB_HOLDINGS_STREAM_ID,
    FED_BROAD_DOLLAR_STREAM_ID,
    IBIT_HOLDINGS_STREAM_ID,
    NYFED_SOMA_STREAM_ID,
}


class OfficialMetricSource(Protocol):
    stream_ids: tuple[str, ...]

    def fetch(
        self,
        stream_id: str,
        *,
        observed_at: datetime,
    ) -> OfficialMetricDocument | None: ...


class SourcePollRecorder(Protocol):
    def put(self, poll: SourcePollRecord) -> bool: ...


class AggregatedEtfFlowSource(Protocol):
    def fetch(self, *, observed_at: datetime) -> AggregatedEtfFlowDocument: ...


@dataclass(frozen=True, slots=True)
class OfficialMetricIngestionResult:
    record: StructuredRecordWrite | None
    new_fact_revision: CanonicalFactRevision | None


class SqlOfficialMetricFactIngestor:
    """Persist raw official metric payloads before their canonical fact projection."""

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

    def ingest(
        self,
        document: OfficialMetricDocument,
        *,
        observed_at: datetime,
    ) -> OfficialMetricIngestionResult:
        observed_at = require_utc(observed_at)
        snapshot = parse_official_metric_document(
            document.stream_id,
            document.content,
            source_url=document.source_url,
            media_type=document.media_type,
            observed_at=observed_at,
        )
        snapshot = with_official_metric_history(
            snapshot,
            self._records.metric_history(
                source_id=snapshot.observation.source_id,
                source_record_id=snapshot.observation.source_record_id,
                as_of=observed_at,
            ),
        )
        raw = build_raw_source_payload(
            source_id=snapshot.observation.source_id,
            source_url=document.source_url,
            media_type=document.media_type,
            observed_at=observed_at,
            content=document.content,
        )
        if raw.payload_id != snapshot.observation.payload_ref:
            raise ValueError("官方指标原始 payload identity 不一致")
        self._raw.put(raw, document.content)
        write = self._records.put(snapshot)
        candidate = project_official_metric_fact(
            write.record,
            policy=self._policy,
        )
        previous = self._facts.latest_fact(candidate.fact_id)
        if previous is not None and not write.inserted:
            return OfficialMetricIngestionResult(record=write, new_fact_revision=None)
        if previous is not None and previous.revision_hash == candidate.revision_hash:
            return OfficialMetricIngestionResult(record=write, new_fact_revision=None)
        fact = (
            candidate
            if previous is None
            else project_official_metric_fact(
                write.record,
                policy=self._policy,
                previous=previous,
            )
        )
        stored = self._facts.put_fact(fact)
        return OfficialMetricIngestionResult(record=write, new_fact_revision=stored)


@dataclass(frozen=True, slots=True)
class AggregatedEtfFlowIngestionResult:
    records: tuple[StructuredRecordWrite, ...]
    new_fact_revisions: tuple[CanonicalFactRevision, ...]


class SqlAggregatedEtfFlowFactIngestor:
    """Persist one aggregate dataset before projecting its latest BTC/ETH rows."""

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

    def ingest(
        self,
        document: AggregatedEtfFlowDocument,
        *,
        observed_at: datetime,
    ) -> AggregatedEtfFlowIngestionResult:
        observed_at = require_utc(observed_at)
        snapshots = parse_aggregated_etf_flows(document, observed_at=observed_at)
        raw = build_raw_source_payload(
            source_id=snapshots[0].observation.source_id,
            source_url=document.source_url,
            media_type=document.media_type,
            observed_at=observed_at,
            content=document.content,
        )
        if any(item.observation.payload_ref != raw.payload_id for item in snapshots):
            raise ValueError("ETF 合计流原始 payload identity 不一致")
        self._raw.put(raw, document.content)

        writes: list[StructuredRecordWrite] = []
        facts: list[CanonicalFactRevision] = []
        for snapshot in snapshots:
            write = self._records.put(snapshot)
            if not isinstance(write.record, AggregatedEtfFlowSnapshot):
                raise ValueError("ETF 合计流存储返回了错误记录类型")
            writes.append(write)
            candidate = project_aggregated_etf_flow_fact(
                write.record,
                policy=self._policy,
            )
            previous = self._facts.latest_fact(candidate.fact_id)
            if not write.inserted or (
                previous is not None and previous.revision_hash == candidate.revision_hash
            ):
                continue
            fact = (
                candidate
                if previous is None
                else project_aggregated_etf_flow_fact(
                    write.record,
                    policy=self._policy,
                    previous=previous,
                )
            )
            facts.append(self._facts.put_fact(fact))
        return AggregatedEtfFlowIngestionResult(
            records=tuple(writes),
            new_fact_revisions=tuple(facts),
        )


@dataclass(slots=True)
class OfficialMetricCollectorHealth:
    poll_count: int = 0
    new_fact_revision_count: int = 0
    last_success_at: datetime | None = None
    last_error_by_stream: dict[str, str] = field(default_factory=dict)


class OfficialMetricCollectorService:
    """Poll independent official streams without letting one failure blind the rest."""

    def __init__(
        self,
        *,
        source: OfficialMetricSource,
        ingestor: SqlOfficialMetricFactIngestor,
        publish_recent: Callable[[datetime], None],
        fast_poll_seconds: int,
        slow_poll_seconds: int,
        poll_recorder: SourcePollRecorder | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if fast_poll_seconds < 1 or slow_poll_seconds < fast_poll_seconds:
            raise ValueError("官方指标轮询周期非法")
        if set(source.stream_ids) != set(OFFICIAL_METRIC_STREAM_DOMAINS):
            raise ValueError("官方指标 source streams 与覆盖合同不一致")
        self._source = source
        self._ingestor = ingestor
        self._publish_recent = publish_recent
        self._fast_poll_seconds = fast_poll_seconds
        self._slow_poll_seconds = slow_poll_seconds
        self._poll_recorder = poll_recorder
        self._clock = clock
        self.health = OfficialMetricCollectorHealth()

    async def run(self, stop: asyncio.Event) -> None:
        next_poll_at = {stream_id: None for stream_id in self._source.stream_ids}
        while not stop.is_set():
            now = require_utc(self._clock())
            due = tuple(
                stream_id
                for stream_id in self._source.stream_ids
                if next_poll_at[stream_id] is None or now >= next_poll_at[stream_id]
            )
            for stream_id in due:
                interval = (
                    self._slow_poll_seconds
                    if stream_id in SLOW_OFFICIAL_METRIC_STREAMS
                    else self._fast_poll_seconds
                )
                next_poll_at[stream_id] = now + timedelta(seconds=interval)
                await self._poll(stream_id)
            try:
                await asyncio.to_thread(self._publish_recent, require_utc(self._clock()))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("official metric trigger publication failed")
            now = require_utc(self._clock())
            delay = max(
                0.1,
                min((item - now).total_seconds() for item in next_poll_at.values() if item),
            )
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)

    async def _poll(self, stream_id: str) -> None:
        started_at = require_utc(self._clock())
        self.health.poll_count += 1
        try:
            document = await asyncio.to_thread(
                self._source.fetch,
                stream_id,
                observed_at=started_at,
            )
            result = (
                OfficialMetricIngestionResult(record=None, new_fact_revision=None)
                if document is None
                else await asyncio.to_thread(
                    self._ingestor.ingest,
                    document,
                    observed_at=require_utc(self._clock()),
                )
            )
            completed_at = require_utc(self._clock())
            self._record_poll(
                stream_id=stream_id,
                status=(
                    SourcePollStatus.CHANGED
                    if result.new_fact_revision is not None
                    else SourcePollStatus.UNCHANGED
                ),
                started_at=started_at,
                completed_at=completed_at,
                result=result,
            )
            self.health.last_success_at = completed_at
            self.health.last_error_by_stream.pop(stream_id, None)
            if result.new_fact_revision is not None:
                self.health.new_fact_revision_count += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.health.last_error_by_stream.get(stream_id) != type(exc).__name__:
                logger.exception("official metric collector failed: %s", stream_id)
            self.health.last_error_by_stream[stream_id] = type(exc).__name__
            if isinstance(exc, SourcePollAuditError):
                raise
            self._record_poll(
                stream_id=stream_id,
                status=SourcePollStatus.FAILED,
                started_at=started_at,
                completed_at=max(require_utc(self._clock()), started_at),
                error_class=type(exc).__name__,
            )

    def _record_poll(
        self,
        *,
        stream_id: str,
        status: SourcePollStatus,
        started_at: datetime,
        completed_at: datetime,
        result: OfficialMetricIngestionResult | None = None,
        error_class: str | None = None,
    ) -> None:
        if self._poll_recorder is None:
            return
        record = None if result is None else result.record
        poll = build_source_poll_record(
            source_stream_id=stream_id,
            domain=OFFICIAL_METRIC_STREAM_DOMAINS[stream_id],
            status=status,
            started_at=started_at,
            completed_at=completed_at,
            latest_publication_at=(
                None if record is None else record.record.observation.source_published_at
            ),
            observation_count=0 if record is None else 1,
            new_fact_count=(0 if result is None or result.new_fact_revision is None else 1),
            error_class=error_class,
        )
        try:
            self._poll_recorder.put(poll)
        except Exception as exc:
            raise SourcePollAuditError(
                f"官方指标 {stream_id} 来源轮询事实无法持久化"
            ) from exc


@dataclass(slots=True)
class AggregatedEtfFlowCollectorHealth:
    poll_count: int = 0
    new_fact_revision_count: int = 0
    last_success_at: datetime | None = None
    last_error_class: str | None = None


class AggregatedEtfFlowCollectorService:
    """Poll one finalized aggregate-flow dataset with explicit audit health."""

    def __init__(
        self,
        *,
        source: AggregatedEtfFlowSource,
        ingestor: SqlAggregatedEtfFlowFactIngestor,
        publish_recent: Callable[[datetime], None],
        poll_seconds: int,
        poll_recorder: SourcePollRecorder | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if poll_seconds < 1:
            raise ValueError("ETF 合计流轮询周期必须为正数")
        self._source = source
        self._ingestor = ingestor
        self._publish_recent = publish_recent
        self._poll_seconds = poll_seconds
        self._poll_recorder = poll_recorder
        self._clock = clock
        self.health = AggregatedEtfFlowCollectorHealth()

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self._poll()
            try:
                await asyncio.to_thread(self._publish_recent, require_utc(self._clock()))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ETF aggregate-flow trigger publication failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._poll_seconds)

    async def _poll(self) -> None:
        started_at = require_utc(self._clock())
        self.health.poll_count += 1
        try:
            document = await asyncio.to_thread(
                self._source.fetch,
                observed_at=started_at,
            )
            result = await asyncio.to_thread(
                self._ingestor.ingest,
                document,
                observed_at=require_utc(self._clock()),
            )
            completed_at = require_utc(self._clock())
            self._record_poll(
                status=(
                    SourcePollStatus.CHANGED
                    if result.new_fact_revisions
                    else SourcePollStatus.UNCHANGED
                ),
                started_at=started_at,
                completed_at=completed_at,
                result=result,
            )
            self.health.last_success_at = completed_at
            self.health.last_error_class = None
            self.health.new_fact_revision_count += len(result.new_fact_revisions)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.health.last_error_class != type(exc).__name__:
                logger.exception("ETF aggregate-flow collector failed")
            self.health.last_error_class = type(exc).__name__
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
        result: AggregatedEtfFlowIngestionResult | None = None,
        error_class: str | None = None,
    ) -> None:
        if self._poll_recorder is None:
            return
        publications = tuple(
            record.record.observation.source_published_at
            for record in (() if result is None else result.records)
            if record.record.observation.source_published_at is not None
        )
        poll = build_source_poll_record(
            source_stream_id=ETF_AGGREGATE_FLOW_STREAM_ID,
            domain=CausalDomain.INSTITUTIONAL_FLOWS,
            status=status,
            started_at=started_at,
            completed_at=completed_at,
            latest_publication_at=max(publications, default=None),
            observation_count=0 if result is None else len(result.records),
            new_fact_count=(
                0 if result is None else len(result.new_fact_revisions)
            ),
            error_class=error_class,
        )
        try:
            self._poll_recorder.put(poll)
        except Exception as exc:
            raise SourcePollAuditError(
                "ETF 合计流来源轮询事实无法持久化"
            ) from exc
