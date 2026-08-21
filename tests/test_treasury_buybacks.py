import asyncio
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from investment_manager.information.coverage import SqlInformationCoverageStore
from investment_manager.information.models import CausalDomain, SourcePollRecord, SourcePollStatus
from investment_manager.information.official.repository import (
    SqlTreasuryBuybackInformationIngestor,
)
from investment_manager.information.official.source import HttpTreasuryBuybackSource
from investment_manager.information.official.treasury_buybacks import (
    TREASURY_BUYBACK_SOURCE_ID,
    TREASURY_BUYBACK_STREAM_ID,
    TREASURY_BUYBACK_URL,
    parse_treasury_buyback_calendar,
)
from investment_manager.information.tables import source_poll_records
from investment_manager.schema import create_schema
from investment_manager.state.facts import (
    TREASURY_BUYBACK_OPERATION_FACT_TYPE,
    OfficialFactProjectionPolicy,
)
from investment_manager.state.models import FactRevisionStatus
from investment_manager.state.official_ingestion import (
    SqlTreasuryBuybackFactIngestor,
    TreasuryBuybackCollectorService,
)
from investment_manager.state.repository import SqlFactStateStore

OBSERVED_AT = datetime(2026, 8, 17, 12, tzinfo=UTC)
POLICY = OfficialFactProjectionPolicy(
    version="treasury-buyback-fact-v1",
    affected_assets=("BTC", "ETH"),
)


def _calendar(*, include_second: bool = True, first_maximum: int = 2_000_000_000) -> bytes:
    second = (
        """
        <BuybackCalendarDate>
          <OperationDate>2026-08-20</OperationDate>
          <OperationStartTimeEasternUS>13:40:00</OperationStartTimeEasternUS>
          <OperationEndTimeEasternUS>14:00:00</OperationEndTimeEasternUS>
          <SettlementDate>2026-08-21</SettlementDate>
          <OperationType>Liquidity Support</OperationType>
          <SecurityType>Nominal Coupon</SecurityType>
          <PurchaseBucketName>3Y-5Y</PurchaseBucketName>
          <MaturityDateRangeStart>2029-08-15</MaturityDateRangeStart>
          <MaturityDateRangeEnd>2031-08-15</MaturityDateRangeEnd>
          <MinimumPurchaseAmountDollars>1000000</MinimumPurchaseAmountDollars>
          <MaximumPurchaseAmountDollars>4000000000</MaximumPurchaseAmountDollars>
        </BuybackCalendarDate>
        """
        if include_second
        else ""
    )
    return f"""
    <BuyBackCalendar>
      <BuybackCalendarName>August 2026 Refunding Tentative Buyback Calendar V1</BuybackCalendarName>
      <StartDate>2026-08-18</StartDate>
      <EndDate>2026-10-23</EndDate>
      <BuybackCalendarDate>
        <OperationDate>2026-08-18</OperationDate>
        <OperationStartTimeEasternUS>13:40:00</OperationStartTimeEasternUS>
        <OperationEndTimeEasternUS>14:00:00</OperationEndTimeEasternUS>
        <SettlementDate>2026-08-19</SettlementDate>
        <OperationType>Liquidity Support</OperationType>
        <SecurityType>Nominal Coupon</SecurityType>
        <PurchaseBucketName>20Y-30Y</PurchaseBucketName>
        <MaturityDateRangeStart>2046-08-15</MaturityDateRangeStart>
        <MaturityDateRangeEnd>2056-08-15</MaturityDateRangeEnd>
        <MinimumPurchaseAmountDollars>1000000</MinimumPurchaseAmountDollars>
        <MaximumPurchaseAmountDollars>{first_maximum}</MaximumPurchaseAmountDollars>
      </BuybackCalendarDate>
      {second}
    </BuyBackCalendar>
    """.encode()


def _engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_schema(engine)
    return engine


def test_calendar_parser_preserves_schedule_semantics_and_dst() -> None:
    snapshot = parse_treasury_buyback_calendar(
        _calendar(),
        observed_at=OBSERVED_AT,
    )

    assert snapshot.calendar_cycle == "August 2026 Refunding Tentative Buyback Calendar"
    assert len(snapshot.records) == 2
    first, second = snapshot.records
    assert first.operation_start_at == datetime(2026, 8, 18, 17, 40, tzinfo=UTC)
    assert first.maximum_purchase_usd_m == 2_000
    assert second.maximum_purchase_usd_m == 4_000
    assert first.observation.source_id == TREASURY_BUYBACK_SOURCE_ID


def test_ingestion_revises_amount_and_cancels_removed_future_operation() -> None:
    engine = _engine()
    ingestor = SqlTreasuryBuybackFactIngestor(engine, POLICY)
    first = ingestor.ingest_calendar(_calendar(), observed_at=OBSERVED_AT)
    revised = ingestor.ingest_calendar(
        _calendar(include_second=False, first_maximum=3_000_000_000),
        observed_at=OBSERVED_AT + timedelta(hours=1),
    )

    assert len(first.new_fact_revisions) == 2
    assert len(revised.new_fact_revisions) == 2
    facts = SqlFactStateStore(engine).facts_as_of(
        as_of=OBSERVED_AT + timedelta(hours=1)
    )
    assert {item.fact_type for item in facts} == {
        TREASURY_BUYBACK_OPERATION_FACT_TYPE
    }
    assert sorted(item.status for item in facts) == [
        FactRevisionStatus.ACTIVE,
        FactRevisionStatus.CANCELLED,
    ]
    active = next(item for item in facts if item.status == FactRevisionStatus.ACTIVE)
    assert "scheduled_purchase_range_usd_m=1..3000" in active.claim
    assert "not Federal Reserve QE" in active.claim


def test_information_ingestor_replay_is_idempotent() -> None:
    engine = _engine()
    ingestor = SqlTreasuryBuybackInformationIngestor(engine)
    first = ingestor.ingest_calendar(_calendar(), observed_at=OBSERVED_AT)
    duplicate = ingestor.ingest_calendar(
        _calendar(),
        observed_at=OBSERVED_AT + timedelta(minutes=1),
    )

    assert all(item.inserted for item in first)
    assert all(not item.inserted for item in duplicate)
    assert all(item.calendar_revision is not None for item in duplicate)


def test_http_source_rejects_redirect_and_honors_conditional_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                content=_calendar(),
                headers={"etag": '"v1"'},
                request=request,
            )
        return httpx.Response(304, request=request)

    source = HttpTreasuryBuybackSource(
        timeout_seconds=2,
        transport=httpx.MockTransport(handler),
    )
    assert source.fetch_calendar() == _calendar()
    assert source.fetch_calendar() is None
    assert str(requests[0].url) == TREASURY_BUYBACK_URL
    assert requests[1].headers["if-none-match"] == '"v1"'

    redirecting = HttpTreasuryBuybackSource(
        timeout_seconds=2,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                302,
                headers={"location": "https://example.com/"},
                request=request,
            )
        ),
    )
    try:
        redirecting.fetch_calendar()
    except httpx.HTTPStatusError:
        pass
    else:
        raise AssertionError("redirect must be rejected")


def test_collector_records_fiscal_coverage() -> None:
    engine = _engine()
    coverage = SqlInformationCoverageStore(engine)

    class Source:
        def fetch_calendar(self) -> bytes:
            return _calendar()

    stop = asyncio.Event()
    service = TreasuryBuybackCollectorService(
        source=Source(),
        ingestor=SqlTreasuryBuybackFactIngestor(engine, POLICY),
        publish_recent=lambda as_of: stop.set(),
        poll_seconds=21_600,
        poll_recorder=coverage,
        clock=lambda: OBSERVED_AT,
    )
    asyncio.run(service.run(stop))

    with engine.connect() as connection:
        payload = connection.execute(
            select(source_poll_records.c.payload).where(
                source_poll_records.c.source_stream_id == TREASURY_BUYBACK_STREAM_ID
            )
        ).scalar_one()
    poll = SourcePollRecord.model_validate(payload)
    assert poll.domain == CausalDomain.FISCAL_DEBT
    assert poll.status == SourcePollStatus.CHANGED
    assert poll.new_fact_count == 2
