import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool

from investment_manager.information.aggregated_flows import (
    ETF_AGGREGATE_FLOW_STREAM_ID,
    ETF_AGGREGATE_FLOW_URL,
    AggregatedEtfFlowDocument,
    parse_aggregated_etf_flows,
)
from investment_manager.information.models import SourcePollStatus, SourceTier
from investment_manager.information.tables import source_observations
from investment_manager.schema import create_schema
from investment_manager.state.facts import OfficialFactProjectionPolicy
from investment_manager.state.metric_ingestion import (
    AggregatedEtfFlowCollectorService,
    SqlAggregatedEtfFlowFactIngestor,
)
from investment_manager.state.models import FactDecisionMateriality
from investment_manager.state.tables import canonical_fact_revisions

OBSERVED_AT = datetime(2026, 8, 21, 20, tzinfo=UTC)
POLICY = OfficialFactProjectionPolicy(
    version="aggregate-flow-v1",
    affected_assets=("BTC", "ETH"),
)


def _document(*, latest_btc_flow_usd: int = 100_000_000) -> AggregatedEtfFlowDocument:
    start = date(2026, 7, 1)
    rows = []
    for asset in ("BTC", "ETH"):
        for index in range(30):
            net_flow = (index + 1) * 1_000_000
            if asset == "BTC" and index == 29:
                net_flow = latest_btc_flow_usd
            rows.append(
                {
                    "date": (start + timedelta(days=index)).isoformat(),
                    "asset": asset,
                    "net_inflow_usd": net_flow,
                    "net_assets_usd": 50_000_000_000,
                    "cumulative_inflow_usd": 10_000_000_000 + net_flow,
                    "value_traded_usd": 2_000_000_000,
                    "source": "bykaranteli.com",
                }
            )
    return AggregatedEtfFlowDocument(
        content=json.dumps(
            {"dataset": "etf-flows", "count": len(rows), "rows": rows},
            separators=(",", ":"),
        ).encode(),
    )


def _engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_schema(engine)
    return engine


def test_aggregate_flow_parser_keeps_point_in_time_and_source_tier() -> None:
    snapshots = parse_aggregated_etf_flows(_document(), observed_at=OBSERVED_AT)

    assert tuple(item.asset for item in snapshots) == ("BTC", "ETH")
    btc, eth = snapshots
    assert btc.net_inflow_usd_m == Decimal("100.000")
    assert btc.absolute_flow_percentile == 1
    assert btc.sample_size == 30
    assert eth.net_inflow_usd_m == Decimal("30.000")
    assert btc.observation.source_tier == SourceTier.AGGREGATOR
    assert btc.observation.observed_at == OBSERVED_AT
    assert btc.observation.source_published_at == OBSERVED_AT
    assert btc.source_url == ETF_AGGREGATE_FLOW_URL
    assert btc.observation.payload_ref == eth.observation.payload_ref


@pytest.mark.parametrize("mutation", ("count", "duplicate", "future", "source"))
def test_aggregate_flow_parser_rejects_inconsistent_dataset(mutation: str) -> None:
    payload = json.loads(_document().content)
    if mutation == "count":
        payload["count"] += 1
    elif mutation == "duplicate":
        payload["rows"][-1] = payload["rows"][0]
    elif mutation == "future":
        payload["rows"][-1]["date"] = "2026-08-22"
    else:
        payload["rows"][-1]["source"] = "untrusted.example"
    document = AggregatedEtfFlowDocument(content=json.dumps(payload).encode())

    with pytest.raises(ValueError):
        parse_aggregated_etf_flows(document, observed_at=OBSERVED_AT)


def test_aggregate_flow_ingestion_is_idempotent_and_revises_only_changed_asset() -> None:
    engine = _engine()
    ingestor = SqlAggregatedEtfFlowFactIngestor(engine, policy=POLICY)

    first = ingestor.ingest(_document(), observed_at=OBSERVED_AT)
    duplicate = ingestor.ingest(
        _document(), observed_at=OBSERVED_AT + timedelta(minutes=1)
    )
    revised = ingestor.ingest(
        _document(latest_btc_flow_usd=-120_000_000),
        observed_at=OBSERVED_AT + timedelta(minutes=2),
    )

    assert len(first.records) == 2
    assert len(first.new_fact_revisions) == 2
    assert all(
        item.decision_materiality == FactDecisionMateriality.CANDIDATE
        for item in first.new_fact_revisions
    )
    assert duplicate.new_fact_revisions == ()
    assert len(revised.new_fact_revisions) == 1
    assert all(item.previous_revision_id for item in revised.new_fact_revisions)
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(source_observations)) == 3
        assert (
            connection.scalar(select(func.count()).select_from(canonical_fact_revisions))
            == 3
        )


def test_aggregate_flow_collector_audits_success_and_failure() -> None:
    engine = _engine()
    polls = []

    class Source:
        fail = False

        def fetch(self, *, observed_at):
            if self.fail:
                raise OSError("unavailable")
            return _document()

    class Recorder:
        def put(self, poll):
            polls.append(poll)
            return True

    source = Source()
    service = AggregatedEtfFlowCollectorService(
        source=source,
        ingestor=SqlAggregatedEtfFlowFactIngestor(engine, policy=POLICY),
        publish_recent=lambda as_of: None,
        poll_seconds=300,
        poll_recorder=Recorder(),
        clock=lambda: OBSERVED_AT,
    )

    asyncio.run(service._poll())
    source.fail = True
    asyncio.run(service._poll())

    assert [item.source_stream_id for item in polls] == [
        ETF_AGGREGATE_FLOW_STREAM_ID,
        ETF_AGGREGATE_FLOW_STREAM_ID,
    ]
    assert [item.status for item in polls] == [
        SourcePollStatus.CHANGED,
        SourcePollStatus.FAILED,
    ]
    assert polls[0].observation_count == 2
    assert polls[0].new_fact_count == 2
    assert service.health.new_fact_revision_count == 2
    assert service.health.last_error_class == "OSError"
