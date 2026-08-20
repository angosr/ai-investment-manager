from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select, update

from investment_manager.market.features import FeatureEngine
from investment_manager.schema import create_schema
from investment_manager.state.application import (
    DecisionPacketPreparation,
    PacketPreparationStatus,
)
from investment_manager.state.decision_packet import (
    AnalysisMandate,
    MandateAsset,
)
from investment_manager.state.decision_packet_repository import SqlDecisionPacketAssembler
from investment_manager.state.facts import (
    FOMC_MEETING_FACT_TYPE,
    FactDeltaPolicy,
    FactDeltaRule,
    OfficialFactProjectionPolicy,
)
from investment_manager.state.models import Materiality
from investment_manager.state.official_ingestion import SqlFedFactIngestor
from investment_manager.state.policy import DecisionPacketPolicy
from investment_manager.state.projection import SqlFactStateProjector
from investment_manager.state.repository import SqlFactStateStore
from investment_manager.state.tables import (
    material_deltas,
    state_evidence_snapshots,
    state_snapshots,
)

OBSERVED_AT = datetime(2026, 8, 20, 12, tzinfo=UTC)
FACT_POLICY = OfficialFactProjectionPolicy(
    version="fed-fact-v1",
    affected_assets=("BTC", "ETH"),
)
DELTA_POLICY = FactDeltaPolicy(
    version="fact-delta-v1",
    validity_seconds=3_600,
    horizons_minutes=(60, 240),
    rules=(
        FactDeltaRule(
            fact_type=FOMC_MEETING_FACT_TYPE,
            materiality=Materiality.NORMAL,
            reason_code="FOMC_SCHEDULE_REVISION",
        ),
    ),
)


def _calendar(date_text: str) -> str:
    return f"""
    <h4>2026 FOMC Meetings</h4><div class="row fomc-meeting">
      <div class="fomc-meeting__month"><strong>September</strong></div>
      <div class="fomc-meeting__date">{date_text}</div>
    </div>
    """


class _PointInTimeMarketStore:
    def __init__(self, market) -> None:
        self.market = market

    def snapshot(self, *, cycle_id, symbol, interval, as_of, bar_window, source):
        assert symbol == self.market.symbol
        return self.market.model_copy(
            update={
                "cycle_id": cycle_id,
                "as_of": as_of,
                "observed_at": as_of,
                "source": source,
            }
        )


class _PointInTimeAccountReader:
    def __init__(self, account) -> None:
        self.account = account

    def account_for_cycle(self, *, cycle_id, as_of, initial_quote_balance):
        return self.account.model_copy(
            update={
                "cycle_id": cycle_id,
                "as_of": as_of,
                "observed_at": as_of,
            }
        )


def test_fact_state_projector_records_frozen_evidence_and_fact_revision(
    app_config,
    replay_input,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    fed = SqlFedFactIngestor(engine, FACT_POLICY)
    states = SqlFactStateStore(engine)
    projector = SqlFactStateProjector(
        engine,
        projection_version="portfolio-state-v1",
        delta_policy=DELTA_POLICY,
    )
    first_fact = fed.ingest_calendar(
        _calendar("15-16"),
        observed_at=OBSERVED_AT,
    ).new_fact_revisions[0]
    first_market = replay_input.market.model_copy(
        update={
            "cycle_id": "state-first",
            "as_of": OBSERVED_AT,
            "observed_at": OBSERVED_AT,
        }
    )
    first_feature = FeatureEngine(app_config.feature).compute(first_market)
    first_account = replay_input.account.model_copy(
        update={
            "cycle_id": "state-first",
            "as_of": OBSERVED_AT,
            "observed_at": OBSERVED_AT,
        }
    )

    bootstrap = projector.project(
        analysis_scope="crypto-portfolio",
        as_of=OBSERVED_AT,
        built_at=OBSERVED_AT,
        facts=(first_fact,),
        markets=(first_market,),
        features=(first_feature,),
        account=first_account,
    )
    refreshed = projector.project(
        analysis_scope="crypto-portfolio",
        as_of=OBSERVED_AT + timedelta(minutes=1),
        built_at=OBSERVED_AT + timedelta(minutes=1),
        facts=states.facts_as_of(as_of=OBSERVED_AT + timedelta(minutes=1)),
        markets=(
            first_market.model_copy(
                update={
                    "cycle_id": "state-unchanged",
                    "as_of": OBSERVED_AT + timedelta(minutes=1),
                    "observed_at": OBSERVED_AT + timedelta(minutes=1),
                }
            ),
        ),
        features=(first_feature,),
        account=first_account,
    )
    revised_at = OBSERVED_AT + timedelta(minutes=2)
    revised_fact = fed.ingest_calendar(
        _calendar("16-17"),
        observed_at=revised_at,
    ).new_fact_revisions[0]
    revised_market = first_market.model_copy(
        update={
            "cycle_id": "state-revised",
            "as_of": revised_at,
            "observed_at": revised_at,
        }
    )
    revised_feature = FeatureEngine(app_config.feature).compute(revised_market)
    revised_account = first_account.model_copy(
        update={
            "cycle_id": "state-revised",
            "as_of": revised_at,
            "observed_at": revised_at,
        }
    )
    revised = projector.project(
        analysis_scope="crypto-portfolio",
        as_of=revised_at,
        built_at=revised_at,
        facts=(revised_fact,),
        markets=(revised_market,),
        features=(revised_feature,),
        account=revised_account,
    )
    replayed = projector.project(
        analysis_scope="crypto-portfolio",
        as_of=revised_at,
        built_at=revised_at + timedelta(seconds=1),
        facts=(revised_fact,),
        markets=(revised_market,),
        features=(revised_feature,),
        account=revised_account,
    )

    assert bootstrap.changed is True
    assert bootstrap.delta is None
    assert refreshed.state != bootstrap.state
    assert refreshed.state.fact_revision_ids == bootstrap.state.fact_revision_ids
    assert refreshed.state.market_snapshot_refs != bootstrap.state.market_snapshot_refs
    assert refreshed.delta is None
    assert refreshed.changed is True
    assert revised.changed is True
    assert revised.delta is not None
    assert replayed.state == revised.state
    assert replayed.delta == revised.delta
    assert replayed.changed is False
    assembler = SqlDecisionPacketAssembler(
        engine,
        DecisionPacketPolicy(
            version="packet-policy-v1",
            schema_version="decision-packet-v1",
        ),
    )
    mandate = AnalysisMandate(
        version="crypto-mandate-v1",
        analysis_scope="crypto-portfolio",
        question="Assess the material Fed revision across the portfolio.",
        assets=(
            MandateAsset(
                asset="BTC",
                market_symbol="BTCUSDT",
                horizons_minutes=(60, 240),
            ),
        ),
        required_risk_factors=("US_MONETARY_POLICY",),
    )
    packet = assembler.assemble(
        mandate=mandate,
        state_id=revised.state.state_id,
        delta_ids=(revised.delta.delta_id,),
    )

    assert packet.state_id == revised.state.state_id
    assert packet.trigger_ids == (revised.delta.delta_id,)
    assert packet.asset_states[0].market_symbol == "BTCUSDT"
    assert packet.facts[0].highest_source_tier == "FIRST_PARTY"
    assert packet.facts[0].independent_source_count == 1
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(state_snapshots)) == 3
        assert connection.scalar(select(func.count()).select_from(material_deltas)) == 1
        assert connection.scalar(
            select(func.count()).select_from(state_evidence_snapshots)
        ) == 7

    with engine.begin() as connection:
        payload = connection.scalar(
            select(state_snapshots.c.payload).where(
                state_snapshots.c.state_id == revised.state.state_id
            )
        )
        connection.execute(
            update(state_snapshots)
            .where(state_snapshots.c.state_id == revised.state.state_id)
            .values(payload={**payload, "coverage_gap_codes": ["TAMPERED"]})
        )
    with pytest.raises(ValueError, match="StateSnapshot content_hash"):
        assembler.assemble(
            mandate=mandate,
            state_id=revised.state.state_id,
            delta_ids=(revised.delta.delta_id,),
        )


def test_packet_preparation_runs_only_for_material_canonical_fact_change(
    app_config,
    replay_input,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    fed = SqlFedFactIngestor(engine, FACT_POLICY)
    facts = SqlFactStateStore(engine)
    projector = SqlFactStateProjector(
        engine,
        projection_version="portfolio-state-v1",
        delta_policy=DELTA_POLICY,
    )
    preparation = DecisionPacketPreparation(
        market_store=_PointInTimeMarketStore(replay_input.market),
        account_reader=_PointInTimeAccountReader(replay_input.account),
        facts=facts,
        projector=projector,
        assembler=SqlDecisionPacketAssembler(
            engine,
            DecisionPacketPolicy(
                version="packet-policy-v1",
                schema_version="decision-packet-v1",
            ),
        ),
        features=FeatureEngine(app_config.feature),
        market_interval=app_config.market_data.interval,
        market_bar_window=app_config.market_data.bar_window,
        market_source=app_config.market_data.version,
        initial_quote_balance=app_config.shadow.initial_quote_balance,
        maximum_market_age_seconds=app_config.risk.maximum_market_age_seconds,
        clock=lambda: OBSERVED_AT + timedelta(minutes=2),
    )
    mandate = AnalysisMandate(
        version="crypto-mandate-v1",
        analysis_scope="crypto-portfolio",
        question="Assess material first-party changes across the portfolio.",
        assets=(
            MandateAsset(
                asset="BTC",
                market_symbol="BTCUSDT",
                horizons_minutes=(60, 240),
            ),
        ),
        required_risk_factors=("US_MONETARY_POLICY",),
    )
    fed.ingest_calendar(_calendar("15-16"), observed_at=OBSERVED_AT)

    baseline = preparation.prepare(
        analysis_id="assessment-baseline",
        as_of=OBSERVED_AT,
        mandate=mandate,
    )
    unchanged = preparation.prepare(
        analysis_id="assessment-unchanged",
        as_of=OBSERVED_AT + timedelta(minutes=1),
        mandate=mandate,
    )
    revised_at = OBSERVED_AT + timedelta(minutes=2)
    fed.ingest_calendar(_calendar("16-17"), observed_at=revised_at)
    revised = preparation.prepare(
        analysis_id="assessment-revised",
        as_of=revised_at,
        mandate=mandate,
    )
    replayed = preparation.prepare(
        analysis_id="assessment-revised",
        as_of=revised_at,
        mandate=mandate,
    )

    assert baseline.status == PacketPreparationStatus.BASELINE_RECORDED
    assert baseline.packet is None
    assert unchanged.status == PacketPreparationStatus.NO_MATERIAL_DELTA
    assert unchanged.packet is None
    assert revised.status == PacketPreparationStatus.READY
    assert revised.packet is not None
    assert revised.packet.analysis_scope == "crypto-portfolio"
    assert revised.packet.trigger_ids == (revised.delta_id,)
    assert revised.packet.facts[0].highest_source_tier == "FIRST_PARTY"
    assert replayed == revised
