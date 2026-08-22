import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool

from investment_manager.information.models import SourcePollStatus
from investment_manager.information.official.metrics import (
    ARKB_HOLDINGS_STREAM_ID,
    BITB_HOLDINGS_STREAM_ID,
    FED_BROAD_DOLLAR_STREAM_ID,
    IBIT_HOLDINGS_STREAM_ID,
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
from investment_manager.state.facts import OfficialFactProjectionPolicy
from investment_manager.state.metric_ingestion import (
    OFFICIAL_METRIC_STREAM_DOMAINS,
    OfficialMetricCollectorService,
    SqlOfficialMetricFactIngestor,
)
from investment_manager.state.models import FactDecisionMateriality
from investment_manager.state.official_ingestion import SourcePollAuditError
from investment_manager.state.tables import canonical_fact_revisions

OBSERVED_AT = datetime(2026, 8, 21, 20, tzinfo=UTC)
METRIC_POLICY = OfficialFactProjectionPolicy(
    version="official-metric-v1",
    affected_assets=("BTC", "ETH"),
)


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
        IBIT_HOLDINGS_STREAM_ID: (
            "https://www.ishares.com/us/products/333011/"
            "ishares-bitcoin-trust-etf/latest-holdings.csv",
            (
                b"iShares Bitcoin Trust ETF\n"
                b'Fund Holdings as of,"Aug 20, 2026"\n'
                b'Inception Date,"Jan 05, 2024"\n'
                b'Shares Outstanding,"1,333,840,000.00"\n'
                b'Stock,"-"\nBond,"-"\nCash,"-"\nOther,"-"\n\n'
                b"Ticker,Name,Sector,Asset Class,Market Value,Weight (%),"
                b"Notional Value,Quantity,Market Currency,Accrual Date\n"
                b'"BTC","BITCOIN","-","Alternative","55,223,236,415.06",'
                b'"100.00","55,223,236,415.06","762,287.03650","BTC","-"\n'
                b'"USD","USD CASH","-","Cash","13,366.70","0.00",'
                b'"13,366.70","13,366.70000","USD","-"\n'
            ),
            "text/csv",
        ),
        ARKB_HOLDINGS_STREAM_ID: (
            "https://assets.ark-funds.com/fund-documents/funds-etf-csv/"
            "ARK_21SHARES_BITCOIN_ETF_ARKB_HOLDINGS.csv",
            (
                b"date,fund,company,ticker,cusip,shares,market value ($),weight (%)\n"
                b'08/21/2026,ARKB,BITCOIN,BTC,-,"35,026.00000000",'
                b'"$2,542,000,000.00",100.00\n'
            ),
            "text/csv",
        ),
        BITB_HOLDINGS_STREAM_ID: (
            "https://bitbetf.com/",
            (
                "<html><body><script id=\"__NEXT_DATA__\" type=\"application/json\">"
                + json.dumps(
                    {
                        "props": {
                            "pageProps": {
                                "fundData": {
                                    "data": {
                                        "updatedAt": "2026-08-21T18:00:00Z",
                                        "fundDetails": {
                                            "asOfDate": "2026-08-21",
                                            "netAssets": "2920000000.25",
                                            "sharesOutstanding": "69780000",
                                        },
                                        "holdings": {
                                            "asOfDate": "2026-08-21",
                                            "basket": [
                                                {
                                                    "companyName": "BITCOIN",
                                                    "shares": "37871.96676424",
                                                    "marketValue": "2919999000.12",
                                                }
                                            ],
                                        },
                                    }
                                }
                            }
                        }
                    }
                )
                + "</script></body></html>"
            ).encode(),
            "text/html",
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

    assert len({item.fact_type for item in snapshots}) == 9
    tga = next(item for item in snapshots if item.stream_id == TGA_STREAM_ID)
    assert {item.name.value: item.value for item in tga.metrics}["tga_change_1d_usd_m"] == -1329
    ibit = next(item for item in snapshots if item.stream_id == IBIT_HOLDINGS_STREAM_ID)
    assert {item.name.value: item.value for item in ibit.metrics}["ibit_btc_holdings"] == Decimal(
        "762287.0365"
    )
    arkb = next(item for item in snapshots if item.stream_id == ARKB_HOLDINGS_STREAM_ID)
    assert {item.name.value: item.value for item in arkb.metrics}["btc_etp_holdings"] == Decimal(
        "35026"
    )
    bitb = next(item for item in snapshots if item.stream_id == BITB_HOLDINGS_STREAM_ID)
    bitb_values = {item.name.value: item.value for item in bitb.metrics}
    assert bitb_values["btc_etp_holdings"] == Decimal("37871.96676424")
    assert bitb_values["btc_etp_shares_outstanding"] == Decimal("69780000")


def test_metric_ingestion_is_idempotent_and_appends_only_semantic_revision() -> None:
    engine = _engine()
    ingestor = SqlOfficialMetricFactIngestor(
        engine,
        policy=METRIC_POLICY,
    )
    document = _documents()[TGA_STREAM_ID]

    first = ingestor.ingest(document, observed_at=OBSERVED_AT)
    duplicate = ingestor.ingest(document, observed_at=OBSERVED_AT + timedelta(minutes=1))
    changed = replace(document, content=document.content.replace(b"935077", b"930000"))
    revised = ingestor.ingest(changed, observed_at=OBSERVED_AT + timedelta(minutes=2))

    assert first.new_fact_revision is not None
    assert (
        first.new_fact_revision.decision_materiality
        == FactDecisionMateriality.BACKGROUND
    )
    assert duplicate.new_fact_revision is None
    assert revised.new_fact_revision is not None
    assert revised.new_fact_revision.previous_revision_id == first.new_fact_revision.revision_id
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(source_observations)) == 2
        assert connection.scalar(select(func.count()).select_from(canonical_fact_revisions)) == 2


def test_empirically_extreme_metric_change_becomes_material_candidate() -> None:
    start = OBSERVED_AT.date() - timedelta(days=39)
    rows = [
        {
            "record_date": (start + timedelta(days=index)).isoformat(),
            "account_type": "Treasury General Account (TGA) Closing Balance",
            "open_today_bal": str(900_000 + index * 10),
        }
        for index in range(39)
    ]
    rows.append(
        {
            "record_date": OBSERVED_AT.date().isoformat(),
            "account_type": "Treasury General Account (TGA) Closing Balance",
            "open_today_bal": "950000",
        }
    )
    base = _documents()[TGA_STREAM_ID]
    document = replace(base, content=json.dumps({"data": rows}).encode())

    result = SqlOfficialMetricFactIngestor(
        _engine(),
        policy=METRIC_POLICY,
    ).ingest(document, observed_at=OBSERVED_AT)

    assert result.record is not None
    assert result.record.record.change_context is not None
    assert result.record.record.change_context.absolute_change_percentile == 1
    assert result.record.record.change_context.sample_size >= 30
    assert result.new_fact_revision is not None
    assert (
        result.new_fact_revision.decision_materiality
        == FactDecisionMateriality.CANDIDATE
    )


def test_latest_only_issuer_feed_builds_honest_change_after_second_day() -> None:
    engine = _engine()
    ingestor = SqlOfficialMetricFactIngestor(engine, policy=METRIC_POLICY)
    first_document = _documents()[IBIT_HOLDINGS_STREAM_ID]
    first = ingestor.ingest(first_document, observed_at=OBSERVED_AT)
    second_document = replace(
        first_document,
        content=(
            first_document.content
            .replace(b"Aug 20, 2026", b"Aug 21, 2026")
            .replace(b"1,333,840,000.00", b"1,343,840,000.00")
            .replace(b"762,287.03650", b"768,287.03650")
        ),
    )

    second = ingestor.ingest(
        second_document,
        observed_at=OBSERVED_AT + timedelta(days=1),
    )

    assert first.new_fact_revision is not None
    assert second.record is not None
    values = {item.name.value: item.value for item in second.record.record.metrics}
    assert values["ibit_btc_holdings_change_1d"] == 6_000
    assert values["ibit_shares_outstanding_change_1d"] == 10_000_000
    assert second.record.record.change_context is not None
    assert second.record.record.change_context.sample_size == 1
    assert second.new_fact_revision is not None
    assert (
        second.new_fact_revision.decision_materiality
        == FactDecisionMateriality.BACKGROUND
    )


@pytest.mark.parametrize(
    ("stream_id", "replacements", "expected_change"),
    (
        (
            ARKB_HOLDINGS_STREAM_ID,
            (
                (b"08/21/2026", b"08/22/2026"),
                (b"35,026.00000000", b"35,126.00000000"),
                (b"2,542,000,000.00", b"2,552,000,000.00"),
            ),
            Decimal("100"),
        ),
        (
            BITB_HOLDINGS_STREAM_ID,
            (
                (b"2026-08-21", b"2026-08-22"),
                (b"37871.96676424", b"37971.96676424"),
                (b"69780000", b"69880000"),
            ),
            Decimal("100"),
        ),
    ),
)
def test_each_additional_issuer_builds_only_its_own_point_in_time_change(
    stream_id: str,
    replacements: tuple[tuple[bytes, bytes], ...],
    expected_change: Decimal,
) -> None:
    engine = _engine()
    ingestor = SqlOfficialMetricFactIngestor(engine, policy=METRIC_POLICY)
    first_document = _documents()[stream_id]
    ingestor.ingest(first_document, observed_at=OBSERVED_AT)
    content = first_document.content
    for old, new in replacements:
        content = content.replace(old, new)

    second = ingestor.ingest(
        replace(first_document, content=content),
        observed_at=OBSERVED_AT + timedelta(days=1),
    )

    assert second.record is not None
    values = {item.name.value: item.value for item in second.record.record.metrics}
    assert values["btc_etp_holdings_change_1d"] == expected_change
    assert second.record.record.change_context is not None
    assert second.record.record.change_context.sample_size == 1


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
            policy=METRIC_POLICY,
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
            policy=METRIC_POLICY,
        ),
        publish_recent=lambda as_of: None,
        fast_poll_seconds=300,
        slow_poll_seconds=21_600,
        poll_recorder=BrokenRecorder(),
        clock=lambda: OBSERVED_AT,
    )

    with pytest.raises(SourcePollAuditError, match="无法持久化"):
        asyncio.run(service._poll(TGA_STREAM_ID))
