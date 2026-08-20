import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool

from investment_manager.information.official_repository import SqlFedOfficialInformationIngestor
from investment_manager.information.tables import (
    raw_source_payloads,
    source_observations,
)
from investment_manager.scheduling.application import ensure_trigger_plans
from investment_manager.scheduling.fact_triggers import CanonicalFactTriggerPublisher
from investment_manager.scheduling.repository import SqlTriggerRepository
from investment_manager.scheduling.tables import analysis_trigger_events, trigger_outbox
from investment_manager.schema import create_schema
from investment_manager.state.facts import OfficialFactProjectionPolicy
from investment_manager.state.official_ingestion import (
    FedOfficialCollectorService,
    SqlFedFactIngestor,
)
from investment_manager.state.repository import SqlFactStateStore
from investment_manager.state.tables import canonical_fact_revisions

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
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
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


def test_official_collector_polls_both_first_party_feeds_and_publishes() -> None:
    engine = _engine()
    stop = asyncio.Event()
    published_at: list[datetime] = []

    class Source:
        def fetch_calendar(self):
            return _calendar("15-16")

        def fetch_monetary_rss(self):
            return """<rss><channel><item>
              <title>Federal Reserve issues FOMC statement</title>
              <link>https://www.federalreserve.gov/newsevents/pressreleases/monetary.htm</link>
              <guid>fed-release-service-1</guid>
              <description>Policy statement.</description>
              <pubDate>Wed, 19 Aug 2026 18:00:00 GMT</pubDate>
            </item></channel></rss>"""

    def publish_recent(as_of):
        published_at.append(as_of)
        stop.set()

    service = FedOfficialCollectorService(
        source=Source(),
        ingestor=SqlFedFactIngestor(engine, POLICY),
        publish_recent=publish_recent,
        monetary_poll_seconds=15,
        calendar_poll_seconds=21_600,
        clock=lambda: OBSERVED_AT,
    )

    asyncio.run(service.run(stop))

    assert service.health.calendar_poll_count == 1
    assert service.health.monetary_poll_count == 1
    assert service.health.new_fact_revision_count == 2
    assert service.health.publication_count == 1
    assert published_at == [OBSERVED_AT]


def test_new_official_fact_revision_reaches_durable_trigger_outbox(app_config) -> None:
    engine = _engine()
    triggers = SqlTriggerRepository(engine, app_config.trigger)
    ensure_trigger_plans(
        repository=triggers,
        symbols=app_config.market_data.symbols,
        pipeline_id=app_config.pipeline.version,
        manifest_id="manifest-official-fact-test",
        heartbeat_seconds=app_config.trigger.heartbeat_minutes * 60,
        high_impact_threshold=app_config.trigger.high_impact_threshold,
        debounce_seconds=app_config.trigger.debounce_seconds,
        now=OBSERVED_AT,
    )
    fed = SqlFedFactIngestor(engine, POLICY)
    calendar = fed.ingest_calendar(
        _calendar("15-16"),
        observed_at=OBSERVED_AT,
    )
    fed.ingest_monetary_rss(
        """<rss><channel><item>
          <title>Federal Reserve issues FOMC statement</title>
          <link>https://www.federalreserve.gov/newsevents/pressreleases/monetary.htm</link>
          <guid>fed-release-trigger-1</guid>
          <description>Policy statement.</description>
          <pubDate>Thu, 20 Aug 2026 11:59:00 GMT</pubDate>
        </item></channel></rss>""",
        observed_at=OBSERVED_AT,
    )
    publisher = CanonicalFactTriggerPublisher(
        facts=SqlFactStateStore(engine),
        triggers=triggers,
        mandate=app_config.assessment.mandate,
        delta_policy=app_config.decision_state.delta_policy,
        pipeline_id=app_config.pipeline.version,
        trigger_expiry_seconds=app_config.trigger.trigger_expiry_seconds,
        required_freshness_seconds=app_config.risk.maximum_market_age_seconds,
    )

    publisher.publish_recent(OBSERVED_AT)
    publisher.publish_recent(OBSERVED_AT)
    with engine.connect() as connection:
        assert connection.scalar(
            select(func.count())
            .select_from(analysis_trigger_events)
            .where(analysis_trigger_events.c.trigger_type == "CANONICAL_FACT_REVISED")
        ) == 4
        assert connection.scalar(
            select(func.count())
            .select_from(trigger_outbox)
            .where(trigger_outbox.c.message_kind == "TRIGGER_CREATED")
        ) == 4
    for symbol in app_config.market_data.symbols:
        plan = triggers.plan_for_scope(
            symbol=symbol,
            pipeline_id=app_config.pipeline.version,
        )
        assert len(plan.scheduled_wakeups) == 1
        assert plan.scheduled_wakeups[0].evidence_ids == (
            calendar.new_fact_revisions[0].revision_id,
        )
        assert plan.scheduled_wakeups[0].wake_at == datetime(
            2026,
            9,
            16,
            18,
            tzinfo=UTC,
        )

    revised_at = OBSERVED_AT + timedelta(minutes=1)
    revised_calendar = fed.ingest_calendar(
        _calendar("16-17"),
        observed_at=revised_at,
    )
    publisher.publish_recent(revised_at)
    for symbol in app_config.market_data.symbols:
        plan = triggers.plan_for_scope(
            symbol=symbol,
            pipeline_id=app_config.pipeline.version,
        )
        assert len(plan.scheduled_wakeups) == 1
        assert plan.scheduled_wakeups[0].evidence_ids == (
            revised_calendar.new_fact_revisions[0].revision_id,
        )
        assert plan.scheduled_wakeups[0].wake_at == datetime(
            2026,
            9,
            17,
            18,
            tzinfo=UTC,
        )
