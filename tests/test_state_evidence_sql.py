from datetime import timedelta

import pytest
from sqlalchemy import create_engine, func, select

from quant_core.fact_pipeline import build_state_snapshot
from quant_core.fact_state_sql import SqlFactStateStore
from quant_core.features import FeatureEngine
from quant_core.persistence import create_schema, state_evidence_snapshots
from quant_core.state_evidence_sql import SqlStateEvidenceStore, StateEvidenceKind


def _stores():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    return engine, SqlStateEvidenceStore(engine), SqlFactStateStore(engine)


def test_state_evidence_is_content_addressed_and_replayable(
    app_config, replay_input
) -> None:
    engine, evidence, states = _stores()
    market = replay_input.market
    feature = FeatureEngine(app_config.feature).compute(market)
    account = replay_input.account

    market_ref = evidence.put_market(market)
    feature_ref = evidence.put_feature(feature)
    account_ref = evidence.put_account(account)
    state = build_state_snapshot(
        projection_version="state-v1",
        analysis_scope="crypto-portfolio",
        as_of=market.as_of,
        built_at=market.as_of,
        facts=(),
        market_snapshot_refs=(market_ref,),
        feature_snapshot_refs=(feature_ref,),
        account_snapshot_ref=account_ref,
    )

    assert states.put_state(state) == state
    assert evidence.get(market_ref) == (StateEvidenceKind.MARKET, market)
    assert evidence.get(feature_ref) == (StateEvidenceKind.FEATURE, feature)
    assert evidence.get(account_ref) == (StateEvidenceKind.ACCOUNT, account)
    assert evidence.put_market(market) == market_ref
    with engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(state_evidence_snapshots)
        ) == 3


def test_state_rejects_missing_or_future_evidence(app_config, replay_input) -> None:
    _, evidence, states = _stores()
    market = replay_input.market
    future_market = market.model_copy(
        update={
            "cycle_id": "future-market",
            "as_of": market.as_of + timedelta(minutes=1),
            "observed_at": market.as_of + timedelta(minutes=1),
        }
    )
    future_ref = evidence.put_market(future_market)
    state = build_state_snapshot(
        projection_version="state-v1",
        analysis_scope="crypto-portfolio",
        as_of=market.as_of,
        built_at=market.as_of,
        facts=(),
        market_snapshot_refs=(future_ref,),
        feature_snapshot_refs=("f" * 64,),
    )

    with pytest.raises(ValueError, match="evidence"):
        states.put_state(state)
