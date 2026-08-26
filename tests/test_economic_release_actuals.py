from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from investment_manager.information.official.economic_actual_source import (
    BEA_RELEASE_RSS_URL,
    BLS_API_URL,
    EconomicReleaseActualDocument,
    HttpEconomicReleaseActualSource,
    parse_economic_release_actual,
)
from investment_manager.information.official.economic_actuals import (
    EconomicReleaseActualRecord,
    EconomicReleaseMetric,
)
from investment_manager.information.official.economic_calendar import (
    BEA_CALENDAR_STREAM_ID,
    BEA_CALENDAR_URL,
    BLS_CALENDAR_URL,
    EconomicReleaseEventRecord,
    parse_economic_release_calendar,
)
from investment_manager.information.official.repository import (
    SqlStructuredInformationStore,
)
from investment_manager.information.official.source import OfficialCalendarDocument
from investment_manager.schema import create_schema
from investment_manager.state.economic_releases import (
    EconomicReleaseActualCollectorService,
    SqlEconomicReleaseActualFactIngestor,
)
from investment_manager.state.facts import OfficialFactProjectionPolicy
from investment_manager.state.models import FactRevisionStatus
from investment_manager.state.official_ingestion import (
    SqlEconomicReleaseCalendarFactIngestor,
)

OBSERVED_AT = datetime(2026, 8, 26, 10, tzinfo=UTC)
RELEASED_AT = datetime(2026, 8, 26, 12, 30, tzinfo=UTC)


def _engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_schema(engine)
    return engine


def _event(*, kind: str = "pce", scheduled_at: datetime = RELEASED_AT):
    if kind == "pce":
        source_url = BEA_CALENDAR_URL
        title = "Personal Income and Outlays\\, July 2026"
    elif kind == "gdp":
        source_url = BEA_CALENDAR_URL
        title = "GDP (Second Estimate) and Corporate Profits\\, 2nd Quarter 2026"
    elif kind == "cpi":
        source_url = BLS_CALENDAR_URL
        title = "Consumer Price Index"
    else:
        source_url = BLS_CALENDAR_URL
        title = "Employment Situation"
    content = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
        f"UID:{kind}-2026-08\r\nDTSTART:{scheduled_at:%Y%m%dT%H%M%SZ}\r\n"
        f"SUMMARY:{title}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    ).encode()
    return parse_economic_release_calendar(
        content,
        source_url=source_url,
        observed_at=OBSERVED_AT,
    ).records[0]


def _pce_html() -> bytes:
    return b"""
    <html><body><h1>Personal Income and Outlays, July 2026</h1>
    <p>From the same month one year ago, the PCE price index for July increased
    3.7 percent. Excluding food and energy, the PCE price index increased 3.3
    percent from one year ago.</p>
    <p>The personal saving rate was 3.0 percent.</p>
    <table>
      <tr><th>Measure</th><th>June</th><th>July</th></tr>
      <tr><td>Current-dollar personal income</td><td>0.2</td><td>0.4</td></tr>
      <tr><td>Current-dollar DPI</td><td>0.2</td><td>0.5</td></tr>
      <tr><td>Current-dollar PCE</td><td>0.3</td><td>0.2</td></tr>
      <tr><td>Real PCE</td><td>0.4</td><td>0.0</td></tr>
      <tr><td>PCE price index</td><td>-0.1</td><td>0.2</td></tr>
      <tr><td>PCE price index excluding food and energy</td><td>0.1</td><td>0.2</td></tr>
    </table></body></html>
    """


def _gdp_html() -> bytes:
    return b"""
    <html><body><h1>GDP (Second Estimate) and Corporate Profits, 2nd Quarter 2026</h1>
    <p>Real gross domestic product (GDP) increased at an annual rate of 1.5
    percent in the second quarter of 2026. Real GDP increased at the same rate
    as in the advance estimate.</p></body></html>
    """


def _bls_rows(*, latest_month: int = 7, base: float = 100.0):
    rows = []
    year = 2026
    month = latest_month
    for index in range(13):
        rows.append(
            {
                "year": str(year),
                "period": f"M{month:02d}",
                "periodName": datetime(year, month, 1).strftime("%B"),
                "value": str(base - index),
            }
        )
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return rows


def _bls_payload(series: dict[str, list[dict]]) -> bytes:
    return json.dumps(
        {
            "status": "REQUEST_SUCCEEDED",
            "Results": {
                "series": [
                    {"seriesID": series_id, "data": rows} for series_id, rows in series.items()
                ]
            },
        }
    ).encode()


def test_bea_source_uses_pinned_rss_and_parses_current_official_values() -> None:
    event = _event()
    release_url = "https://www.bea.gov/news/2026/personal-income-and-outlays-july-2026"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if str(request.url) == BEA_RELEASE_RSS_URL:
            return httpx.Response(
                200,
                content=(
                    "<rss><channel><item><title>Personal Income and Outlays, "
                    f"July 2026</title><link>{release_url}</link></item></channel></rss>"
                ).encode(),
                request=request,
            )
        return httpx.Response(200, content=_pce_html(), request=request)

    source = HttpEconomicReleaseActualSource(
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )
    document = source.fetch(event, observed_at=RELEASED_AT + timedelta(minutes=1))
    assert document is not None
    record = parse_economic_release_actual(
        event,
        document,
        observed_at=RELEASED_AT + timedelta(minutes=1),
    )

    assert record is not None
    values = {item.name: item.value for item in record.values}
    assert values[EconomicReleaseMetric.PCE_PRICE_MOM_PCT] == Decimal("0.2")
    assert values[EconomicReleaseMetric.PCE_PRICE_YOY_PCT] == Decimal("3.7")
    assert values[EconomicReleaseMetric.CORE_PCE_PRICE_YOY_PCT] == Decimal("3.3")
    assert "application/xml" in requests[0].headers["accept"]
    assert [str(item.url) for item in requests] == [BEA_RELEASE_RSS_URL, release_url]


def test_bea_gdp_parser_preserves_current_and_prior_vintage() -> None:
    event = _event(kind="gdp")
    record = parse_economic_release_actual(
        event,
        EconomicReleaseActualDocument(
            source_url="https://www.bea.gov/news/2026/gdp-second-estimate",
            media_type="text/html",
            content=_gdp_html(),
        ),
        observed_at=RELEASED_AT + timedelta(minutes=1),
    )

    assert record is not None
    values = {item.name: item.value for item in record.values}
    assert record.period == "second quarter of 2026"
    assert values[EconomicReleaseMetric.REAL_GDP_QOQ_ANNUALIZED_PCT] == Decimal("1.5")
    assert values[EconomicReleaseMetric.PRIOR_GDP_VINTAGE_QOQ_ANNUALIZED_PCT] == Decimal("1.5")


def test_bls_parser_rejects_previous_release_until_expected_month_is_visible() -> None:
    event = _event(kind="cpi", scheduled_at=datetime(2026, 8, 12, 12, 30, tzinfo=UTC))
    current = _bls_payload(
        {
            "CUSR0000SA0": _bls_rows(base=310),
            "CUSR0000SA0L1E": _bls_rows(base=320),
        }
    )
    stale = _bls_payload(
        {
            "CUSR0000SA0": _bls_rows(latest_month=6, base=310),
            "CUSR0000SA0L1E": _bls_rows(latest_month=6, base=320),
        }
    )
    source_url = BLS_API_URL

    assert (
        parse_economic_release_actual(
            event,
            EconomicReleaseActualDocument(
                source_url=source_url,
                media_type="application/json",
                content=stale,
            ),
            observed_at=event.scheduled_at + timedelta(minutes=1),
        )
        is None
    )
    record = parse_economic_release_actual(
        event,
        EconomicReleaseActualDocument(
            source_url=source_url,
            media_type="application/json",
            content=current,
        ),
        observed_at=event.scheduled_at + timedelta(minutes=1),
    )
    assert record is not None
    assert record.period == "July 2026"


def test_bls_employment_source_fetches_required_first_party_series() -> None:
    event = _event(
        kind="employment",
        scheduled_at=datetime(2026, 9, 4, 12, 30, tzinfo=UTC),
    )
    payload = _bls_payload(
        {
            "CES0000000001": _bls_rows(latest_month=8, base=160_000),
            "LNS14000000": _bls_rows(latest_month=8, base=4.5),
            "CES0500000003": _bls_rows(latest_month=8, base=40),
        }
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=payload, request=request)

    source = HttpEconomicReleaseActualSource(
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )
    document = source.fetch(
        event,
        observed_at=event.scheduled_at + timedelta(minutes=1),
    )
    assert document is not None
    record = parse_economic_release_actual(
        event,
        document,
        observed_at=event.scheduled_at + timedelta(minutes=1),
    )

    assert record is not None
    values = {item.name: item.value for item in record.values}
    assert record.period == "August 2026"
    assert values[EconomicReleaseMetric.NONFARM_PAYROLL_CHANGE_THOUSANDS] == 1
    assert values[EconomicReleaseMetric.UNEMPLOYMENT_RATE_PCT] == Decimal("4.5")
    assert str(requests[0].url) == BLS_API_URL
    assert set(json.loads(requests[0].content)["seriesid"]) == {
        "CES0000000001",
        "LNS14000000",
        "CES0500000003",
    }


class _ChangingSource:
    def __init__(self, document: EconomicReleaseActualDocument | None) -> None:
        self.document = document
        self.fetch_count = 0

    def fetch(self, _event, *, observed_at):
        self.fetch_count += 1
        return self.document


def _stored_event(engine) -> EconomicReleaseEventRecord:
    event = _event()
    result = SqlEconomicReleaseCalendarFactIngestor(
        engine,
        OfficialFactProjectionPolicy(version="economic-release-test-v1"),
    ).ingest(
        OfficialCalendarDocument(
            stream_id=BEA_CALENDAR_STREAM_ID,
            source_url=BEA_CALENDAR_URL,
            content=(
                b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
                b"UID:pce-2026-08\r\nDTSTART:20260826T123000Z\r\n"
                b"SUMMARY:Personal Income and Outlays\\, July 2026\r\n"
                b"END:VEVENT\r\nEND:VCALENDAR\r\n"
            ),
        ),
        observed_at=OBSERVED_AT,
    )
    record = result.records[0].record
    assert isinstance(record, EconomicReleaseEventRecord)
    assert record.observation.source_record_id == event.observation.source_record_id
    return record


def test_collector_freezes_available_actual_once() -> None:
    engine = _engine()
    event = _stored_event(engine)
    now = [event.scheduled_at + timedelta(minutes=1)]
    source = _ChangingSource(
        EconomicReleaseActualDocument(
            source_url="https://www.bea.gov/news/2026/personal-income-and-outlays-july-2026",
            media_type="text/html",
            content=_pce_html(),
        )
    )
    ingestor = SqlEconomicReleaseActualFactIngestor(
        engine,
        policy=OfficialFactProjectionPolicy(version="economic-release-test-v1"),
    )
    service = EconomicReleaseActualCollectorService(
        source=source,
        ingestor=ingestor,
        records=SqlStructuredInformationStore(engine),
        publish_recent=lambda _as_of: None,
        poll_seconds=15,
        deadline_seconds=900,
        recovery_lookback_seconds=14_400,
        clock=lambda: now[0],
    )

    asyncio.run(service._poll_due())
    asyncio.run(service._poll_due())

    resolution = ingestor.latest_resolution(event)
    assert resolution is not None
    assert resolution.status == FactRevisionStatus.ACTIVE
    assert source.fetch_count == 1
    assert service.health.available_count == 1
    assert any(
        isinstance(item, EconomicReleaseActualRecord)
        for item in SqlStructuredInformationStore(engine).records_as_of(as_of=now[0])
    )


def test_unavailable_terminal_is_auditable_and_allows_bounded_late_retry() -> None:
    engine = _engine()
    event = _stored_event(engine)
    now = [event.scheduled_at + timedelta(minutes=16)]
    source = _ChangingSource(None)
    ingestor = SqlEconomicReleaseActualFactIngestor(
        engine,
        policy=OfficialFactProjectionPolicy(version="economic-release-test-v1"),
    )
    service = EconomicReleaseActualCollectorService(
        source=source,
        ingestor=ingestor,
        records=SqlStructuredInformationStore(engine),
        publish_recent=lambda _as_of: None,
        poll_seconds=15,
        deadline_seconds=900,
        recovery_lookback_seconds=14_400,
        clock=lambda: now[0],
    )

    asyncio.run(service._poll_due())
    resolution = ingestor.latest_resolution(event)
    assert resolution is not None
    assert resolution.status == FactRevisionStatus.UNAVAILABLE

    now[0] += timedelta(minutes=1)
    asyncio.run(service._poll_due())
    assert source.fetch_count == 1

    source.document = EconomicReleaseActualDocument(
        source_url="https://www.bea.gov/news/2026/personal-income-and-outlays-july-2026",
        media_type="text/html",
        content=_pce_html(),
    )
    now[0] += timedelta(minutes=15)
    asyncio.run(service._poll_due())

    resolution = ingestor.latest_resolution(event)
    assert resolution is not None
    assert resolution.status == FactRevisionStatus.ACTIVE
    assert resolution.previous_revision_id is not None
    assert source.fetch_count == 2
