from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine

from investment_manager.analyst import AnalystResult, verify_bundle
from investment_manager.asset_management import (
    AssessmentUncertainty,
    CanonicalFactRevision,
    ContextView,
    DeltaCategory,
    FactRevisionStatus,
    MaterialDelta,
    Materiality,
    PricedState,
    StateSnapshot,
)
from investment_manager.context_analyst import AssessRunBundleBuilder, CodexContextAnalyst
from investment_manager.context_assessment_sql import SqlContextAssessmentStore
from investment_manager.decision_packet import (
    AnalysisMandate,
    AssessStructuredOutput,
    ContextAssessmentDraft,
    DecisionPacket,
    DecisionPacketBuilder,
    DecisionPacketCapacityError,
    DecisionPacketPolicy,
    MandateAsset,
    VisibleFact,
    build_assess_prompt,
    finalize_context_assessment,
)
from investment_manager.domain import DirectionalView
from investment_manager.information.models import SourceTier
from investment_manager.kernel.identity import canonical_json, content_hash
from investment_manager.market.features import FeatureEngine
from investment_manager.platform.database import metadata

HASH = "a" * 64


def _mandate() -> AnalysisMandate:
    return AnalysisMandate(
        version="mandate-v1",
        analysis_scope="crypto-risk",
        question="Assess material changes across the crypto portfolio.",
        assets=(
            MandateAsset(
                asset="BTC",
                market_symbol="BTCUSDT",
                horizons_minutes=(60, 240),
            ),
            MandateAsset(
                asset="ETH",
                market_symbol="ETHUSDT",
                horizons_minutes=(60, 240),
            ),
        ),
        required_risk_factors=("REGULATION",),
    )


def _fact(as_of, *, revision_id: str = "revision-1") -> VisibleFact:
    return VisibleFact(
        fact=CanonicalFactRevision(
            fact_id="fact-1",
            revision_id=revision_id,
            projection_version="fact-projection-v1",
            fact_type="REGULATORY_EVENT",
            status=FactRevisionStatus.ACTIVE,
            event_time=as_of + timedelta(hours=1),
            observed_at=as_of - timedelta(minutes=1),
            headline="<b>CFTC meeting</b>",
            claim="Official schedule update.",
            affected_assets=("BTC", "ETH"),
            risk_factors=("REGULATION",),
            source_observation_ids=("obs-1",),
            revision_hash=HASH,
        ),
        highest_source_tier=SourceTier.FIRST_PARTY,
        independent_source_count=1,
    )


def _state(as_of, *, account, markets, features) -> StateSnapshot:
    return StateSnapshot(
        state_id="state-1",
        projection_version="state-projection-v1",
        analysis_scope="crypto-risk",
        as_of=as_of,
        built_at=as_of,
        fact_revision_ids=("revision-1",),
        market_snapshot_refs=tuple(sorted(content_hash(item) for item in markets)),
        feature_snapshot_refs=tuple(sorted(content_hash(item) for item in features)),
        account_snapshot_ref=content_hash(account),
        content_hash=HASH,
    )


def _delta(as_of, *, delta_id: str = "delta-1", seconds: int = 0) -> MaterialDelta:
    observed_at = as_of - timedelta(seconds=seconds)
    return MaterialDelta(
        delta_id=delta_id,
        policy_version="delta-policy-v1",
        analysis_scope="crypto-risk",
        previous_state_id="state-0",
        current_state_id="state-1",
        observed_at=observed_at,
        expires_at=as_of + timedelta(minutes=30),
        category=DeltaCategory.FIRST_PARTY_FACT,
        materiality=Materiality.HIGH,
        affected_assets=("BTC", "ETH"),
        risk_factors=("REGULATION",),
        horizons_minutes=(60, 240),
        fact_revision_ids=("revision-1",),
        feature_snapshot_refs=("feature-btc", "feature-eth"),
        reason_codes=("OFFICIAL_REVISION",),
        content_hash=HASH,
    )


def _packet(app_config, replay_input):
    market_btc = replay_input.market
    market_eth = replay_input.market.model_copy(update={"symbol": "ETHUSDT"})
    feature_btc = FeatureEngine(app_config.feature).compute(market_btc)
    feature_eth = feature_btc.model_copy(update={"symbol": "ETHUSDT"})
    builder = DecisionPacketBuilder(
        DecisionPacketPolicy(
            version="packet-policy-v1",
            schema_version="decision-packet-v1",
        )
    )
    packet = builder.build(
        mandate=_mandate(),
        state=_state(
            market_btc.as_of,
            account=replay_input.account,
            markets=(market_btc, market_eth),
            features=(feature_btc, feature_eth),
        ),
        deltas=(
            _delta(market_btc.as_of, delta_id="delta-2"),
            _delta(market_btc.as_of, delta_id="delta-1", seconds=1),
        ),
        facts=(_fact(market_btc.as_of),),
        account=replay_input.account,
        markets=(market_eth, market_btc),
        features=(feature_eth, feature_btc),
    )
    return builder, packet


def _assessment_output() -> AssessStructuredOutput:
    return AssessStructuredOutput(
        assessment=ContextAssessmentDraft(
            market_mechanism="Regulatory clarity may alter the risk premium.",
            views=tuple(
                ContextView(
                    asset=asset,
                    horizon_minutes=horizon,
                    direction=DirectionalView.UNCERTAIN,
                    already_priced=PricedState.UNKNOWN,
                    uncertainty=AssessmentUncertainty.HIGH,
                    evidence_ids=("revision-1",),
                    invalidation_conditions=("official-retraction",),
                )
                for asset in ("BTC", "ETH")
                for horizon in (60, 240)
            ),
            data_gaps=("MARKET_REACTION_NOT_MATURE",),
        )
    )


def test_packet_is_one_multi_asset_high_density_projection(
    app_config, replay_input
) -> None:
    _, packet = _packet(app_config, replay_input)

    assert tuple(item.asset for item in packet.asset_states) == ("BTC", "ETH")
    assert tuple(
        (item.asset, item.horizon_minutes) for item in packet.required_views
    ) == (("BTC", 60), ("BTC", 240), ("ETH", 60), ("ETH", 240))
    assert packet.trigger_ids == ("delta-1", "delta-2")
    encoded = canonical_json(packet)
    assert '"bars"' not in encoded
    assert "<b>" not in encoded
    assert len(encoded) < 12_000


def test_packet_hash_is_independent_of_input_collection_order(
    app_config, replay_input
) -> None:
    builder, first = _packet(app_config, replay_input)
    market_btc = replay_input.market
    market_eth = replay_input.market.model_copy(update={"symbol": "ETHUSDT"})
    feature_btc = FeatureEngine(app_config.feature).compute(market_btc)
    feature_eth = feature_btc.model_copy(update={"symbol": "ETHUSDT"})

    second = builder.build(
        mandate=_mandate(),
        state=_state(
            market_btc.as_of,
            account=replay_input.account,
            markets=(market_btc, market_eth),
            features=(feature_btc, feature_eth),
        ),
        deltas=(
            _delta(market_btc.as_of, delta_id="delta-1", seconds=1),
            _delta(market_btc.as_of, delta_id="delta-2"),
        ),
        facts=(_fact(market_btc.as_of),),
        account=replay_input.account,
        markets=(market_btc, market_eth),
        features=(feature_btc, feature_eth),
    )

    assert second.content_hash == first.content_hash
    assert second.packet_id == first.packet_id


def test_packet_rejects_content_tampering_during_recovery(
    app_config, replay_input
) -> None:
    _, packet = _packet(app_config, replay_input)
    payload = packet.model_dump(mode="json")
    payload["question"] = "tampered after persistence"

    with pytest.raises(ValueError, match="content_hash"):
        DecisionPacket.model_validate(payload)


def test_packet_rejects_trigger_refs_that_do_not_match_deltas(
    app_config, replay_input
) -> None:
    _, packet = _packet(app_config, replay_input)
    payload = packet.model_dump(mode="json")
    payload["trigger_ids"] = ["different-delta"]

    with pytest.raises(ValueError, match="trigger_ids 与 deltas"):
        DecisionPacket.model_validate(payload)


def test_packet_rejects_market_replacement_for_frozen_state(
    app_config, replay_input
) -> None:
    builder, _ = _packet(app_config, replay_input)
    market_btc = replay_input.market
    market_eth = market_btc.model_copy(update={"symbol": "ETHUSDT"})
    feature_btc = FeatureEngine(app_config.feature).compute(market_btc)
    feature_eth = feature_btc.model_copy(update={"symbol": "ETHUSDT"})
    state = _state(
        market_btc.as_of,
        account=replay_input.account,
        markets=(market_btc, market_eth),
        features=(feature_btc, feature_eth),
    )
    replacement = market_btc.model_copy(update={"last": market_btc.last + 1})

    with pytest.raises(ValueError, match="market_snapshot_refs"):
        builder.build(
            mandate=_mandate(),
            state=state,
            deltas=(_delta(market_btc.as_of),),
            facts=(_fact(market_btc.as_of),),
            account=replay_input.account,
            markets=(replacement, market_eth),
            features=(feature_btc, feature_eth),
        )


def test_direct_fact_cannot_be_silently_truncated(app_config, replay_input) -> None:
    market = replay_input.market
    oversized = _fact(market.as_of)
    oversized = oversized.model_copy(
        update={
            "fact": oversized.fact.model_copy(
                update={"claim": "x" * 2_000},
            )
        }
    )
    builder = DecisionPacketBuilder(
        DecisionPacketPolicy(
            version="packet-policy-v1",
            schema_version="decision-packet-v1",
            maximum_fact_characters=500,
            maximum_characters_per_fact=600,
        )
    )

    with pytest.raises(DecisionPacketCapacityError, match="direct facts"):
        features = (FeatureEngine(app_config.feature).compute(market),)
        builder.build(
            mandate=AnalysisMandate(
                version="mandate-v1",
                analysis_scope="crypto-risk",
                question="Assess the event.",
                assets=(
                    MandateAsset(
                        asset="BTC",
                        market_symbol="BTCUSDT",
                        horizons_minutes=(60,),
                    ),
                ),
                required_risk_factors=("REGULATION",),
            ),
            state=_state(
                market.as_of,
                account=replay_input.account,
                markets=(market,),
                features=features,
            ),
            deltas=(_delta(market.as_of),),
            facts=(oversized,),
            account=replay_input.account,
            markets=(market,),
            features=features,
        )


def test_assess_schema_has_no_trade_action_fields(app_config, replay_input) -> None:
    _, packet = _packet(app_config, replay_input)
    schema = canonical_json(AssessStructuredOutput.model_json_schema())
    prompt = build_assess_prompt(packet)

    assert "suggested_action" not in schema
    assert "order_type" not in schema
    assert "target_notional" not in schema
    assert "decision_packet_json=" in prompt


def test_finalize_assessment_binds_authoritative_runtime_metadata(
    app_config, replay_input
) -> None:
    _, packet = _packet(app_config, replay_input)
    available_at = packet.as_of + timedelta(seconds=20)

    assessment = finalize_context_assessment(
        output=_assessment_output(),
        packet=packet,
        analysis_behavior_hash=HASH,
        available_at=available_at,
    )

    assert assessment.available_at == available_at
    assert assessment.decision_packet_hash == packet.content_hash
    assert assessment.trigger_ids == packet.trigger_ids
    assert len(assessment.views) == 4


def test_finalize_assessment_rejects_unknown_evidence(
    app_config, replay_input
) -> None:
    _, packet = _packet(app_config, replay_input)
    payload = _assessment_output().model_dump()
    payload["assessment"]["views"][0]["evidence_ids"] = ("not-visible",)
    output = AssessStructuredOutput.model_validate(payload)

    with pytest.raises(ValueError, match="不可见证据"):
        finalize_context_assessment(
            output=output,
            packet=packet,
            analysis_behavior_hash=HASH,
            available_at=packet.as_of + timedelta(seconds=20),
        )


def test_finalize_assessment_rejects_missing_required_view(
    app_config, replay_input
) -> None:
    _, packet = _packet(app_config, replay_input)
    payload = _assessment_output().model_dump()
    payload["assessment"]["views"] = payload["assessment"]["views"][:-1]
    output = AssessStructuredOutput.model_validate(payload)

    with pytest.raises(ValueError, match="required_views 不一致"):
        finalize_context_assessment(
            output=output,
            packet=packet,
            analysis_behavior_hash=HASH,
            available_at=packet.as_of + timedelta(seconds=20),
        )


def test_assessment_output_rejects_smuggled_order(replay_input) -> None:
    payload = _assessment_output().model_dump()
    payload["assessment"]["order_type"] = "MARKET"

    with pytest.raises(ValidationError, match="extra_forbidden"):
        AssessStructuredOutput.model_validate(payload)


class _StaticRouter:
    def __init__(self, result: AnalystResult) -> None:
        self.result = result
        self.bundles = []

    def run(self, bundle):
        self.bundles.append(bundle)
        return self.result


def _assess_bundle_builder(app_config) -> AssessRunBundleBuilder:
    return AssessRunBundleBuilder(
        app_config.codex_runtime,
        code_version="test-code",
        configuration_hash=HASH,
    )


def test_assess_bundle_reuses_generic_locked_runner_contract(
    app_config, replay_input, tmp_path
) -> None:
    _, packet = _packet(app_config, replay_input)
    bundle = _assess_bundle_builder(app_config).build(packet, tmp_path / "bundle")

    assert verify_bundle(bundle)
    assert bundle.cycle_id == packet.packet_id
    assert bundle.analysis_behavior_hash == _assess_bundle_builder(
        app_config
    ).behavior_hash(packet)
    schema = (bundle.path / "output.schema.json").read_text(encoding="utf-8")
    assert "suggested_action" not in schema
    assert "target_notional" not in schema


def test_context_analyst_finalizes_assessment_without_trade_authority(
    app_config, replay_input, tmp_path
) -> None:
    _, packet = _packet(app_config, replay_input)
    completed_at = packet.as_of + timedelta(seconds=20)
    router = _StaticRouter(
        AnalystResult(
            True,
            _assessment_output(),
            "CODEX_ANALYSIS_SUCCEEDED",
            ".codex",
            1,
            {"total_tokens": 100},
            completed_at,
            "run-1",
        )
    )
    analyst = CodexContextAnalyst(
        tmp_path,
        _assess_bundle_builder(app_config),
        router,
    )

    result = analyst.assess(packet)

    assert result.success
    assert result.output is not None
    assert result.output.available_at == completed_at
    assert result.output.decision_packet_hash == packet.content_hash
    assert len(router.bundles) == 1


def test_context_analyst_fails_closed_on_semantically_invalid_output(
    app_config, replay_input, tmp_path
) -> None:
    _, packet = _packet(app_config, replay_input)
    payload = _assessment_output().model_dump()
    payload["assessment"]["views"][0]["evidence_ids"] = ("not-visible",)
    router = _StaticRouter(
        AnalystResult(
            True,
            AssessStructuredOutput.model_validate(payload),
            "CODEX_ANALYSIS_SUCCEEDED",
            completed_at=packet.as_of + timedelta(seconds=20),
        )
    )
    analyst = CodexContextAnalyst(
        tmp_path,
        _assess_bundle_builder(app_config),
        router,
    )

    result = analyst.assess(packet)

    assert not result.success
    assert result.output is None
    assert result.reason_code == "CODEX_SCHEMA_INVALID"


def test_context_assessment_store_is_immutable_and_idempotent(
    app_config, replay_input
) -> None:
    _, packet = _packet(app_config, replay_input)
    assessment = finalize_context_assessment(
        output=_assessment_output(),
        packet=packet,
        analysis_behavior_hash=HASH,
        available_at=packet.as_of + timedelta(seconds=20),
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    metadata.create_all(engine)
    store = SqlContextAssessmentStore(engine)

    assert store.record_packet(packet) == packet
    assert store.record_packet(packet) == packet
    assert store.record_assessment(packet.packet_id, assessment) == assessment
    assert store.record_assessment(packet.packet_id, assessment) == assessment
    assert store.packet(packet.packet_id) == packet
    assert store.assessment(assessment.assessment_id) == assessment
    assert store.assessment_for(
        packet_id=packet.packet_id,
        analysis_behavior_hash=assessment.analysis_behavior_hash,
    ) == assessment


def test_context_assessment_store_rejects_second_output_for_same_behavior(
    app_config, replay_input
) -> None:
    _, packet = _packet(app_config, replay_input)
    first = finalize_context_assessment(
        output=_assessment_output(),
        packet=packet,
        analysis_behavior_hash=HASH,
        available_at=packet.as_of + timedelta(seconds=20),
    )
    retry = finalize_context_assessment(
        output=_assessment_output(),
        packet=packet,
        analysis_behavior_hash=HASH,
        available_at=packet.as_of + timedelta(seconds=30),
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    metadata.create_all(engine)
    store = SqlContextAssessmentStore(engine)
    store.record_packet(packet)
    store.record_assessment(packet.packet_id, first)

    with pytest.raises(ValueError, match="已有不同的权威"):
        store.record_assessment(packet.packet_id, retry)

    assert store.assessment_for(
        packet_id=packet.packet_id,
        analysis_behavior_hash=HASH,
    ) == first


def test_context_assessment_store_rejects_packet_mismatch(
    app_config, replay_input
) -> None:
    _, packet = _packet(app_config, replay_input)
    assessment = finalize_context_assessment(
        output=_assessment_output(),
        packet=packet,
        analysis_behavior_hash=HASH,
        available_at=packet.as_of + timedelta(seconds=20),
    ).model_copy(update={"decision_packet_hash": "b" * 64})
    engine = create_engine("sqlite+pysqlite:///:memory:")
    metadata.create_all(engine)
    store = SqlContextAssessmentStore(engine)
    store.record_packet(packet)

    with pytest.raises(ValueError, match="身份不一致"):
        store.record_assessment(packet.packet_id, assessment)
