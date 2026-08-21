import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool

from investment_manager.information.models import SourcePollStatus
from investment_manager.information.official.metrics import (
    FED_BROAD_DOLLAR_STREAM_ID,
    NYFED_RATES_STREAM_ID,
    NYFED_RRP_STREAM_ID,
    NYFED_SOMA_STREAM_ID,
    TGA_STREAM_ID,
    TREASURY_YIELD_STREAM_ID,
    parse_official_metric_document,
)
from investment_manager.information.official.source import OfficialMetricDocument
from investment_manager.information.tables import source_observations
from investment_manager.schema import create_schema
from investment_manager.state.metric_ingestion import (
    OFFICIAL_METRIC_STREAM_DOMAINS,
    OfficialMetricCollectorService,
    SqlOfficialMetricFactIngestor,
)
from investment_manager.state.official_ingestion import SourcePollAuditError
from investment_manager.state.tables import canonical_fact_revisions

OBSERVED_AT = datetime(2026, 8, 21, 20, tzinfo=UTC)


def _documents() -> dict[str, OfficialMetricDocument]:
    treasury_xml = b"""<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"
      xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices">
      <updated>2026-08-21T19:00:00Z</updated>
      <entry><content><m:properties><d:NEW_DATE>2026-08-20T00:00:00</d:NEW_DATE>
        <d:BC_2YEAR>4.19</d:BC_2YEAR><d:BC_10YEAR>4.69</d:BC_10YEAR>
        <d:BC_30YEAR>5.23</d:BC_30YEAR></m:properties></content></entry>
      <entry><content><m:properties><d:NEW_DATE>2026-08-21T00:00:00</d:NEW_DATE>
        <d:BC_2YEAR>4.24</d:BC_2YEAR><d:BC_10YEAR>4.74</d:BC_10YEAR>
        <d:BC_30YEAR>5.27</d:BC_30YEAR></m:properties></content></entry>
    </feed>"""
    dollar_xml = b"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
      xmlns:rss="http://purl.org/rss/1.0/"
      xmlns:dc="http://purl.org/dc/elements/1.1/"
      xmlns:cb="http://www.cbwiki.net/wiki/index.php/Specification_1.1">
      <rss:channel><dc:date>2026-08-21T12:00:00-04:00</dc:date></rss:channel>
      <rss:item><cb:observationPeriod>2026-08-13</cb:observationPeriod>
        <cb:value>119.1848</cb:value></rss:item>
      <rss:item><cb:observationPeriod>2026-08-14</cb:observationPeriod>
        <cb:value>118.9028</cb:value></rss:item>
    </rdf:RDF>"""
    payloads = {
        TGA_STREAM_ID: (
            "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/"
            "v1/accounting/dts/operating_cash_balance",
            json.dumps(
                {
                    "data": [
                        {
                            "record_date": "2026-08-18",
                            "account_type": "Treasury General Account (TGA) Closing Balance",
                            "open_today_bal": "936406",
                        },
                        {
                            "record_date": "2026-08-19",
                            "account_type": "Treasury General Account (TGA) Closing Balance",
                            "open_today_bal": "936406",
                        },
                        {
                            "record_date": "2026-08-20",
                            "account_type": "Treasury General Account (TGA) Closing Balance",
                            "open_today_bal": "935077",
                        },
                    ]
                }
            ).encode(),
            "application/json",
        ),
        TREASURY_YIELD_STREAM_ID: (
            "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml",
            treasury_xml,
            "application/xml",
        ),
        FED_BROAD_DOLLAR_STREAM_ID: (
            "https://www.federalreserve.gov/feeds/data/H10_H10_JRXWTFB_N.B.xml",
            dollar_xml,
            "application/xml",
        ),
        NYFED_RRP_STREAM_ID: (
            "https://markets.newyorkfed.org/api/rp/reverserepo/propositions/search.json",
            json.dumps(
                {
                    "repo": {
                        "operations": [
                            {
                                "operationDate": "2026-08-20",
                                "operationType": "Reverse Repo",
                                "totalAmtAccepted": "225000000",
                            },
                            {
                                "operationDate": "2026-08-21",
                                "operationType": "Reverse Repo",
                                "totalAmtAccepted": "200000000",
                            },
                        ]
                    }
                }
            ).encode(),
            "application/json",
        ),
        NYFED_SOMA_STREAM_ID: (
            "https://markets.newyorkfed.org/api/soma/summary.json",
            json.dumps(
                {
                    "soma": {
                        "summary": [
                            {"asOfDate": "2026-08-12", "total": "6364637243485"},
                            {"asOfDate": "2026-08-19", "total": "6368753087439"},
                        ]
                    }
                }
            ).encode(),
            "application/json",
        ),
        NYFED_RATES_STREAM_ID: (
            "https://markets.newyorkfed.org/api/rates/all/latest.json",
            json.dumps(
                {
                    "refRates": [
                        {
                            "type": "EFFR",
                            "effectiveDate": "2026-08-20",
                            "percentRate": "3.63",
                            "targetRateFrom": "3.50",
                            "targetRateTo": "3.75",
                        },
                        {
                            "type": "SOFR",
                            "effectiveDate": "2026-08-20",
                            "percentRate": "3.63",
                        },
                    ]
                }
            ).encode(),
            "application/json",
        ),
    }
    return {
        stream_id: OfficialMetricDocument(
            stream_id=stream_id,
            source_url=url,
            media_type=media_type,
            content=content,
        )
        for stream_id, (url, content, media_type) in payloads.items()
    }


def _engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_schema(engine)
    return engine


def test_all_fixed_first_party_metric_documents_parse_to_compact_snapshots() -> None:
    snapshots = []
    for document in _documents().values():
        snapshot = parse_official_metric_document(
            document.stream_id,
            document.content,
            source_url=document.source_url,
            media_type=document.media_type,
            observed_at=OBSERVED_AT,
        )
        snapshots.append(snapshot)
        assert snapshot.stream_id == document.stream_id
        assert snapshot.observation.source_published_at <= OBSERVED_AT
        assert tuple(item.name.value for item in snapshot.metrics) == tuple(
            sorted(item.name.value for item in snapshot.metrics)
        )
        serialized = snapshot.model_dump_json()
        assert len(serialized) < 2_000
        assert "E+" not in serialized

    assert len({item.fact_type for item in snapshots}) == 6
    tga = next(item for item in snapshots if item.stream_id == TGA_STREAM_ID)
    assert {item.name.value: item.value for item in tga.metrics}["tga_change_1d_usd_m"] == -1329


def test_metric_ingestion_is_idempotent_and_appends_only_semantic_revision() -> None:
    engine = _engine()
    ingestor = SqlOfficialMetricFactIngestor(
        engine,
        projection_version="official-metric-v1",
        affected_assets=("BTC", "ETH"),
    )
    document = _documents()[TGA_STREAM_ID]

    first = ingestor.ingest(document, observed_at=OBSERVED_AT)
    duplicate = ingestor.ingest(document, observed_at=OBSERVED_AT + timedelta(minutes=1))
    changed = replace(document, content=document.content.replace(b"935077", b"930000"))
    revised = ingestor.ingest(changed, observed_at=OBSERVED_AT + timedelta(minutes=2))

    assert first.new_fact_revision is not None
    assert duplicate.new_fact_revision is None
    assert revised.new_fact_revision is not None
    assert revised.new_fact_revision.previous_revision_id == first.new_fact_revision.revision_id
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(source_observations)) == 2
        assert connection.scalar(select(func.count()).select_from(canonical_fact_revisions)) == 2


def test_metric_collector_isolates_one_stream_failure_and_audits_both_polls() -> None:
    engine = _engine()
    documents = _documents()
    polls = []

    class Source:
        stream_ids = tuple(sorted(OFFICIAL_METRIC_STREAM_DOMAINS))

        def fetch(self, stream_id, *, observed_at):
            if stream_id == FED_BROAD_DOLLAR_STREAM_ID:
                raise OSError("feed unavailable")
            return documents[stream_id]

    class Recorder:
        def put(self, poll):
            polls.append(poll)
            return True

    service = OfficialMetricCollectorService(
        source=Source(),
        ingestor=SqlOfficialMetricFactIngestor(
            engine,
            projection_version="official-metric-v1",
            affected_assets=("BTC", "ETH"),
        ),
        publish_recent=lambda as_of: None,
        fast_poll_seconds=300,
        slow_poll_seconds=21_600,
        poll_recorder=Recorder(),
        clock=lambda: OBSERVED_AT,
    )

    asyncio.run(service._poll(FED_BROAD_DOLLAR_STREAM_ID))
    asyncio.run(service._poll(TGA_STREAM_ID))

    assert [item.status for item in polls] == [
        SourcePollStatus.FAILED,
        SourcePollStatus.CHANGED,
    ]
    assert service.health.new_fact_revision_count == 1
    assert service.health.last_error_by_stream == {FED_BROAD_DOLLAR_STREAM_ID: "OSError"}


def test_metric_collector_fails_closed_if_poll_audit_is_not_durable() -> None:
    class Source:
        stream_ids = tuple(sorted(OFFICIAL_METRIC_STREAM_DOMAINS))

        def fetch(self, stream_id, *, observed_at):
            return _documents()[stream_id]

    class BrokenRecorder:
        def put(self, poll):
            raise OSError("coverage ledger unavailable")

    service = OfficialMetricCollectorService(
        source=Source(),
        ingestor=SqlOfficialMetricFactIngestor(
            _engine(),
            projection_version="official-metric-v1",
            affected_assets=("BTC", "ETH"),
        ),
        publish_recent=lambda as_of: None,
        fast_poll_seconds=300,
        slow_poll_seconds=21_600,
        poll_recorder=BrokenRecorder(),
        clock=lambda: OBSERVED_AT,
    )

    with pytest.raises(SourcePollAuditError, match="无法持久化"):
        asyncio.run(service._poll(TGA_STREAM_ID))
