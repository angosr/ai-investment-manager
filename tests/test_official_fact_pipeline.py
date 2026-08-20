from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, func, select

from investment_manager.fact_pipeline import OfficialFactProjectionPolicy
from investment_manager.fact_state_sql import SqlFactStateStore
from investment_manager.official_fact_pipeline import SqlFedFactIngestor
from investment_manager.official_information_sql import SqlFedOfficialInformationIngestor
from investment_manager.persistence import (
    canonical_fact_revisions,
    raw_source_payloads,
    source_observations,
)
from investment_manager.schema import create_schema

OBSERVED_AT = datetime(2026, 8, 20, 12, tzinfo=UTC)
POLICY = OfficialFactProjectionPolicy(
    version="fed-fact-v1",
    affected_assets=("BTC", "ETH"),
)


def _calendar(date_text: str) -> str:
    return f"""
    <h4>2026 FOMC Meetings</h4><div class="row fomc-meeting">
      <div class="fomc-meeting__month"><strong>September</strong></div>
      <div class="fomc-meeting__date">{date_text}</div>
    </div>
    """


def _engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    return engine


def test_fed_ingestion_projects_revision_once_and_preserves_lineage() -> None:
    engine = _engine()
    pipeline = SqlFedFactIngestor(engine, POLICY)
    facts = SqlFactStateStore(engine)

    first = pipeline.ingest_calendar(_calendar("15-16"), observed_at=OBSERVED_AT)
    duplicate = pipeline.ingest_calendar(
        _calendar("15-16"),
        observed_at=OBSERVED_AT + timedelta(minutes=1),
    )
    revised_at = OBSERVED_AT + timedelta(minutes=2)
    revised = pipeline.ingest_calendar(
        _calendar("16-17"),
        observed_at=revised_at,
    )

    assert len(first.new_fact_revisions) == 1
    assert duplicate.new_fact_revisions == ()
    assert duplicate.records[0].calendar_revision is not None
    assert len(revised.new_fact_revisions) == 1
    original_fact = first.new_fact_revisions[0]
    revised_fact = revised.new_fact_revisions[0]
    assert revised_fact.previous_revision_id == original_fact.revision_id
    assert facts.facts_as_of(as_of=OBSERVED_AT) == (original_fact,)
    assert facts.facts_as_of(as_of=revised_at) == (revised_fact,)
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(raw_source_payloads)) == 2
        assert connection.scalar(select(func.count()).select_from(source_observations)) == 2
        assert connection.scalar(
            select(func.count()).select_from(canonical_fact_revisions)
        ) == 2


def test_retry_repairs_official_record_to_fact_projection_gap() -> None:
    engine = _engine()
    official_only = SqlFedOfficialInformationIngestor(engine)
    official_only.ingest_calendar(_calendar("15-16"), observed_at=OBSERVED_AT)
    pipeline = SqlFedFactIngestor(engine, POLICY)

    recovered = pipeline.ingest_calendar(
        _calendar("15-16"),
        observed_at=OBSERVED_AT + timedelta(minutes=1),
    )

    assert recovered.records[0].inserted is False
    assert len(recovered.new_fact_revisions) == 1
    assert recovered.new_fact_revisions[0].observed_at == OBSERVED_AT


def test_fed_rss_projects_canonical_release_fact() -> None:
    engine = _engine()
    pipeline = SqlFedFactIngestor(engine, POLICY)
    xml = """<rss><channel><item>
      <title>Federal Reserve issues FOMC statement</title>
      <link>https://www.federalreserve.gov/newsevents/pressreleases/monetary.htm</link>
      <guid>fed-release-1</guid><description>Policy statement.</description>
      <pubDate>Wed, 19 Aug 2026 18:00:00 GMT</pubDate>
    </item></channel></rss>"""

    result = pipeline.ingest_monetary_rss(xml, observed_at=OBSERVED_AT)

    assert len(result.new_fact_revisions) == 1
    assert result.new_fact_revisions[0].fact_type == "FED_MONETARY_RELEASE"
