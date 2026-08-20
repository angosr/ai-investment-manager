from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine

from investment_manager.information.official.records import parse_fomc_calendar
from investment_manager.information.official.repository import (
    SqlFedOfficialInformationIngestor,
    SqlOfficialInformationStore,
)
from investment_manager.information.tables import (
    market_calendar_event_revisions,
    raw_source_payloads,
    source_observations,
)
from investment_manager.schema import create_schema


def _calendar(date_text: str) -> str:
    return f"""
    <h4>2026 FOMC Meetings</h4><div class="row fomc-meeting">
      <div class="fomc-meeting__month"><strong>September</strong></div>
      <div class="fomc-meeting__date">{date_text}</div>
      <strong>Projection Materials</strong>
    </div>
    """


def _store() -> tuple[
    SqlFedOfficialInformationIngestor,
    SqlOfficialInformationStore,
    Engine,
]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    return (
        SqlFedOfficialInformationIngestor(engine),
        SqlOfficialInformationStore(engine),
        engine,
    )


def test_unchanged_poll_keeps_first_seen_observation_and_no_revision() -> None:
    ingestor, _, engine = _store()
    first_seen = datetime(2026, 8, 20, 12, tzinfo=UTC)
    repeated_at = datetime(2026, 8, 20, 12, 1, tzinfo=UTC)

    inserted = ingestor.ingest_calendar(
        _calendar("15-16*"), observed_at=first_seen
    )[0]
    duplicate = ingestor.ingest_calendar(
        _calendar("15-16*"), observed_at=repeated_at
    )[0]

    assert inserted.inserted is True
    assert inserted.calendar_revision is not None
    assert duplicate.inserted is False
    assert duplicate.record.observation.observation_id == (
        inserted.record.observation.observation_id
    )
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(raw_source_payloads)) == 1
        assert connection.scalar(select(func.count()).select_from(source_observations)) == 1
        assert (
            connection.scalar(select(func.count()).select_from(market_calendar_event_revisions))
            == 1
        )


def test_calendar_revision_and_reversion_are_both_point_in_time_visible() -> None:
    ingestor, store, engine = _store()
    first_at = datetime(2026, 8, 20, 12, tzinfo=UTC)
    second_at = datetime(2026, 8, 21, 12, tzinfo=UTC)
    reverted_at = datetime(2026, 8, 22, 12, tzinfo=UTC)

    first = ingestor.ingest_calendar(
        _calendar("15-16*"), observed_at=first_at
    )[0].calendar_revision
    second = ingestor.ingest_calendar(
        _calendar("16-17*"), observed_at=second_at
    )[0].calendar_revision
    reverted = ingestor.ingest_calendar(
        _calendar("15-16*"), observed_at=reverted_at
    )[0].calendar_revision

    assert first is not None and second is not None and reverted is not None
    assert second.previous_revision_id == first.revision_id
    assert reverted.previous_revision_id == second.revision_id
    assert reverted.content_hash == first.content_hash
    assert reverted.revision_id != first.revision_id
    assert store.calendar_as_of(as_of=first_at)[0] == first
    assert store.calendar_as_of(as_of=second_at)[0] == second
    assert store.calendar_as_of(as_of=reverted_at)[0] == reverted
    assert store.records_as_of(as_of=first_at)[0].observation.observation_id == (
        first.source_observation_id
    )
    assert store.records_as_of(as_of=second_at)[0].observation.observation_id == (
        second.source_observation_id
    )
    assert store.records_as_of(as_of=reverted_at)[0].observation.observation_id == (
        reverted.source_observation_id
    )
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(source_observations)) == 3
        assert (
            connection.scalar(select(func.count()).select_from(market_calendar_event_revisions))
            == 3
        )


def test_out_of_order_calendar_revision_fails_without_partial_observation() -> None:
    ingestor, _, engine = _store()
    later = datetime(2026, 8, 21, 12, tzinfo=UTC)
    earlier = datetime(2026, 8, 20, 12, tzinfo=UTC)
    ingestor.ingest_calendar(_calendar("16-17*"), observed_at=later)

    with pytest.raises(ValueError, match="严格递增"):
        ingestor.ingest_calendar(_calendar("15-16*"), observed_at=earlier)

    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(source_observations)) == 1


def test_rss_observation_is_immutable_without_creating_calendar_event() -> None:
    ingestor, store, engine = _store()
    observed_at = datetime(2026, 8, 20, 12, tzinfo=UTC)
    xml = """<rss><channel><item>
      <title>Federal Reserve issues FOMC statement</title>
      <link>https://www.federalreserve.gov/newsevents/pressreleases/monetary.htm</link>
      <guid>fed-release-1</guid><description>Statement</description>
      <pubDate>Wed, 19 Aug 2026 18:00:00 GMT</pubDate>
    </item></channel></rss>"""
    result = ingestor.ingest_monetary_rss(xml, observed_at=observed_at)[0]
    record = result.record

    assert result.inserted is True
    assert result.calendar_revision is None
    assert store.observation(record.observation.observation_id) == record
    with engine.connect() as connection:
        assert (
            connection.scalar(select(func.count()).select_from(market_calendar_event_revisions))
            == 0
        )


def test_official_record_cannot_be_stored_without_raw_evidence() -> None:
    _, store, _ = _store()
    observed_at = datetime(2026, 8, 20, 12, tzinfo=UTC)
    record = parse_fomc_calendar(
        _calendar("15-16*"),
        observed_at=observed_at,
    )[0]

    with pytest.raises(ValueError, match="原始来源 payload"):
        store.put(record)


def test_invalid_rss_is_retained_as_raw_evidence_before_parse_failure() -> None:
    ingestor, _, engine = _store()

    with pytest.raises(ValueError, match="XML 非法"):
        ingestor.ingest_monetary_rss("<rss>", observed_at=datetime.now(UTC))

    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(raw_source_payloads)) == 1
