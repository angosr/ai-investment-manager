from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.engine import Engine

from investment_manager.asset_management import CanonicalFactRevision
from investment_manager.fact_pipeline import (
    OfficialFactProjectionPolicy,
    project_fed_monetary_release_fact,
    project_fomc_calendar_fact,
)
from investment_manager.fact_state_sql import SqlFactStateStore
from investment_manager.official_information import FedMonetaryReleaseRecord, FomcMeetingRecord
from investment_manager.official_information_sql import (
    OfficialRecordWrite,
    SqlFedOfficialInformationIngestor,
)


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

    def _project(
        self,
        writes: tuple[OfficialRecordWrite, ...],
    ) -> tuple[CanonicalFactRevision, ...]:
        projected: list[CanonicalFactRevision] = []
        for write in writes:
            candidate = self._candidate(write, previous=None)
            previous = self._facts.latest_fact(candidate.fact_id)
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
        if isinstance(record, FedMonetaryReleaseRecord):
            return project_fed_monetary_release_fact(
                record,
                policy=self._policy,
                previous=previous,
            )
        raise TypeError(f"不支持的 Fed 官方记录类型: {type(record).__name__}")
