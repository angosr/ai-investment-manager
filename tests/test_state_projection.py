from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, func, select

from quant_core.asset_management import Materiality
from quant_core.fact_pipeline import (
    FOMC_MEETING_FACT_TYPE,
    FactDeltaPolicy,
    FactDeltaRule,
    OfficialFactProjectionPolicy,
)
from quant_core.fact_state_sql import SqlFactStateStore
from quant_core.features import FeatureEngine
from quant_core.official_fact_pipeline import SqlFedFactIngestor
from quant_core.persistence import (
    create_schema,
    material_deltas,
    state_evidence_snapshots,
    state_snapshots,
)
from quant_core.state_projection import SqlFactStateProjector

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


def test_fact_state_projector_bootstraps_deduplicates_and_records_revision(
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
    unchanged = projector.project(
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
    assert unchanged.state == bootstrap.state
    assert unchanged.delta is None
    assert unchanged.changed is False
    assert revised.changed is True
    assert revised.delta is not None
    assert replayed.state == revised.state
    assert replayed.delta == revised.delta
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(state_snapshots)) == 2
        assert connection.scalar(select(func.count()).select_from(material_deltas)) == 1
        assert connection.scalar(
            select(func.count()).select_from(state_evidence_snapshots)
        ) == 6
