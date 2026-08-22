import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool

from investment_manager.information.models import SourcePollStatus, SourceTier
from investment_manager.information.official.regulation import (
    FEDERAL_REGISTER_RULEMAKING_STREAM_ID,
    parse_federal_register_rulemaking,
)
from investment_manager.information.official.source import (
    HttpFederalRegisterSource,
    OfficialRegulatoryDocument,
)
from investment_manager.information.tables import source_observations
from investment_manager.schema import create_schema
from investment_manager.state.facts import OfficialFactProjectionPolicy
from investment_manager.state.models import FactDecisionMateriality
from investment_manager.state.official_ingestion import (
    RegulatoryOfficialCollectorService,
    SourcePollAuditError,
    SqlFederalRegisterFactIngestor,
)
from investment_manager.state.tables import canonical_fact_revisions

OBSERVED_AT = datetime(2026, 8, 21, 23, tzinfo=UTC)
SOURCE_URL = (
    "https://www.federalregister.gov/api/v1/documents.json?"
    "conditions%5Bpublication_date%5D%5Bgte%5D=2026-08-14"
)


def _payload(*, abstract: str = "Tailored exemptions for crypto asset offerings.") -> bytes:
    return json.dumps(
        {
            "results": [
                {
                    "document_number": "2026-17183",
                    "type": "Proposed Rule",
                    "title": "Regulation Crypto Assets",
                    "abstract": abstract,
                    "action": "Proposed rule.",
                    "publication_date": "2026-08-21",
                    # Federal Register metadata can expose a date for a proposal;
                    # it must not be represented as a final rule effective date.
                    "effective_on": "2026-08-21",
                    "comments_close_on": "2026-10-20",
                    "agencies": [
                        {"name": "Securities and Exchange Commission"}
                    ],
                    "html_url": (
                        "https://www.federalregister.gov/documents/2026/08/21/"
                        "2026-17183/regulation-crypto-assets"
                    ),
                },
                {
                    "document_number": "2026-17000",
                    "type": "Proposed Rule",
                    "title": "Agricultural swap reporting",
                    "abstract": "Reporting rules for agricultural commodity swaps.",
                    "action": "Proposed rule.",
                    "publication_date": "2026-08-20",
                    "agencies": [
                        {"name": "Commodity Futures Trading Commission"}
                    ],
                    "html_url": (
                        "https://www.federalregister.gov/documents/2026/08/20/"
                        "2026-17000/agricultural-swap-reporting"
                    ),
                },
                {
                    "document_number": "2026-16999",
                    "type": "Proposed Rule",
                    "title": "Crypto asset custody",
                    "abstract": "A non-covered agency publication.",
                    "action": "Proposed rule.",
                    "publication_date": "2026-08-20",
                    "agencies": [{"name": "Department of Commerce"}],
                    "html_url": (
                        "https://www.federalregister.gov/documents/2026/08/20/"
                        "2026-16999/crypto-asset-custody"
                    ),
                },
            ]
        }
    ).encode()


def _document(content: bytes | None = None) -> OfficialRegulatoryDocument:
    return OfficialRegulatoryDocument(
        stream_id=FEDERAL_REGISTER_RULEMAKING_STREAM_ID,
        source_url=SOURCE_URL,
        content=_payload() if content is None else content,
    )


def _engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_schema(engine)
    return engine


def _policy() -> OfficialFactProjectionPolicy:
    return OfficialFactProjectionPolicy(
        version="official-regulation-v1",
        affected_assets=("BTC", "ETH"),
    )


def test_parser_keeps_only_relevant_sec_or_cftc_rulemaking() -> None:
    records = parse_federal_register_rulemaking(
        _payload(),
        source_url=SOURCE_URL,
        observed_at=OBSERVED_AT,
    )

    assert len(records) == 1
    record = records[0]
    assert record.document_number == "2026-17183"
    assert record.document_type == "Proposed Rule"
    assert record.effective_at is None
    assert record.comments_close_at == datetime(2026, 10, 20, tzinfo=UTC)
    assert record.observation.source_tier == SourceTier.FIRST_PARTY
    assert "Proposed" in record.action


def test_http_source_uses_bounded_official_query() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=_payload(), request=request)

    source = HttpFederalRegisterSource(
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )
    document = source.fetch(observed_at=OBSERVED_AT)

    assert document is not None
    assert document.stream_id == FEDERAL_REGISTER_RULEMAKING_STREAM_ID
    assert requests[0].url.host == "www.federalregister.gov"
    assert requests[0].url.params["conditions[publication_date][gte]"] == "2026-08-14"
    assert requests[0].url.params.get_list("conditions[agencies][]") == [
        "commodity-futures-trading-commission",
        "securities-and-exchange-commission",
    ]


def test_ingestion_is_idempotent_and_revisions_are_append_only() -> None:
    engine = _engine()
    ingestor = SqlFederalRegisterFactIngestor(engine, _policy())

    first = ingestor.ingest(_document(), observed_at=OBSERVED_AT)
    duplicate = ingestor.ingest(
        _document(),
        observed_at=OBSERVED_AT + timedelta(minutes=1),
    )
    changed = ingestor.ingest(
        _document(_payload(abstract="Expanded exemptions for crypto asset offerings.")),
        observed_at=OBSERVED_AT + timedelta(minutes=2),
    )

    assert len(first.new_fact_revisions) == 1
    assert first.new_fact_revisions[0].decision_materiality == FactDecisionMateriality.CANDIDATE
    assert "legal_status=proposal-not-final" in first.new_fact_revisions[0].claim
    assert len(first.new_fact_revisions[0].claim) <= 600
    assert duplicate.new_fact_revisions == ()
    assert len(changed.new_fact_revisions) == 1
    assert (
        changed.new_fact_revisions[0].previous_revision_id
        == first.new_fact_revisions[0].revision_id
    )
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(source_observations)) == 2
        assert connection.scalar(select(func.count()).select_from(canonical_fact_revisions)) == 2


def test_collector_audits_poll_and_publishes_new_fact() -> None:
    polls = []
    published = []

    class Source:
        def fetch(self, *, observed_at):
            return _document()

    class Recorder:
        def put(self, poll):
            polls.append(poll)
            return True

    service = RegulatoryOfficialCollectorService(
        source=Source(),
        ingestor=SqlFederalRegisterFactIngestor(_engine(), _policy()),
        publish_recent=published.append,
        poll_seconds=300,
        poll_recorder=Recorder(),
        clock=lambda: OBSERVED_AT,
    )

    asyncio.run(service._poll())
    asyncio.run(service._publish())

    assert len(polls) == 1
    assert polls[0].status == SourcePollStatus.CHANGED
    assert polls[0].new_fact_count == 1
    assert polls[0].latest_publication_at == datetime(2026, 8, 21, tzinfo=UTC)
    assert published == [OBSERVED_AT]


def test_collector_fails_closed_when_poll_audit_is_not_durable() -> None:
    class Source:
        def fetch(self, *, observed_at):
            return _document()

    class BrokenRecorder:
        def put(self, poll):
            raise OSError("coverage unavailable")

    service = RegulatoryOfficialCollectorService(
        source=Source(),
        ingestor=SqlFederalRegisterFactIngestor(_engine(), _policy()),
        publish_recent=lambda _: None,
        poll_seconds=300,
        poll_recorder=BrokenRecorder(),
        clock=lambda: OBSERVED_AT,
    )

    with pytest.raises(SourcePollAuditError, match="无法持久化"):
        asyncio.run(service._poll())


def test_trigger_publication_failure_does_not_corrupt_source_health() -> None:
    class Source:
        def fetch(self, *, observed_at):
            return _document()

    def broken_publish(_):
        raise OSError("trigger store unavailable")

    service = RegulatoryOfficialCollectorService(
        source=Source(),
        ingestor=SqlFederalRegisterFactIngestor(_engine(), _policy()),
        publish_recent=broken_publish,
        poll_seconds=300,
        clock=lambda: OBSERVED_AT,
    )

    asyncio.run(service._poll())
    asyncio.run(service._publish())

    assert service.health.error_class is None
    assert service.health.last_success_at == OBSERVED_AT
    assert service.health.publication_error_class == "OSError"
