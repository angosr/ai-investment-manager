from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool

from investment_manager.information.official.economic_calendar import (
    BEA_CALENDAR_STREAM_ID,
    BEA_CALENDAR_URL,
    BLS_CALENDAR_STREAM_ID,
    BLS_CALENDAR_URL,
    EconomicReleaseKind,
    parse_economic_release_calendar,
)
from investment_manager.information.official.source import (
    HttpEconomicReleaseCalendarSource,
    OfficialCalendarDocument,
)
from investment_manager.scheduling.application import ensure_trigger_plans
from investment_manager.scheduling.fact_triggers import CanonicalFactTriggerPublisher
from investment_manager.scheduling.repository import SqlTriggerRepository
from investment_manager.scheduling.tables import analysis_trigger_events
from investment_manager.schema import create_schema
from investment_manager.state.facts import OfficialFactProjectionPolicy
from investment_manager.state.official_ingestion import (
    SqlEconomicReleaseCalendarFactIngestor,
)
from investment_manager.state.repository import SqlFactStateStore

OBSERVED_AT = datetime(2026, 8, 26, 1, tzinfo=UTC)


def _calendar(*events: tuple[str, str, str], timezone: str | None = None) -> bytes:
    rows = ["BEGIN:VCALENDAR", "VERSION:2.0"]
    for uid, start, summary in events:
        rows.extend(
            (
                "BEGIN:VEVENT",
                f"UID:{uid}",
                (
                    f"DTSTART;TZID={timezone}:{start}"
                    if timezone is not None
                    else f"DTSTART:{start}"
                ),
                f"SUMMARY:{summary}",
                "END:VEVENT",
            )
        )
    rows.append("END:VCALENDAR")
    return "\r\n".join(rows).encode()


def _engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_schema(engine)
    return engine


def test_bls_parser_keeps_only_cpi_and_employment_and_resolves_dst() -> None:
    content = _calendar(
        ("jobs", "20260904T083000", "Employment Situation"),
        ("cpi", "20260911T083000", "Consumer Price Index"),
        ("jolts", "20260929T100000", "Job Openings and Labor Turnover Survey"),
        timezone="US-Eastern",
    )

    snapshot = parse_economic_release_calendar(
        content,
        source_url=BLS_CALENDAR_URL,
        observed_at=OBSERVED_AT,
    )

    assert snapshot.source_id == "bureau-of-labor-statistics"
    assert tuple(item.release_kind for item in snapshot.records) == (
        EconomicReleaseKind.US_EMPLOYMENT,
        EconomicReleaseKind.US_CPI,
    )
    assert snapshot.records[0].scheduled_at == datetime(2026, 9, 4, 12, 30, tzinfo=UTC)


def test_bea_parser_unfolds_titles_and_keeps_only_pce_and_national_gdp() -> None:
    content = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\nUID:gdp\r\n"
        b"DTSTART:20260930T123000Z\r\n"
        b"SUMMARY:GDP (Third Estimate)\\, Industries\\, Corporate Profits\\, State GDP\\\r\n"
        b" \\, and State Personal Income\\, 2nd Quarter 2026\r\nEND:VEVENT\r\n"
        b"BEGIN:VEVENT\r\nUID:pce\r\nDTSTART:20260930T123000Z\r\n"
        b"SUMMARY:Personal Income and Outlays\\, August 2026\r\nEND:VEVENT\r\n"
        b"BEGIN:VEVENT\r\nUID:county\r\nDTSTART:20261202T133000Z\r\n"
        b"SUMMARY:GDP by County and Personal Income by County\\, 2025\r\nEND:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )

    snapshot = parse_economic_release_calendar(
        content,
        source_url=BEA_CALENDAR_URL,
        observed_at=OBSERVED_AT,
    )

    assert tuple(item.release_kind for item in snapshot.records) == (
        EconomicReleaseKind.US_GDP,
        EconomicReleaseKind.US_PCE,
    )
    assert "State Personal Income" in snapshot.records[0].title


def test_http_source_is_pinned_and_reuses_validated_body_after_304() -> None:
    content = _calendar(
        ("pce", "20260930T123000Z", "Personal Income and Outlays\\, August 2026"),
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                content=content,
                headers={"etag": '"calendar-v1"'},
                request=request,
            )
        return httpx.Response(304, request=request)

    source = HttpEconomicReleaseCalendarSource(
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )
    first = source.fetch(BEA_CALENDAR_STREAM_ID)
    second = source.fetch(BEA_CALENDAR_STREAM_ID)

    assert isinstance(first, OfficialCalendarDocument)
    assert second == first
    assert str(requests[0].url) == BEA_CALENDAR_URL
    assert requests[1].headers["if-none-match"] == '"calendar-v1"'
    assert "+https://localhost/" in requests[0].headers["user-agent"]


def test_fact_ingestor_projects_only_next_event_of_each_kind_then_promotes_next() -> None:
    engine = _engine()
    ingestor = SqlEconomicReleaseCalendarFactIngestor(
        engine,
        OfficialFactProjectionPolicy(
            version="economic-calendar-fact-v1",
            affected_assets=("BTC", "ETH"),
        ),
    )
    content = _calendar(
        ("jobs-1", "20260904T083000", "Employment Situation"),
        ("jobs-2", "20261002T083000", "Employment Situation"),
        ("cpi-1", "20260911T083000", "Consumer Price Index"),
        ("cpi-2", "20261014T083000", "Consumer Price Index"),
        timezone="US-Eastern",
    )
    document = OfficialCalendarDocument(
        stream_id=BLS_CALENDAR_STREAM_ID,
        source_url=BLS_CALENDAR_URL,
        content=content,
    )

    first = ingestor.ingest(document, observed_at=OBSERVED_AT)
    assert len(first.records) == 4
    assert len(first.new_fact_revisions) == 2
    assert {item.event_time for item in first.new_fact_revisions} == {
        datetime(2026, 9, 4, 12, 30, tzinfo=UTC),
        datetime(2026, 9, 11, 12, 30, tzinfo=UTC),
    }

    promoted = ingestor.ingest(
        document,
        observed_at=datetime(2026, 9, 12, tzinfo=UTC),
    )
    assert len(promoted.records) == 2
    assert len(promoted.new_fact_revisions) == 2
    assert {item.event_time for item in promoted.new_fact_revisions} == {
        datetime(2026, 10, 2, 12, 30, tzinfo=UTC),
        datetime(2026, 10, 14, 12, 30, tzinfo=UTC),
    }
    assert len(SqlFactStateStore(engine).facts_as_of(as_of=datetime(2026, 9, 12, tzinfo=UTC))) == 4


def test_fact_ingestor_rejects_stream_url_misattribution() -> None:
    ingestor = SqlEconomicReleaseCalendarFactIngestor(
        _engine(),
        OfficialFactProjectionPolicy(version="economic-calendar-fact-v1"),
    )

    with pytest.raises(ValueError, match="stream 与固定来源 URL"):
        ingestor.ingest(
            OfficialCalendarDocument(
                stream_id=BLS_CALENDAR_STREAM_ID,
                source_url=BEA_CALENDAR_URL,
                content=_calendar(
                    ("pce", "20260930T123000Z", "Personal Income and Outlays\\, August 2026"),
                ),
            ),
            observed_at=OBSERVED_AT,
        )


def test_removed_future_event_becomes_a_durable_cancellation() -> None:
    engine = _engine()
    ingestor = SqlEconomicReleaseCalendarFactIngestor(
        engine,
        OfficialFactProjectionPolicy(
            version="economic-calendar-fact-v1",
            affected_assets=("BTC", "ETH"),
        ),
    )
    first_content = _calendar(
        ("jobs", "20260904T083000", "Employment Situation"),
        ("cpi", "20260911T083000", "Consumer Price Index"),
        timezone="US-Eastern",
    )
    ingestor.ingest(
        OfficialCalendarDocument(
            stream_id=BLS_CALENDAR_STREAM_ID,
            source_url=BLS_CALENDAR_URL,
            content=first_content,
        ),
        observed_at=OBSERVED_AT,
    )

    changed = ingestor.ingest(
        OfficialCalendarDocument(
            stream_id=BLS_CALENDAR_STREAM_ID,
            source_url=BLS_CALENDAR_URL,
            content=_calendar(
                ("cpi", "20260911T083000", "Consumer Price Index"),
                timezone="US-Eastern",
            ),
        ),
        observed_at=datetime(2026, 8, 26, 2, tzinfo=UTC),
    )

    assert len(changed.new_fact_revisions) == 1
    assert changed.new_fact_revisions[0].status.value == "CANCELLED"
    assert changed.new_fact_revisions[0].event_time == datetime(2026, 9, 4, 12, 30, tzinfo=UTC)


def test_calendar_sync_keeps_obligations_without_premature_ai_wakeups(
    app_config,
) -> None:
    engine = _engine()
    facts = SqlFactStateStore(engine)
    triggers = SqlTriggerRepository(engine, app_config.trigger)
    ensure_trigger_plans(
        repository=triggers,
        symbols=app_config.analysis_symbols,
        pipeline_id=app_config.pipeline.version,
        manifest_id="economic-calendar-test-manifest",
        heartbeat_seconds=app_config.trigger.heartbeat_minutes * 60,
        minimum_intelligence_review_priority=(
            app_config.trigger.minimum_intelligence_review_priority
        ),
        debounce_seconds=app_config.trigger.debounce_seconds,
        now=OBSERVED_AT,
    )
    ingestor = SqlEconomicReleaseCalendarFactIngestor(
        engine,
        app_config.decision_state.official_fact_policy,
    )
    ingestor.ingest(
        OfficialCalendarDocument(
            stream_id=BLS_CALENDAR_STREAM_ID,
            source_url=BLS_CALENDAR_URL,
            content=_calendar(
                ("jobs", "20260904T083000", "Employment Situation"),
                ("cpi", "20260911T083000", "Consumer Price Index"),
                timezone="US-Eastern",
            ),
        ),
        observed_at=OBSERVED_AT,
    )
    ingestor.ingest(
        OfficialCalendarDocument(
            stream_id=BEA_CALENDAR_STREAM_ID,
            source_url=BEA_CALENDAR_URL,
            content=_calendar(
                ("gdp", "20260826T123000Z", "GDP (Second Estimate)\\, Q2 2026"),
                ("pce", "20260826T123000Z", "Personal Income and Outlays\\, July 2026"),
            ),
        ),
        observed_at=OBSERVED_AT,
    )
    publisher = CanonicalFactTriggerPublisher(
        facts=facts,
        triggers=triggers,
        mandate=app_config.assessment.mandate,
        delta_policy=app_config.decision_state.delta_policy,
        pipeline_id=app_config.pipeline.version,
        trigger_expiry_seconds=app_config.trigger.trigger_expiry_seconds,
        required_freshness_seconds=app_config.risk.maximum_market_age_seconds,
    )

    publisher.publish_recent(OBSERVED_AT)

    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(analysis_trigger_events)) == 0
    for symbol in app_config.analysis_symbols:
        plan = triggers.plan_for_scope(
            symbol=symbol,
            pipeline_id=app_config.pipeline.version,
        )
        assert plan.scheduled_wakeups == ()
