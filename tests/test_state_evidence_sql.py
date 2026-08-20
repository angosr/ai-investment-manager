from datetime import timedelta

import pytest
from sqlalchemy import create_engine, func, insert, select

from investment_manager.fact_pipeline import build_state_snapshot
from investment_manager.fact_state_sql import SqlFactStateStore
from investment_manager.market.features import FeatureEngine
from investment_manager.persistence import (
    state_evidence_snapshots,
    state_snapshots,
)
from investment_manager.schema import create_schema
from investment_manager.state_evidence_sql import SqlStateEvidenceStore, StateEvidenceKind


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

    assert states.record_state(state=state, previous_state_id=None) == state
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
        states.record_state(state=state, previous_state_id=None)


def test_idempotent_state_replay_audits_pre_evidence_rows(replay_input) -> None:
    engine, _, states = _stores()
    market_ref = "a" * 64
    legacy_state = build_state_snapshot(
        projection_version="state-v1",
        analysis_scope="crypto-portfolio",
        as_of=replay_input.market.as_of,
        built_at=replay_input.market.as_of,
        facts=(),
        market_snapshot_refs=(market_ref,),
    )
    with engine.begin() as connection:
        connection.execute(
            insert(state_snapshots).values(
                state_id=legacy_state.state_id,
                projection_version=legacy_state.projection_version,
                analysis_scope=legacy_state.analysis_scope,
                as_of=legacy_state.as_of,
                built_at=legacy_state.built_at,
                content_hash=legacy_state.content_hash,
                payload=legacy_state.model_dump(mode="json"),
            )
        )

    with pytest.raises(ValueError, match=market_ref):
        states.record_state(state=legacy_state, previous_state_id=None)
