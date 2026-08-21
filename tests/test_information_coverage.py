from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine

from investment_manager.information.coverage import (
    SqlInformationCoverageStore,
    build_source_poll_record,
)
from investment_manager.information.models import (
    CausalDomain,
    CoverageStatus,
    SourcePollStatus,
)
from investment_manager.information.policy import CoverageRequirement
from investment_manager.schema import create_schema

AS_OF = datetime(2026, 8, 21, 16, tzinfo=UTC)


def _store() -> SqlInformationCoverageStore:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    return SqlInformationCoverageStore(engine)


def _requirement(
    domain: CausalDomain,
    streams: tuple[str, ...],
    *,
    poll_age: int = 120,
    publication_age: int | None = None,
) -> CoverageRequirement:
    return CoverageRequirement(
        domain=domain,
        source_stream_ids=streams,
        maximum_poll_age_seconds=poll_age,
        maximum_publication_age_seconds=publication_age,
    )


def test_coverage_distinguishes_current_unconfigured_and_stale() -> None:
    store = _store()
    store.put(
        build_source_poll_record(
            source_stream_id="fed-monetary-releases",
            domain=CausalDomain.MONETARY_INFLATION,
            status=SourcePollStatus.UNCHANGED,
            started_at=AS_OF - timedelta(seconds=31),
            completed_at=AS_OF - timedelta(seconds=30),
            latest_publication_at=AS_OF - timedelta(days=3),
        )
    )
    store.put(
        build_source_poll_record(
            source_stream_id="cross-asset-feed",
            domain=CausalDomain.CROSS_ASSET_EXTERNAL,
            status=SourcePollStatus.CHANGED,
            started_at=AS_OF - timedelta(minutes=11),
            completed_at=AS_OF - timedelta(minutes=10),
            latest_publication_at=AS_OF - timedelta(minutes=10),
            observation_count=1,
        )
    )

    snapshots = store.snapshot(
        as_of=AS_OF,
        requirements=(
            _requirement(CausalDomain.MONETARY_INFLATION, ("fed-monetary-releases",)),
            _requirement(CausalDomain.FISCAL_DEBT, ()),
            _requirement(CausalDomain.CROSS_ASSET_EXTERNAL, ("cross-asset-feed",)),
        ),
    )
    by_domain = {item.domain: item for item in snapshots}

    assert by_domain[CausalDomain.MONETARY_INFLATION].status == CoverageStatus.CURRENT
    assert by_domain[CausalDomain.FISCAL_DEBT].status == CoverageStatus.NOT_CONFIGURED
    assert by_domain[CausalDomain.CROSS_ASSET_EXTERNAL].status == CoverageStatus.SOURCE_STALE
    assert store.gap_codes(snapshots) == (
        "INFORMATION_CROSS_ASSET_EXTERNAL_SOURCE_STALE",
        "INFORMATION_FISCAL_DEBT_NOT_CONFIGURED",
    )


def test_coverage_distinguishes_failed_source_from_no_recent_publication() -> None:
    store = _store()
    store.put(
        build_source_poll_record(
            source_stream_id="etf-flow",
            domain=CausalDomain.INSTITUTIONAL_FLOWS,
            status=SourcePollStatus.UNCHANGED,
            started_at=AS_OF - timedelta(seconds=21),
            completed_at=AS_OF - timedelta(seconds=20),
            latest_publication_at=AS_OF - timedelta(days=3),
        )
    )
    store.put(
        build_source_poll_record(
            source_stream_id="onchain-feed",
            domain=CausalDomain.ONCHAIN_SUPPLY,
            status=SourcePollStatus.FAILED,
            started_at=AS_OF - timedelta(seconds=11),
            completed_at=AS_OF - timedelta(seconds=10),
            error_class="TimeoutError",
        )
    )

    snapshots = store.snapshot(
        as_of=AS_OF,
        requirements=(
            _requirement(
                CausalDomain.INSTITUTIONAL_FLOWS,
                ("etf-flow",),
                publication_age=86_400,
            ),
            _requirement(CausalDomain.ONCHAIN_SUPPLY, ("onchain-feed",)),
        ),
    )
    by_domain = {item.domain: item for item in snapshots}

    assert (
        by_domain[CausalDomain.INSTITUTIONAL_FLOWS].status
        == CoverageStatus.NO_RECENT_PUBLICATION
    )
    assert by_domain[CausalDomain.ONCHAIN_SUPPLY].status == CoverageStatus.SOURCE_FAILED
    assert store.gap_codes(snapshots) == (
        "INFORMATION_ONCHAIN_SUPPLY_SOURCE_FAILED",
    )


def test_coverage_is_point_in_time_and_ignores_future_recovery() -> None:
    store = _store()
    failed = build_source_poll_record(
        source_stream_id="regulatory-feed",
        domain=CausalDomain.REGULATION_LEGISLATION,
        status=SourcePollStatus.FAILED,
        started_at=AS_OF - timedelta(seconds=11),
        completed_at=AS_OF - timedelta(seconds=10),
        error_class="ConnectError",
    )
    recovered = build_source_poll_record(
        source_stream_id="regulatory-feed",
        domain=CausalDomain.REGULATION_LEGISLATION,
        status=SourcePollStatus.UNCHANGED,
        started_at=AS_OF + timedelta(seconds=9),
        completed_at=AS_OF + timedelta(seconds=10),
    )
    assert store.put(failed)
    assert store.put(recovered)
    assert not store.put(recovered)

    snapshot = store.snapshot(
        as_of=AS_OF,
        requirements=(
            _requirement(
                CausalDomain.REGULATION_LEGISLATION,
                ("regulatory-feed",),
            ),
        ),
    )[0]

    assert snapshot.status == CoverageStatus.SOURCE_FAILED
    assert snapshot.latest_poll_refs == (failed.poll_id,)


def test_failed_latest_poll_retains_last_success_and_publication() -> None:
    store = _store()
    succeeded = build_source_poll_record(
        source_stream_id="binance-usdm-market",
        domain=CausalDomain.SPOT_DERIVATIVES,
        status=SourcePollStatus.CHANGED,
        started_at=AS_OF - timedelta(seconds=31),
        completed_at=AS_OF - timedelta(seconds=30),
        latest_publication_at=AS_OF - timedelta(seconds=31),
        observation_count=6,
    )
    failed = build_source_poll_record(
        source_stream_id="binance-usdm-market",
        domain=CausalDomain.SPOT_DERIVATIVES,
        status=SourcePollStatus.FAILED,
        started_at=AS_OF - timedelta(seconds=11),
        completed_at=AS_OF - timedelta(seconds=10),
        error_class="TimeoutError",
    )
    store.put(succeeded)
    store.put(failed)

    snapshot = store.snapshot(
        as_of=AS_OF,
        requirements=(
            _requirement(
                CausalDomain.SPOT_DERIVATIVES,
                ("binance-usdm-market",),
                publication_age=120,
            ),
        ),
    )[0]

    assert snapshot.status == CoverageStatus.SOURCE_FAILED
    assert snapshot.latest_success_at == succeeded.completed_at
    assert snapshot.latest_publication_at == succeeded.latest_publication_at
    assert snapshot.latest_poll_refs == (failed.poll_id,)
