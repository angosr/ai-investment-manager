from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.information.models import (
    CausalDomain,
    CoverageStatus,
    DomainCoverageSnapshot,
    SourcePollRecord,
    SourcePollStatus,
)
from investment_manager.information.policy import CoverageRequirement
from investment_manager.information.tables import source_poll_records
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc


def build_source_poll_record(
    *,
    source_stream_id: str,
    domain: CausalDomain,
    status: SourcePollStatus,
    started_at: datetime,
    completed_at: datetime,
    poll_interval_seconds: int,
    latest_publication_at: datetime | None = None,
    valid_until: datetime | None = None,
    observation_count: int = 0,
    new_fact_count: int = 0,
    error_class: str | None = None,
) -> SourcePollRecord:
    if poll_interval_seconds < 1:
        raise ValueError("来源轮询周期必须为正数")
    completed_at = require_utc(completed_at)
    payload = {
        "source_stream_id": source_stream_id,
        "domain": domain,
        "status": status,
        "started_at": require_utc(started_at),
        "completed_at": completed_at,
        "latest_publication_at": latest_publication_at,
        "observation_count": observation_count,
        "new_fact_count": new_fact_count,
        "error_class": error_class,
    }
    if status != SourcePollStatus.FAILED:
        # One delayed opportunity is tolerated; two missed cycles mean the
        # collector no longer proves that this source is current.
        payload["poll_fresh_until"] = completed_at + timedelta(
            seconds=2 * poll_interval_seconds
        )
    if valid_until is not None:
        payload["valid_until"] = require_utc(valid_until)
    return SourcePollRecord(
        poll_id=stable_id("source_poll", content_hash(payload)),
        **payload,
    )


class SqlInformationCoverageStore:
    """Append-only poll outcomes and point-in-time causal-domain coverage."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def put(self, poll: SourcePollRecord) -> bool:
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(source_poll_records).values(
                        poll_id=poll.poll_id,
                        source_stream_id=poll.source_stream_id,
                        domain=poll.domain.value,
                        status=poll.status.value,
                        completed_at=poll.completed_at,
                        payload=poll.model_dump(mode="json"),
                    )
                )
        except IntegrityError:
            with self._engine.connect() as connection:
                existing = connection.execute(
                    select(source_poll_records.c.payload).where(
                        source_poll_records.c.poll_id == poll.poll_id
                    )
                ).scalar_one_or_none()
            if existing != poll.model_dump(mode="json"):
                raise ValueError("来源轮询身份对应不同内容") from None
            return False
        return True

    def snapshot(
        self,
        *,
        as_of: datetime,
        requirements: tuple[CoverageRequirement, ...],
    ) -> tuple[DomainCoverageSnapshot, ...]:
        as_of = require_utc(as_of)
        stream_ids = tuple(
            sorted(
                {
                    source.stream_id
                    for requirement in requirements
                    for source in requirement.sources
                }
            )
        )
        latest_by_stream: dict[str, SourcePollRecord] = {}
        latest_success_by_stream: dict[str, SourcePollRecord] = {}
        latest_publication_by_stream: dict[str, SourcePollRecord] = {}
        latest_validity_by_stream: dict[str, SourcePollRecord] = {}
        if stream_ids:
            with self._engine.connect() as connection:
                rows = connection.execute(
                    select(source_poll_records.c.payload)
                    .where(
                        source_poll_records.c.source_stream_id.in_(stream_ids),
                        source_poll_records.c.completed_at <= as_of,
                    )
                    .order_by(
                        source_poll_records.c.completed_at.desc(),
                        source_poll_records.c.poll_id.desc(),
                    )
                ).scalars()
                for payload in rows:
                    poll = SourcePollRecord.model_validate(payload)
                    latest_by_stream.setdefault(poll.source_stream_id, poll)
                    if poll.status != SourcePollStatus.FAILED:
                        latest_success_by_stream.setdefault(
                            poll.source_stream_id,
                            poll,
                        )
                        if poll.latest_publication_at is not None:
                            latest_publication_by_stream.setdefault(
                                poll.source_stream_id,
                                poll,
                            )
                        if poll.valid_until is not None:
                            latest_validity_by_stream.setdefault(
                                poll.source_stream_id,
                                poll,
                            )
        snapshots = tuple(
            self._domain_snapshot(
                as_of=as_of,
                requirement=requirement,
                latest_by_stream=latest_by_stream,
                latest_success_by_stream=latest_success_by_stream,
                latest_publication_by_stream=latest_publication_by_stream,
                latest_validity_by_stream=latest_validity_by_stream,
            )
            for requirement in requirements
        )
        return tuple(sorted(snapshots, key=lambda item: item.domain.value))

    @staticmethod
    def gap_codes(snapshots: tuple[DomainCoverageSnapshot, ...]) -> tuple[str, ...]:
        return tuple(
            sorted(
                f"INFORMATION_{item.domain.value}_{item.status.value}"
                for item in snapshots
                if item.status
                in {
                    CoverageStatus.PARTIAL,
                    CoverageStatus.NOT_CONFIGURED,
                    CoverageStatus.SOURCE_FAILED,
                    CoverageStatus.SOURCE_STALE,
                }
            )
        )

    @staticmethod
    def _domain_snapshot(
        *,
        as_of: datetime,
        requirement: CoverageRequirement,
        latest_by_stream: dict[str, SourcePollRecord],
        latest_success_by_stream: dict[str, SourcePollRecord],
        latest_publication_by_stream: dict[str, SourcePollRecord],
        latest_validity_by_stream: dict[str, SourcePollRecord],
    ) -> DomainCoverageSnapshot:
        streams = tuple(source.stream_id for source in requirement.sources)
        covered_capabilities = tuple(
            sorted(
                {
                    capability
                    for source in requirement.sources
                    for capability in source.capabilities
                }
            )
        )
        missing_capabilities = tuple(
            sorted(set(requirement.required_capabilities) - set(covered_capabilities))
        )
        if not streams:
            return DomainCoverageSnapshot(
                domain=requirement.domain,
                status=CoverageStatus.NOT_CONFIGURED,
                as_of=as_of,
                missing_capabilities=requirement.required_capabilities,
            )
        polls = tuple(
            latest_by_stream[item] for item in streams if item in latest_by_stream
        )
        mismatched = tuple(
            item.source_stream_id
            for item in polls
            if item.domain != requirement.domain
        )
        if mismatched:
            raise ValueError(
                "coverage source stream 与因果领域不一致: "
                + ", ".join(sorted(mismatched))
            )
        refs = tuple(sorted(item.poll_id for item in polls))
        successes = tuple(
            latest_success_by_stream[item]
            for item in streams
            if item in latest_success_by_stream
        )
        latest_success = min(
            (item.completed_at for item in successes),
            default=None,
        )
        publications = tuple(
            latest_publication_by_stream[item].latest_publication_at
            for item in streams
            if item in latest_publication_by_stream
        )
        latest_publication = min(publications, default=None)
        latest_polls_failed = any(
            item.status == SourcePollStatus.FAILED for item in polls
        )
        if latest_polls_failed:
            status = CoverageStatus.SOURCE_FAILED
        elif len(successes) != len(streams) or any(
            item.poll_fresh_until is None or item.poll_fresh_until < as_of
            for item in successes
        ):
            status = CoverageStatus.SOURCE_STALE
        elif any(
            source.maximum_publication_age_seconds is not None
            and not _stream_has_current_publication_or_validity(
                stream_id=source.stream_id,
                as_of=as_of,
                maximum_publication_age_seconds=(
                    source.maximum_publication_age_seconds
                ),
                latest_publication_by_stream=latest_publication_by_stream,
                latest_validity_by_stream=latest_validity_by_stream,
            )
            for source in requirement.sources
        ):
            status = CoverageStatus.NO_RECENT_PUBLICATION
        else:
            status = (
                CoverageStatus.PARTIAL
                if missing_capabilities
                else CoverageStatus.CURRENT
            )
        return DomainCoverageSnapshot(
            domain=requirement.domain,
            status=status,
            as_of=as_of,
            source_stream_ids=streams,
            covered_capabilities=covered_capabilities,
            missing_capabilities=missing_capabilities,
            latest_success_at=latest_success,
            latest_publication_at=latest_publication,
            latest_poll_refs=refs,
        )


def _stream_has_current_publication_or_validity(
    *,
    stream_id: str,
    as_of: datetime,
    maximum_publication_age_seconds: int,
    latest_publication_by_stream: dict[str, SourcePollRecord],
    latest_validity_by_stream: dict[str, SourcePollRecord],
) -> bool:
    publication = latest_publication_by_stream.get(stream_id)
    if (
        publication is not None
        and publication.latest_publication_at is not None
        and publication.latest_publication_at
        >= as_of - timedelta(seconds=maximum_publication_age_seconds)
    ):
        return True
    validity = latest_validity_by_stream.get(stream_id)
    return (
        validity is not None
        and validity.valid_until is not None
        and validity.valid_until >= as_of
    )
