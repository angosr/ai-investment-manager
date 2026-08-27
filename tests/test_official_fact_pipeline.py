import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool

from investment_manager.information.official.economic_calendar import (
    BEA_CALENDAR_STREAM_ID,
    BLS_CALENDAR_STREAM_ID,
)
from investment_manager.information.official.repository import SqlFedOfficialInformationIngestor
from investment_manager.information.official.source import FedPolicyDocument
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
    MacroOfficialCollectorService,
    SourcePollAuditError,
    SqlEconomicReleaseCalendarFactIngestor,
    SqlFedFactIngestor,
)
from investment_manager.state.repository import SqlFactStateStore
from investment_manager.state.tables import canonical_fact_revisions

OBSERVED_AT = datetime(2026, 8, 20, 12, tzinfo=UTC)
POLICY = OfficialFactProjectionPolicy(
    version="fed-fact-v1",
    affected_assets=("BTC", "ETH", "PAXG"),
)


class EmptyEconomicCalendarSource:
    stream_ids = (BEA_CALENDAR_STREAM_ID, BLS_CALENDAR_STREAM_ID)

    def fetch(self, stream_id):
        assert stream_id in self.stream_ids
        return None


def _calendar(date_text: str) -> str:
    return f"""
    <h4>2026 FOMC Meetings</h4><div class="row fomc-meeting">
      <div class="fomc-meeting__month"><strong>September</strong></div>
      <div class="fomc-meeting__date">{date_text}</div>
    </div>
    """


def _public_calendar(*, include_chair: bool = True) -> str:
    events = (
        [
            {
                "description": "Keynote Remarks",
                "location": "At the Jackson Hole Economic Policy Symposium",
                "title": "Speech - Chairman Kevin Warsh",
                "time": "10:00 a.m.",
                "month": "2026-08",
                "days": "28",
                "type": "Speeches",
            }
        ]
        if include_chair
        else []
    )
    events.append(
        {
            "title": "Speech - Governor Example",
            "time": "1:00 p.m.",
            "month": "2026-09",
            "days": "1",
            "type": "Speeches",
        }
    )
    return json.dumps({"events": [*events, {}]})


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
        assert connection.scalar(select(func.count()).select_from(canonical_fact_revisions)) == 2


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


def test_unchanged_source_replay_does_not_reproject_existing_fact() -> None:
    engine = _engine()
    xml = """<rss><channel><item>
      <title>Federal Reserve announces a public information collection</title>
      <link>https://www.federalreserve.gov/newsevents/pressreleases/monetary.htm</link>
      <guid>fed-release-policy-replay</guid><description>Policy statement.</description>
      <pubDate>Wed, 19 Aug 2026 18:00:00 GMT</pubDate>
    </item></channel></rss>"""
    first = SqlFedFactIngestor(engine, POLICY).ingest_monetary_rss(
        xml,
        observed_at=OBSERVED_AT,
    )
    revised_policy = POLICY.model_copy(update={"version": "fed-fact-v2"})

    replayed = SqlFedFactIngestor(engine, revised_policy).ingest_monetary_rss(
        xml,
        observed_at=OBSERVED_AT + timedelta(minutes=1),
    )

    assert replayed.records[0].inserted is False
    assert replayed.new_fact_revisions == ()
    assert (
        SqlFactStateStore(engine).latest_fact(first.new_fact_revisions[0].fact_id)
        == first.new_fact_revisions[0]
    )
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(canonical_fact_revisions)) == 1


def test_non_policy_fed_rss_projects_canonical_release_fact() -> None:
    engine = _engine()
    pipeline = SqlFedFactIngestor(engine, POLICY)
    xml = """<rss><channel><item>
      <title>Federal Reserve announces a public information collection</title>
      <link>https://www.federalreserve.gov/newsevents/pressreleases/monetary.htm</link>
      <guid>fed-release-1</guid><description>Policy statement.</description>
      <pubDate>Wed, 19 Aug 2026 18:00:00 GMT</pubDate>
    </item></channel></rss>"""

    result = pipeline.ingest_monetary_rss(xml, observed_at=OBSERVED_AT)

    assert len(result.new_fact_revisions) == 1
    assert result.new_fact_revisions[0].fact_type == "FED_MONETARY_RELEASE"


def test_fed_policy_document_is_first_canonical_policy_fact_and_rss_cannot_regress_it() -> None:
    engine = _engine()
    pipeline = SqlFedFactIngestor(engine, POLICY)
    xml = """<rss><channel><item>
      <title>Minutes of the Federal Open Market Committee</title>
      <link>https://www.federalreserve.gov/monetarypolicy/fomcminutes20260729.htm</link>
      <guid>fed-minutes-1</guid><description></description>
      <pubDate>Wed, 19 Aug 2026 18:00:00 GMT</pubDate>
    </item></channel></rss>"""
    first = pipeline.ingest_monetary_rss(xml, observed_at=OBSERVED_AT)
    source_record = first.records[0].record
    html = """<html><main>
      <p>The Committee decided to maintain the target range for the federal funds rate.</p>
      <p>Inflation remained elevated and labor market conditions remained stable.</p>
      <p>Many participants assessed that policy tightening would likely be necessary.</p>
      <p>The market was fully pricing in a 25 basis point hike by September.</p>
    </main></html>"""

    enriched = pipeline.ingest_monetary_document(
        source_record,
        html,
        document_url=source_record.source_url,
        observed_at=OBSERVED_AT + timedelta(minutes=1),
    )

    assert first.new_fact_revisions == ()
    assert len(enriched.new_fact_revisions) == 1
    policy_fact = enriched.new_fact_revisions[0]
    assert policy_fact.previous_revision_id is None
    assert "action=" in policy_fact.claim
    assert "expectations=" in policy_fact.claim
    assert "path=" in policy_fact.claim

    replayed_rss = pipeline.ingest_monetary_rss(
        xml,
        observed_at=OBSERVED_AT + timedelta(minutes=2),
    )

    assert replayed_rss.new_fact_revisions == ()
    assert SqlFactStateStore(engine).latest_fact(policy_fact.fact_id) == policy_fact
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(canonical_fact_revisions)) == 1


def test_official_collector_polls_both_first_party_feeds_and_publishes() -> None:
    engine = _engine()
    stop = asyncio.Event()
    published_at: list[datetime] = []

    class Source:
        def fetch_calendar(self):
            return _calendar("15-16")

        def fetch_public_calendar(self):
            return _public_calendar()

        def fetch_monetary_rss(self):
            return """<rss><channel><item>
              <title>Federal Reserve issues FOMC statement</title>
              <link>https://www.federalreserve.gov/newsevents/pressreleases/monetary.htm</link>
              <guid>fed-release-service-1</guid>
              <description>Policy statement.</description>
              <pubDate>Wed, 19 Aug 2026 18:00:00 GMT</pubDate>
            </item></channel></rss>"""

        def fetch_monetary_document(self, url):
            assert url.endswith("/newsevents/pressreleases/monetary.htm")
            return FedPolicyDocument(
                source_url=url,
                content="""<html><main>
                  <p>The Committee decided to maintain the target range for the
                  federal funds rate.</p>
                  <p>Inflation remained elevated.</p>
                  <p>The market was fully pricing in a later policy increase.</p>
                </main></html>""",
            )

    def publish_recent(as_of):
        published_at.append(as_of)
        stop.set()

    service = MacroOfficialCollectorService(
        source=Source(),
        ingestor=SqlFedFactIngestor(engine, POLICY),
        economic_calendar_source=EmptyEconomicCalendarSource(),
        economic_calendar_ingestor=SqlEconomicReleaseCalendarFactIngestor(
            engine,
            POLICY,
        ),
        publish_recent=publish_recent,
        monetary_poll_seconds=15,
        calendar_poll_seconds=21_600,
        economic_calendar_poll_seconds=21_600,
        clock=lambda: OBSERVED_AT,
    )

    asyncio.run(service.run(stop))

    assert service.health.calendar_poll_count == 1
    assert service.health.public_calendar_poll_count == 1
    assert service.health.economic_calendar_poll_count == 2
    assert service.health.monetary_poll_count == 1
    assert service.health.new_fact_revision_count == 3
    assert service.health.publication_count == 1
    assert published_at == [OBSERVED_AT]


def test_official_collector_fails_closed_when_poll_fact_is_not_durable() -> None:
    engine = _engine()

    class Source:
        def fetch_calendar(self):
            return _calendar("15-16")

        def fetch_public_calendar(self):
            return _public_calendar()

        def fetch_monetary_rss(self):
            return None

    class BrokenRecorder:
        def put(self, poll):
            raise OSError("coverage ledger unavailable")

    service = MacroOfficialCollectorService(
        source=Source(),
        ingestor=SqlFedFactIngestor(engine, POLICY),
        economic_calendar_source=EmptyEconomicCalendarSource(),
        economic_calendar_ingestor=SqlEconomicReleaseCalendarFactIngestor(
            engine,
            POLICY,
        ),
        publish_recent=lambda as_of: None,
        monetary_poll_seconds=15,
        calendar_poll_seconds=21_600,
        economic_calendar_poll_seconds=21_600,
        poll_recorder=BrokenRecorder(),
        clock=lambda: OBSERVED_AT,
    )

    with pytest.raises(SourcePollAuditError, match="无法持久化"):
        asyncio.run(service._poll("calendar"))

    assert service.health.last_calendar_success_at is None
    assert service.health.calendar_error_class == "SourcePollAuditError"


def test_new_official_fact_revision_reaches_durable_trigger_outbox(app_config) -> None:
    engine = _engine()
    triggers = SqlTriggerRepository(engine, app_config.trigger)
    ensure_trigger_plans(
        repository=triggers,
        symbols=app_config.analysis_symbols,
        pipeline_id=app_config.pipeline.version,
        manifest_id="manifest-official-fact-test",
        heartbeat_seconds=app_config.trigger.heartbeat_minutes * 60,
        minimum_intelligence_review_priority=(
            app_config.trigger.minimum_intelligence_review_priority
        ),
        debounce_seconds=app_config.trigger.debounce_seconds,
        now=OBSERVED_AT,
    )
    fed = SqlFedFactIngestor(engine, POLICY)
    calendar = fed.ingest_calendar(
        _calendar("15-16"),
        observed_at=OBSERVED_AT,
    )
    chair = fed.ingest_public_calendar(
        _public_calendar(),
        observed_at=OBSERVED_AT,
        years=(2026,),
    )
    release = fed.ingest_monetary_rss(
        """<rss><channel><item>
          <title>Federal Reserve issues FOMC statement</title>
          <link>https://www.federalreserve.gov/newsevents/pressreleases/monetary.htm</link>
          <guid>fed-release-trigger-1</guid>
          <description>Policy statement.</description>
          <pubDate>Thu, 20 Aug 2026 11:59:00 GMT</pubDate>
        </item></channel></rss>""",
        observed_at=OBSERVED_AT,
    )
    fed.ingest_monetary_document(
        release.records[0].record,
        """<html><main>
          <p>The Committee decided to maintain the target range for the federal
          funds rate.</p>
          <p>Inflation remained elevated.</p>
        </main></html>""",
        document_url=release.records[0].record.source_url,
        observed_at=OBSERVED_AT + timedelta(microseconds=1),
    )
    publisher = CanonicalFactTriggerPublisher(
        facts=SqlFactStateStore(engine),
        triggers=triggers,
        mandate=app_config.assessment.mandate,
        delta_policy=app_config.decision_state.delta_policy,
        pipeline_id=app_config.pipeline.version,
        trigger_expiry_seconds=app_config.trigger.trigger_expiry_seconds,
        required_freshness_seconds=(
            app_config.decision_state.packet_policy.maximum_market_age_seconds
        ),
        analysis_owner_symbol=app_config.assessment.review_trigger_symbol,
    )

    publish_at = OBSERVED_AT + timedelta(seconds=1)
    publisher.publish_recent(publish_at)
    publisher.publish_recent(publish_at)
    with engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(analysis_trigger_events)
                .where(analysis_trigger_events.c.trigger_type == "CANONICAL_FACT_REVISED")
            )
            # Calendar facts own future wakeups; only the newly published
            # policy release spends an immediate event-driven review.
            == 1
        )
        assert (
            connection.scalar(
                select(func.count())
                .select_from(trigger_outbox)
                .where(trigger_outbox.c.message_kind == "TRIGGER_CREATED")
            )
            == 1
        )
    owner = app_config.assessment.review_trigger_symbol
    plan = triggers.plan_for_scope(symbol=owner, pipeline_id=app_config.pipeline.version)
    assert len(plan.scheduled_wakeups) == 2
    assert plan.scheduled_wakeups[0].evidence_ids == (chair.new_fact_revisions[0].revision_id,)
    assert plan.scheduled_wakeups[0].wake_at == datetime(
        2026,
        8,
        28,
        14,
        tzinfo=UTC,
    )
    assert plan.scheduled_wakeups[1].evidence_ids == (calendar.new_fact_revisions[0].revision_id,)
    assert plan.scheduled_wakeups[1].wake_at == datetime(
        2026,
        9,
        16,
        18,
        tzinfo=UTC,
    )
    for symbol in set(app_config.analysis_symbols) - {owner}:
        assert not triggers.plan_for_scope(
            symbol=symbol,
            pipeline_id=app_config.pipeline.version,
        ).scheduled_wakeups

    revised_at = OBSERVED_AT + timedelta(minutes=1)
    revised_calendar = fed.ingest_calendar(
        _calendar("16-17"),
        observed_at=revised_at,
    )
    publisher.publish_recent(revised_at)
    plan = triggers.plan_for_scope(symbol=owner, pipeline_id=app_config.pipeline.version)
    assert len(plan.scheduled_wakeups) == 2
    assert plan.scheduled_wakeups[1].evidence_ids == (
        revised_calendar.new_fact_revisions[0].revision_id,
    )
    assert plan.scheduled_wakeups[1].wake_at == datetime(
        2026,
        9,
        17,
        18,
        tzinfo=UTC,
    )

    cancelled_at = OBSERVED_AT + timedelta(minutes=2)
    cancelled = fed.ingest_public_calendar(
        _public_calendar(include_chair=False),
        observed_at=cancelled_at,
        years=(2026,),
    )
    assert cancelled.new_fact_revisions[0].status.value == "CANCELLED"
    publisher.publish_recent(cancelled_at)
    plan = triggers.plan_for_scope(symbol=owner, pipeline_id=app_config.pipeline.version)
    assert len(plan.scheduled_wakeups) == 1
    assert plan.scheduled_wakeups[0].evidence_ids == (
        revised_calendar.new_fact_revisions[0].revision_id,
    )
