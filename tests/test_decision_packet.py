import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine

from investment_manager.decision_cycle.trigger import _previous_context
from investment_manager.forecast.codex.bundle import verify_bundle
from investment_manager.forecast.codex.router import AnalystResult
from investment_manager.forecast.context.analyst import (
    AssessRunBundleBuilder,
    CodexContextAnalyst,
    assess_output_schema,
    configured_assess_behavior_hash,
)
from investment_manager.forecast.context.contract import (
    ContextAssessmentContractError,
    ContextMechanismDraft,
    ContextVerificationTestDraft,
    WorldModelDraft,
    WorldModelStructuredOutput,
    assessment_available_feature_selectors,
    assessment_current_evidence_ids,
    assessment_visible_evidence_ids,
    assessment_world_model_evidence_ids,
    build_assess_prompt,
    finalize_world_model,
)
from investment_manager.forecast.context.repository import SqlContextAssessmentStore
from investment_manager.forecast.context.verification import packet_feature_values
from investment_manager.forecast.models import (
    MAX_ACTIVE_WORLD_MECHANISMS,
    MAX_WORLD_CAUSAL_NODES,
    MAX_WORLD_INVALIDATION_CONDITIONS,
    MAX_WORLD_VERIFICATION_TESTS,
    ContextCausalNode,
    ContextMechanismRelationship,
    ContextMechanismRetirement,
    ContextPredicateOperator,
    ContextTransmissionStage,
    ContextVerificationPredicate,
)
from investment_manager.information.aggregated_flows import (
    BTC_ETF_AGGREGATE_FLOW_FACT_TYPE,
)
from investment_manager.information.models import (
    CausalDomain,
    CoverageStatus,
    DomainCoverageSnapshot,
    IntelligenceEvent,
    SourceTier,
)
from investment_manager.kernel.identity import canonical_json, content_hash
from investment_manager.market.features import FeatureEngine
from investment_manager.schema import create_schema
from investment_manager.state.decision.packet import (
    AnalysisMandate,
    DecisionPacket,
    DecisionPacketBuilder,
    DecisionPacketCapacityError,
    MandateExposure,
    ObservationAsset,
    PacketDerivativeState,
    PacketPreviousCausalNode,
    PacketPreviousContext,
    PacketPreviousEventReference,
    PacketPreviousMechanism,
    PacketPreviousVerificationObservation,
    PacketPreviousVerificationPredicate,
    PacketPreviousVerificationTest,
    PacketReviewRequest,
    VisibleFact,
    continuous_fact_numeric_values,
    decision_packet_analysis_projection,
    replace_packet_previous_context,
)
from investment_manager.state.facts import (
    FED_MONETARY_RELEASE_FACT_TYPE,
    TREASURY_BUYBACK_OPERATION_FACT_TYPE,
    TREASURY_BUYBACK_RESULT_FACT_TYPE,
)
from investment_manager.state.models import (
    CanonicalFactRevision,
    DeltaCategory,
    FactDecisionMateriality,
    FactRevisionStatus,
    MaterialDelta,
    Materiality,
    StateSnapshot,
)
from investment_manager.state.policy import DecisionPacketPolicy

HASH = "a" * 64
TEST_MANDATE_EXPOSURES = (MandateExposure(economic_exposure="CRYPTO_NETWORK", asset="BTC"),)


def _mandate() -> AnalysisMandate:
    return AnalysisMandate(
        version="mandate-v1",
        analysis_scope="crypto-risk",
        question="Assess material changes across the crypto portfolio.",
        mandate_exposures=TEST_MANDATE_EXPOSURES,
        observation_assets=(
            ObservationAsset(
                asset="BTC",
                market_symbol="BTCUSDT",
                horizons_minutes=(60, 240),
            ),
            ObservationAsset(
                asset="ETH",
                market_symbol="ETHUSDT",
                horizons_minutes=(60, 240),
            ),
        ),
        required_risk_factors=("REGULATION",),
    )


def _fact(
    as_of,
    *,
    revision_id: str = "revision-1",
    fact_id: str = "fact-1",
    event_time=None,
    observation_id: str = "obs-1",
) -> VisibleFact:
    return VisibleFact(
        fact=CanonicalFactRevision(
            fact_id=fact_id,
            revision_id=revision_id,
            projection_version="fact-projection-v1",
            fact_type="REGULATORY_EVENT",
            status=FactRevisionStatus.ACTIVE,
            event_time=event_time or as_of + timedelta(hours=1),
            observed_at=as_of - timedelta(minutes=1),
            headline="<b>CFTC meeting</b>",
            claim="Official schedule update.",
            affected_assets=("BTC", "ETH"),
            risk_factors=("REGULATION",),
            source_observation_ids=(observation_id,),
            revision_hash=HASH,
        ),
        highest_source_tier=SourceTier.FIRST_PARTY,
        independent_source_count=1,
    )


def _state(
    as_of,
    *,
    account,
    markets,
    features,
    intelligence_events: tuple[IntelligenceEvent, ...] = (),
) -> StateSnapshot:
    return StateSnapshot(
        state_id="state-1",
        projection_version="state-projection-v1",
        analysis_scope="crypto-risk",
        as_of=as_of,
        built_at=as_of,
        fact_revision_ids=("revision-1",),
        market_snapshot_refs=tuple(sorted(content_hash(item) for item in markets)),
        feature_snapshot_refs=tuple(sorted(content_hash(item) for item in features)),
        intelligence_event_refs=tuple(sorted(content_hash(item) for item in intelligence_events)),
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


def _packet(
    app_config,
    replay_input,
    *,
    previous_context=None,
    intelligence_events: tuple[IntelligenceEvent, ...] = (),
    review_requests: tuple[PacketReviewRequest, ...] = (),
    as_of=None,
    packet_schema_version: str = "decision-packet-v1",
    mandate: AnalysisMandate | None = None,
):
    market_btc = replay_input.market
    state_as_of = market_btc.as_of if as_of is None else as_of
    market_eth = replay_input.market.model_copy(update={"symbol": "ETHUSDT"})
    feature_btc = FeatureEngine(app_config.feature).compute(market_btc)
    feature_eth = feature_btc.model_copy(update={"symbol": "ETHUSDT"})
    builder = DecisionPacketBuilder(
        DecisionPacketPolicy(
            version="packet-policy-v1",
            schema_version=packet_schema_version,
        )
    )
    packet = builder.build(
        mandate=mandate or _mandate(),
        state=_state(
            state_as_of,
            account=replay_input.account,
            markets=(market_btc, market_eth),
            features=(feature_btc, feature_eth),
            intelligence_events=intelligence_events,
        ),
        deltas=(
            _delta(state_as_of, delta_id="delta-2"),
            _delta(state_as_of, delta_id="delta-1", seconds=1),
        ),
        facts=(_fact(state_as_of),),
        intelligence_events=intelligence_events,
        review_requests=review_requests,
        account=replay_input.account,
        markets=(market_eth, market_btc),
        features=(feature_eth, feature_btc),
        previous_context=previous_context,
    )
    return builder, packet


def _world_model_output() -> WorldModelStructuredOutput:
    return WorldModelStructuredOutput(
        world_model=WorldModelDraft(
            synthesis=(
                "监管日程变化提供外生风险溢价输入，市场响应尚在传导，"
                "当前判断的主要反转风险是后续正式结果否定该路径。"
            ),
            synthesis_horizon_hours=168,
            mechanisms=(
                ContextMechanismDraft(
                    relationship=ContextMechanismRelationship.SUPPORTS,
                    claim="监管日程变化正在通过风险溢价影响加密资产定价。",
                    horizon_hours=168,
                    causal_chain=(
                        ContextCausalNode(
                            statement="官方监管日程发生了可验证变化。",
                            evidence_ids=("revision-1",),
                        ),
                        ContextCausalNode(
                            statement="市场材料变化提供了传导观察锚点。",
                            evidence_ids=("delta-1",),
                        ),
                    ),
                    transmission_stage=ContextTransmissionStage.PROPAGATING,
                    verification_tests=(
                        ContextVerificationTestDraft(
                            feature_selector="asset_state:BTC.return_fraction",
                            evaluation_window_minutes=240,
                            supports_predicate=ContextVerificationPredicate(
                                operator=ContextPredicateOperator.GT,
                                value=Decimal("0"),
                            ),
                            contradicts_predicate=ContextVerificationPredicate(
                                operator=ContextPredicateOperator.LT,
                                value=Decimal("0"),
                            ),
                        ),
                    ),
                    invalidation_conditions=("正式结果撤回或市场响应反转",),
                    next_review_at=datetime(2026, 8, 18, 18, tzinfo=UTC),
                ),
            ),
        )
    )


def _previous_world_model(as_of: datetime, *, assessment_id: str) -> PacketPreviousContext:
    predicate = PacketPreviousVerificationPredicate(operator="GT", value=Decimal("0"))
    return PacketPreviousContext(
        assessment_id=assessment_id,
        analysis_scope="crypto-risk",
        mandate_version="mandate-v1",
        analysis_behavior_hash="a" * 64,
        decision_packet_hash="b" * 64,
        as_of=as_of - timedelta(hours=1),
        available_at=as_of - timedelta(minutes=59),
        synthesis="上一轮仍有效的联合因果基准。",
        synthesis_horizon_hours=24,
        mechanisms=(
            PacketPreviousMechanism(
                mechanism_id=f"{assessment_id}-mechanism",
                relationship="SUPPORTS",
                claim="上一轮主导机制仍需由当前证据复核。",
                horizon_hours=24,
                causal_chain=(
                    PacketPreviousCausalNode(statement="原因端。", evidence_ids=("old-1",)),
                    PacketPreviousCausalNode(statement="响应端。", evidence_ids=("old-2",)),
                ),
                transmission_stage="PROPAGATING",
                verification_tests=(
                    PacketPreviousVerificationTest(
                        feature_selector="asset_state:BTC.return_fraction",
                        evaluation_window_minutes=60,
                        supports_predicate=predicate,
                        contradicts_predicate=predicate.model_copy(update={"operator": "LTE"}),
                    ),
                ),
                invalidation_conditions=("当前证据否定传导",),
                next_review_at=as_of + timedelta(hours=1),
            ),
        ),
    )


def test_packet_is_one_multi_asset_high_density_projection(app_config, replay_input) -> None:
    _, packet = _packet(app_config, replay_input)

    assert tuple(item.asset for item in packet.asset_states) == ("BTC", "ETH")
    assert tuple((item.asset, item.horizon_minutes) for item in packet.required_views) == (
        ("BTC", 60),
        ("BTC", 240),
        ("ETH", 60),
        ("ETH", 240),
    )
    assert packet.trigger_ids == ("delta-1", "delta-2")
    encoded = canonical_json(packet)
    assert '"bars"' not in encoded
    assert "<b>" not in encoded
    assert len(encoded) < 12_000


def test_packet_hash_is_independent_of_input_collection_order(app_config, replay_input) -> None:
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


def test_packet_rejects_content_tampering_during_recovery(app_config, replay_input) -> None:
    _, packet = _packet(app_config, replay_input)
    payload = packet.model_dump(mode="json")
    payload["question"] = "tampered after persistence"

    with pytest.raises(ValueError, match="content_hash"):
        DecisionPacket.model_validate(payload)


def test_packet_recovers_legacy_hash_without_default_review_field(app_config, replay_input) -> None:
    _, packet = _packet(app_config, replay_input)
    payload = packet.model_dump(mode="json")
    payload.pop("review_requests")

    assert DecisionPacket.model_validate(payload) == packet


def test_packet_requires_mandate_exposures_from_schema_v20_onward(
    app_config,
    replay_input,
) -> None:
    _, current = _packet(app_config, replay_input)
    content = {
        name: getattr(current, name)
        for name in DecisionPacket.model_fields
        if name not in {"packet_id", "content_hash", "mandate_exposures", "schema_version"}
    }
    historical = DecisionPacket.create(
        **content,
        schema_version="decision-packet-v19",
    )
    payload = historical.model_dump(mode="json")
    payload.pop("mandate_exposures")
    assert DecisionPacket.model_validate(payload) == historical

    payload["schema_version"] = "decision-packet-v20"
    with pytest.raises(ValueError, match="必须冻结 Capital mandate exposures"):
        DecisionPacket.model_validate(payload)


def test_packet_rejects_trigger_refs_that_do_not_match_deltas(app_config, replay_input) -> None:
    _, packet = _packet(app_config, replay_input)
    payload = packet.model_dump(mode="json")
    payload["trigger_ids"] = ["different-delta"]

    with pytest.raises(ValueError, match="trigger_ids 与分析原因"):
        DecisionPacket.model_validate(payload)


def test_packet_can_be_driven_by_an_explicit_review_without_fake_delta(
    app_config, replay_input
) -> None:
    builder, _ = _packet(app_config, replay_input)
    market_btc = replay_input.market
    market_eth = market_btc.model_copy(update={"symbol": "ETHUSDT"})
    feature_btc = FeatureEngine(app_config.feature).compute(market_btc)
    feature_eth = feature_btc.model_copy(update={"symbol": "ETHUSDT"})
    review = PacketReviewRequest.create(
        requested_at=market_btc.as_of,
        reason="FOMC 会前立即复核风险倾向",
        evidence_ids=("revision-1",),
    )

    packet = builder.build(
        mandate=_mandate(),
        state=_state(
            market_btc.as_of,
            account=replay_input.account,
            markets=(market_btc, market_eth),
            features=(feature_btc, feature_eth),
        ),
        deltas=(),
        review_requests=(review,),
        facts=(_fact(market_btc.as_of),),
        account=replay_input.account,
        markets=(market_btc, market_eth),
        features=(feature_btc, feature_eth),
    )

    assert packet.deltas == ()
    assert packet.review_requests == (review,)
    assert packet.trigger_ids == (review.review_id,)
    assert packet.facts[0].directly_triggered is True
    assert review.reason in canonical_json(packet)


def test_packet_rejects_market_replacement_for_frozen_state(app_config, replay_input) -> None:
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
                mandate_exposures=TEST_MANDATE_EXPOSURES,
                observation_assets=(
                    ObservationAsset(
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


def test_packet_evicts_low_priority_background_facts_to_fit_total_capacity(
    app_config,
    replay_input,
) -> None:
    market = replay_input.market
    features = (FeatureEngine(app_config.feature).compute(market),)
    facts = tuple(
        _fact(market.as_of, revision_id=f"revision-{index}").model_copy(
            update={
                "fact": _fact(
                    market.as_of,
                    revision_id=f"revision-{index}",
                ).fact.model_copy(
                    update={
                        "fact_id": f"fact-{index}",
                        "claim": f"background-{index}-" + "x" * 500,
                        "event_time": market.as_of + timedelta(hours=index),
                        "source_observation_ids": (f"obs-{index}",),
                    }
                )
            }
        )
        for index in range(1, 11)
    )
    continuous = facts[-1].model_copy(
        update={
            "fact": facts[-1].fact.model_copy(
                update={
                    "fact_type": "US_TREASURY_CASH_SNAPSHOT",
                    "claim": (
                        "tga_change_5d_usd_m=-25000 USD_MILLIONS; "
                        "tga_balance_usd_m=925000 USD_MILLIONS"
                    ),
                }
            )
        }
    )
    facts = (*facts[:-1], continuous)
    state = _state(
        market.as_of,
        account=replay_input.account,
        markets=(market,),
        features=features,
    ).model_copy(update={"fact_revision_ids": tuple(item.fact.revision_id for item in facts)})
    packet = DecisionPacketBuilder(
        DecisionPacketPolicy(
            version="packet-policy-v1",
            schema_version="decision-packet-v1",
            maximum_fact_characters=10_000,
            maximum_characters_per_fact=600,
            maximum_packet_characters=6_000,
        )
    ).build(
        mandate=AnalysisMandate(
            version="mandate-v1",
            analysis_scope="crypto-risk",
            question="Assess the event.",
            mandate_exposures=TEST_MANDATE_EXPOSURES,
            observation_assets=(
                ObservationAsset(
                    asset="BTC",
                    market_symbol="BTCUSDT",
                    horizons_minutes=(60,),
                ),
            ),
            required_risk_factors=("REGULATION",),
        ),
        state=state,
        deltas=(_delta(market.as_of),),
        facts=facts,
        account=replay_input.account,
        markets=(market,),
        features=features,
    )

    projected_length = len(canonical_json(decision_packet_analysis_projection(packet)))
    assert projected_length <= 6_000
    assert len(canonical_json(packet)) > projected_length
    assert packet.facts[0].revision_id == "revision-1"
    assert packet.facts[0].directly_triggered is True
    assert "revision-10" in {item.revision_id for item in packet.facts}
    assert packet.omitted_fact_revision_ids
    assert set(packet.omitted_fact_revision_ids).isdisjoint(
        item.revision_id for item in packet.facts
    )


def test_packet_keeps_direct_event_and_causal_coverage_when_state_is_redundant(
    app_config,
    replay_input,
) -> None:
    market = replay_input.market
    features = (FeatureEngine(app_config.feature).compute(market),)
    direct = _fact(market.as_of)

    def continuous(
        revision_id: str,
        fact_id: str,
        fact_type: str,
        claim: str,
        risk_factors: tuple[str, ...],
    ) -> VisibleFact:
        base = _fact(
            market.as_of,
            revision_id=revision_id,
            fact_id=fact_id,
            observation_id=f"obs-{revision_id}",
        )
        return base.model_copy(
            update={
                "fact": base.fact.model_copy(
                    update={
                        "fact_type": fact_type,
                        "claim": claim,
                        "risk_factors": risk_factors,
                    }
                )
            }
        )

    facts = (
        direct,
        continuous(
            "revision-dollar",
            "fact-dollar",
            "FED_BROAD_DOLLAR_SNAPSHOT",
            "broad_dollar_index=121.4 INDEX; broad_dollar_change_5d_pct=0.8 PERCENT",
            ("US_DOLLAR",),
        ),
        continuous(
            "revision-arkb",
            "fact-arkb",
            "ARKB_HOLDINGS_SNAPSHOT",
            "arkb_btc_holdings=42000 BTC; arkb_btc_holdings_change_1d=100 BTC",
            ("BTC_INSTITUTIONAL_HOLDINGS",),
        ),
        continuous(
            "revision-ibit",
            "fact-ibit",
            "IBIT_HOLDINGS_SNAPSHOT",
            "ibit_btc_holdings=740000 BTC; ibit_btc_holdings_change_1d=250 BTC",
            ("BTC_INSTITUTIONAL_HOLDINGS",),
        ),
    )
    event = IntelligenceEvent(
        evidence_id="direct-event",
        normalizer_version="test-normalizer-v1",
        acquisition_route="test-first-party",
        event_time=market.as_of - timedelta(minutes=1),
        observed_at=market.as_of,
        source="official-source",
        title="官方突发事件",
        body="新的官方信息需要立即结合宏观基线重新评估。" * 12,
        symbols=("BTCUSDT",),
        relevance=Decimal("1"),
        impact=Decimal("1"),
        source_reliability=Decimal("1"),
        novelty=Decimal("1"),
    )
    event_ref = content_hash(event)
    state = _state(
        market.as_of,
        account=replay_input.account,
        markets=(market,),
        features=features,
        intelligence_events=(event,),
    ).model_copy(update={"fact_revision_ids": tuple(item.fact.revision_id for item in facts)})
    delta = _delta(market.as_of).model_copy(update={"intelligence_event_refs": (event_ref,)})

    def build(
        maximum_packet_characters: int,
        *,
        previous_context: PacketPreviousContext | None = None,
        maximum_intelligence_characters: int = 3_000,
    ) -> DecisionPacket:
        return DecisionPacketBuilder(
            DecisionPacketPolicy(
                version="packet-policy-v1",
                schema_version="decision-packet-v1",
                maximum_fact_characters=10_000,
                maximum_characters_per_fact=1_200,
                maximum_packet_characters=maximum_packet_characters,
                maximum_intelligence_characters=maximum_intelligence_characters,
            )
        ).build(
            mandate=AnalysisMandate(
                version="mandate-v1",
                analysis_scope="crypto-risk",
                question="Assess the event against the causal baseline.",
                mandate_exposures=TEST_MANDATE_EXPOSURES,
                observation_assets=(
                    ObservationAsset(
                        asset="BTC",
                        market_symbol="BTCUSDT",
                        horizons_minutes=(60,),
                    ),
                ),
                required_risk_factors=("REGULATION",),
            ),
            state=state,
            deltas=(delta,),
            facts=facts,
            intelligence_events=(event,),
            account=replay_input.account,
            markets=(market,),
            features=features,
            previous_context=previous_context,
        )

    full = build(16_000)
    full_size = len(canonical_json(decision_packet_analysis_projection(full)))
    assert full_size > 2_000
    constrained = build(full_size - 1)

    assert constrained.intelligence_events[0].evidence_ref == event_ref
    assert constrained.intelligence_events[0].directly_triggered
    assert constrained.facts[0].revision_id == direct.fact.revision_id
    assert constrained.facts[0].directly_triggered
    continuous_facts = constrained.facts[1:]
    assert {risk for item in continuous_facts for risk in item.risk_factors} == {
        "US_DOLLAR",
        "BTC_INSTITUTIONAL_HOLDINGS",
    }
    assert len(continuous_facts) == 2
    assert len(constrained.omitted_fact_revision_ids) == 1

    previous = _previous_world_model(
        market.as_of,
        assessment_id="assessment-capacity-refreeze",
    )
    inherited = build(16_000, previous_context=previous)
    inherited_size = len(canonical_json(decision_packet_analysis_projection(inherited)))
    refrozen = replace_packet_previous_context(
        inherited,
        previous,
        maximum_analysis_characters=inherited_size - 1,
    )

    assert len(canonical_json(decision_packet_analysis_projection(refrozen))) <= inherited_size - 1
    assert refrozen.intelligence_events[0].directly_triggered
    assert refrozen.facts[0].directly_triggered
    assert len(refrozen.facts) < len(inherited.facts)
    unique_baseline_size = len(canonical_json(decision_packet_analysis_projection(refrozen)))
    with pytest.raises(
        DecisionPacketCapacityError,
        match="final verified projection exceeds",
    ):
        replace_packet_previous_context(
            refrozen,
            previous,
            maximum_analysis_characters=unique_baseline_size - 1,
        )

    with pytest.raises(
        DecisionPacketCapacityError,
        match="direct intelligence events exceed intelligence capacity",
    ):
        build(16_000, maximum_intelligence_characters=100)


def test_packet_omits_temporally_distant_background_facts_but_keeps_direct_fact(
    app_config,
    replay_input,
) -> None:
    market = replay_input.market
    features = (FeatureEngine(app_config.feature).compute(market),)
    recent = _fact(market.as_of, revision_id="revision-recent")
    distant = _fact(
        market.as_of,
        revision_id="revision-distant",
        fact_id="fact-distant",
        event_time=market.as_of - timedelta(days=30),
        observation_id="obs-distant",
    )
    direct = _fact(
        market.as_of,
        fact_id="fact-direct",
        event_time=market.as_of - timedelta(days=30),
        observation_id="obs-direct",
    )
    facts = (direct, distant, recent)
    state = _state(
        market.as_of,
        account=replay_input.account,
        markets=(market,),
        features=features,
    ).model_copy(update={"fact_revision_ids": tuple(item.fact.revision_id for item in facts)})
    packet = DecisionPacketBuilder(
        DecisionPacketPolicy(
            version="packet-policy-v2",
            schema_version="decision-packet-v1",
            maximum_background_fact_distance_seconds=86_400,
        )
    ).build(
        mandate=AnalysisMandate(
            version="mandate-v1",
            analysis_scope="crypto-risk",
            question="Assess the event.",
            mandate_exposures=TEST_MANDATE_EXPOSURES,
            observation_assets=(
                ObservationAsset(
                    asset="BTC",
                    market_symbol="BTCUSDT",
                    horizons_minutes=(60,),
                ),
            ),
            required_risk_factors=("REGULATION",),
        ),
        state=state,
        deltas=(_delta(market.as_of),),
        facts=facts,
        account=replay_input.account,
        markets=(market,),
        features=features,
    )

    assert tuple(item.revision_id for item in packet.facts) == (
        "revision-1",
        "revision-recent",
    )
    assert packet.facts[0].directly_triggered is True
    assert packet.omitted_fact_revision_ids == ("revision-distant",)


def test_packet_keeps_latest_continuous_official_metric_beyond_event_window(
    app_config,
    replay_input,
) -> None:
    market = replay_input.market
    features = (FeatureEngine(app_config.feature).compute(market),)
    direct = _fact(market.as_of)
    metric = _fact(
        market.as_of,
        revision_id="revision-tga",
        fact_id="fact-tga",
        event_time=market.as_of - timedelta(days=7),
        observation_id="obs-tga",
    )
    metric = metric.model_copy(
        update={"fact": metric.fact.model_copy(update={"fact_type": "US_TREASURY_CASH_SNAPSHOT"})}
    )
    facts = (direct, metric)
    state = _state(
        market.as_of,
        account=replay_input.account,
        markets=(market,),
        features=features,
    ).model_copy(update={"fact_revision_ids": tuple(item.fact.revision_id for item in facts)})

    packet = DecisionPacketBuilder(
        DecisionPacketPolicy(
            version="packet-policy-metric-v1",
            schema_version="decision-packet-v1",
            maximum_background_fact_distance_seconds=86_400,
        )
    ).build(
        mandate=AnalysisMandate(
            version="mandate-v1",
            analysis_scope="crypto-risk",
            question="Assess the event.",
            mandate_exposures=TEST_MANDATE_EXPOSURES,
            observation_assets=(
                ObservationAsset(
                    asset="BTC",
                    market_symbol="BTCUSDT",
                    horizons_minutes=(60,),
                ),
            ),
            required_risk_factors=("REGULATION",),
        ),
        state=state,
        deltas=(_delta(market.as_of),),
        facts=facts,
        account=replay_input.account,
        markets=(market,),
        features=features,
    )

    assert tuple(item.revision_id for item in packet.facts) == (
        "revision-1",
        "revision-tga",
    )
    assert packet.omitted_fact_revision_ids == ()
    projection = decision_packet_analysis_projection(packet)
    assert tuple(item["revision_id"] for item in projection["facts"]) == ("revision-1",)
    assert projection["state_features"] == {
        "algorithm_version": "decision-state-feature-v2",
        "regime_states": (
            {
                "type": "US_TREASURY_CASH_SNAPSHOT",
                "at": metric.fact.event_time.isoformat(),
                "state": metric.fact.claim.rstrip(". "),
                "ref": "revision-tga",
            },
        ),
        "flow_states": (),
        "financing_states": (),
        "policy_states": (),
    }
    numeric_metric = packet.facts[1].model_copy(
        update={
            "claim": (
                "effective_date=2026-08-16; tga_balance_usd_m=800000 USD_MILLIONS; "
                "tga_change_5d_usd_m=-31510 USD_MILLIONS."
            )
        }
    )
    numeric_packet = packet.model_copy(update={"facts": (packet.facts[0], numeric_metric)})

    assert continuous_fact_numeric_values(numeric_metric) == {
        "tga_balance_usd_m": Decimal("800000"),
        "tga_change_5d_usd_m": Decimal("-31510"),
    }
    selector = "fact_state:US_TREASURY_CASH_SNAPSHOT.tga_change_5d_usd_m"
    assert selector in assessment_available_feature_selectors(numeric_packet)
    assert packet_feature_values(numeric_packet)[selector] == Decimal("-31510")


def test_compact_state_refills_capacity_rejected_by_verbose_fact_budget(
    app_config,
    replay_input,
) -> None:
    market = replay_input.market
    features = (FeatureEngine(app_config.feature).compute(market),)
    direct = _fact(market.as_of)
    flow = _fact(
        market.as_of,
        revision_id="revision-etf-flow",
        fact_id="fact-etf-flow",
        event_time=market.as_of - timedelta(days=1),
        observation_id="obs-etf-flow",
    )
    flow = flow.model_copy(
        update={
            "fact": flow.fact.model_copy(
                update={
                    "fact_type": BTC_ETF_AGGREGATE_FLOW_FACT_TYPE,
                    "claim": (
                        "aggregator=ByKaranteli; effective_date=2026-08-25; "
                        "finalized_daily_net_inflow_usd_m=314.370 USD_MILLIONS; "
                        "net_assets_usd_m=99045.023 USD_MILLIONS; "
                        "cumulative_inflow_usd_m=54358.049 USD_MILLIONS; "
                        "value_traded_usd_m=3518.441 USD_MILLIONS; "
                        "absolute_flow_percentile=0.6667; sample_size=318; "
                        "lookback=2025-05-20..2026-08-25. "
                        "This is a finalized aggregate series and not a direction signal."
                    ),
                    "risk_factors": ("BTC_INSTITUTIONAL_FLOW",),
                    "decision_materiality": FactDecisionMateriality.BACKGROUND,
                }
            ),
            "highest_source_tier": SourceTier.AGGREGATOR,
        }
    )
    background = _fact(
        market.as_of,
        revision_id="revision-background",
        fact_id="fact-background",
        observation_id="obs-background",
    )
    background = background.model_copy(
        update={
            "fact": background.fact.model_copy(
                update={
                    "fact_type": "EXTERNAL_BACKGROUND_EVENT",
                    "claim": (
                        "这是低优先级背景材料，不应仅因为最终输入仍有空位而被重新加入。" * 30
                    ),
                    "risk_factors": ("EXTERNAL_INFORMATION",),
                    "decision_materiality": FactDecisionMateriality.BACKGROUND,
                }
            )
        }
    )
    facts = (direct, flow, background)
    state = _state(
        market.as_of,
        account=replay_input.account,
        markets=(market,),
        features=features,
    ).model_copy(update={"fact_revision_ids": tuple(item.fact.revision_id for item in facts)})

    packet = DecisionPacketBuilder(
        DecisionPacketPolicy(
            version="packet-policy-final-cost-v1",
            schema_version="decision-packet-v1",
            maximum_fact_characters=500,
            maximum_packet_characters=6_000,
        )
    ).build(
        mandate=AnalysisMandate(
            version="mandate-v1",
            analysis_scope="crypto-risk",
            question="Assess the event and current institutional flow.",
            mandate_exposures=TEST_MANDATE_EXPOSURES,
            observation_assets=(
                ObservationAsset(
                    asset="BTC",
                    market_symbol="BTCUSDT",
                    horizons_minutes=(60,),
                ),
            ),
            required_risk_factors=(
                "REGULATION",
                "BTC_INSTITUTIONAL_FLOW",
                "EXTERNAL_INFORMATION",
            ),
        ),
        state=state,
        deltas=(_delta(market.as_of),),
        facts=facts,
        account=replay_input.account,
        markets=(market,),
        features=features,
    )

    assert tuple(item.revision_id for item in packet.facts) == (
        "revision-1",
        "revision-etf-flow",
    )
    assert packet.omitted_fact_revision_ids == ("revision-background",)
    projection = decision_packet_analysis_projection(packet)
    assert tuple(item["type"] for item in projection["state_features"]["flow_states"]) == (
        BTC_ETF_AGGREGATE_FLOW_FACT_TYPE,
    )
    assert len(canonical_json(projection)) <= 6_000


def test_analysis_projection_separates_policy_and_financing_from_generic_facts(
    app_config,
    replay_input,
) -> None:
    _, packet = _packet(app_config, replay_input)
    generic = packet.facts[0]
    policy = generic.model_copy(
        update={
            "fact_id": "fact-fed-policy",
            "revision_id": "revision-fed-policy",
            "fact_type": FED_MONETARY_RELEASE_FACT_TYPE,
            "claim": (
                "action=The Committee decided to maintain the target range; "
                "expectations=Market pricing implied a later adjustment; "
                "constraints=Inflation remained elevated; "
                "path=Further tightening would likely be necessary if inflation persisted"
            ),
        }
    )
    financing = generic.model_copy(
        update={
            "fact_id": "fact-auction",
            "revision_id": "revision-auction",
            "fact_type": "US_TREASURY_AUCTION_ABSORPTION_SNAPSHOT",
            "claim": (
                "effective_date=2026-08-18; "
                "treasury_bill_offering_14d_usd_m=1096000 USD_MILLIONS; "
                "treasury_coupon_offering_14d_usd_m=149000 USD_MILLIONS; "
                "treasury_coupon_bid_to_cover=2.59 INDEX; "
                "treasury_coupon_direct_share_pct=20.3 PERCENT; "
                "treasury_coupon_indirect_share_pct=68.6 PERCENT; "
                "treasury_coupon_primary_dealer_share_pct=10.3 PERCENT; "
                "treasury_coupon_soma_addon_14d_usd_m=34703 USD_MILLIONS."
            ),
        }
    )
    projection = decision_packet_analysis_projection(
        packet.model_copy(update={"facts": (generic, policy, financing)})
    )

    assert tuple(item["revision_id"] for item in projection["facts"]) == (generic.revision_id,)
    assert projection["state_features"]["policy_states"] == (
        {
            "type": FED_MONETARY_RELEASE_FACT_TYPE,
            "at": policy.event_time.isoformat(),
            "document": policy.headline,
            "state": policy.claim,
            "ref": "revision-fed-policy",
        },
    )
    assert projection["state_features"]["financing_states"] == (
        {
            "type": "US_TREASURY_AUCTION_ABSORPTION_SNAPSHOT",
            "at": financing.event_time.isoformat(),
            "state": (
                "treasury_bill_offering_14d_usd_m=1096000; "
                "treasury_coupon_offering_14d_usd_m=149000; "
                "treasury_coupon_bid_to_cover=2.59; "
                "treasury_coupon_direct_share_pct=20.3; "
                "treasury_coupon_indirect_share_pct=68.6; "
                "treasury_coupon_primary_dealer_share_pct=10.3; "
                "treasury_coupon_soma_addon_14d_usd_m=34703"
            ),
            "ref": "revision-auction",
        },
    )


def test_packet_keeps_treasury_calendar_context_beyond_event_window(
    app_config,
    replay_input,
) -> None:
    market = replay_input.market
    features = (FeatureEngine(app_config.feature).compute(market),)
    direct = _fact(market.as_of)
    calendar = _fact(
        market.as_of,
        revision_id="revision-buyback",
        fact_id="fact-buyback",
        event_time=market.as_of - timedelta(days=3),
        observation_id="obs-buyback",
    )
    calendar = calendar.model_copy(
        update={
            "fact": calendar.fact.model_copy(
                update={"fact_type": TREASURY_BUYBACK_OPERATION_FACT_TYPE}
            )
        }
    )
    facts = (direct, calendar)
    state = _state(
        market.as_of,
        account=replay_input.account,
        markets=(market,),
        features=features,
    ).model_copy(update={"fact_revision_ids": tuple(item.fact.revision_id for item in facts)})

    packet = DecisionPacketBuilder(
        DecisionPacketPolicy(
            version="packet-policy-buyback-v1",
            schema_version="decision-packet-v1",
            maximum_background_fact_distance_seconds=86_400,
        )
    ).build(
        mandate=AnalysisMandate(
            version="mandate-v1",
            analysis_scope="crypto-risk",
            question="Assess the event.",
            mandate_exposures=TEST_MANDATE_EXPOSURES,
            observation_assets=(
                ObservationAsset(
                    asset="BTC",
                    market_symbol="BTCUSDT",
                    horizons_minutes=(60,),
                ),
            ),
            required_risk_factors=("REGULATION",),
        ),
        state=state,
        deltas=(_delta(market.as_of),),
        facts=facts,
        account=replay_input.account,
        markets=(market,),
        features=features,
    )

    assert tuple(item.revision_id for item in packet.facts) == (
        "revision-1",
        "revision-buyback",
    )
    assert packet.omitted_fact_revision_ids == ()


def test_packet_preserves_transmission_evidence_before_repeating_calendar_rows(
    app_config,
    replay_input,
) -> None:
    market = replay_input.market
    features = (FeatureEngine(app_config.feature).compute(market),)

    def background_fact(
        *, revision_id: str, fact_id: str, fact_type: str, hours: int
    ) -> VisibleFact:
        visible = _fact(
            market.as_of,
            revision_id=revision_id,
            fact_id=fact_id,
            event_time=market.as_of + timedelta(hours=hours),
            observation_id=f"obs-{revision_id}",
        )
        return visible.model_copy(
            update={
                "fact": visible.fact.model_copy(
                    update={
                        "fact_type": fact_type,
                        "decision_materiality": FactDecisionMateriality.BACKGROUND,
                    }
                )
            }
        )

    facts = (
        background_fact(
            revision_id="revision-calendar-1",
            fact_id="fact-calendar-1",
            fact_type=TREASURY_BUYBACK_OPERATION_FACT_TYPE,
            hours=1,
        ),
        background_fact(
            revision_id="revision-calendar-2",
            fact_id="fact-calendar-2",
            fact_type=TREASURY_BUYBACK_OPERATION_FACT_TYPE,
            hours=2,
        ),
        background_fact(
            revision_id="revision-calendar-3",
            fact_id="fact-calendar-3",
            fact_type=TREASURY_BUYBACK_OPERATION_FACT_TYPE,
            hours=3,
        ),
        background_fact(
            revision_id="revision-tga",
            fact_id="fact-tga",
            fact_type="US_TREASURY_CASH_SNAPSHOT",
            hours=-24,
        ),
        background_fact(
            revision_id="revision-yield",
            fact_id="fact-yield",
            fact_type="US_TREASURY_YIELD_CURVE_SNAPSHOT",
            hours=-24,
        ),
        background_fact(
            revision_id="revision-buyback-result",
            fact_id="fact-buyback-result",
            fact_type=TREASURY_BUYBACK_RESULT_FACT_TYPE,
            hours=-72,
        ),
    )
    state = _state(
        market.as_of,
        account=replay_input.account,
        markets=(market,),
        features=features,
    ).model_copy(update={"fact_revision_ids": tuple(item.fact.revision_id for item in facts)})

    packet = DecisionPacketBuilder(
        DecisionPacketPolicy(
            version="packet-policy-diversity-v1",
            schema_version="decision-packet-v12",
            maximum_facts=4,
        )
    ).build(
        mandate=AnalysisMandate(
            version="mandate-v1",
            analysis_scope="crypto-risk",
            question="Assess the event and its transmission evidence.",
            mandate_exposures=TEST_MANDATE_EXPOSURES,
            observation_assets=(
                ObservationAsset(
                    asset="BTC",
                    market_symbol="BTCUSDT",
                    horizons_minutes=(60,),
                ),
            ),
            required_risk_factors=("REGULATION",),
        ),
        state=state,
        deltas=(),
        review_requests=(
            PacketReviewRequest.create(
                requested_at=market.as_of,
                reason="Validate bounded background evidence diversity.",
            ),
        ),
        facts=facts,
        account=replay_input.account,
        markets=(market,),
        features=features,
    )

    assert {item.fact_type for item in packet.facts} == {
        TREASURY_BUYBACK_OPERATION_FACT_TYPE,
        TREASURY_BUYBACK_RESULT_FACT_TYPE,
        "US_TREASURY_CASH_SNAPSHOT",
        "US_TREASURY_YIELD_CURVE_SNAPSHOT",
    }
    assert {
        "revision-calendar-2",
        "revision-calendar-3",
    }.issubset(packet.omitted_fact_revision_ids)


def test_packet_round_robins_causal_channels_before_repeating_one_channel(
    app_config,
    replay_input,
) -> None:
    market = replay_input.market
    features = (FeatureEngine(app_config.feature).compute(market),)

    def background_fact(
        *,
        revision_id: str,
        fact_type: str,
        risk_factor: str,
        minutes: int,
        source_tier: SourceTier = SourceTier.FIRST_PARTY,
    ) -> VisibleFact:
        visible = _fact(
            market.as_of,
            revision_id=revision_id,
            fact_id=f"fact-{revision_id}",
            event_time=market.as_of - timedelta(minutes=minutes),
            observation_id=f"obs-{revision_id}",
        )
        return visible.model_copy(
            update={
                "highest_source_tier": source_tier,
                "fact": visible.fact.model_copy(
                    update={
                        "fact_type": fact_type,
                        "risk_factors": (risk_factor,),
                        "decision_materiality": FactDecisionMateriality.BACKGROUND,
                    }
                ),
            }
        )

    facts = (
        background_fact(
            revision_id="pce-actual",
            fact_type="US_PCE_RELEASE_ACTUAL",
            risk_factor="US_INFLATION",
            minutes=1,
        ),
        background_fact(
            revision_id="fiscal-result",
            fact_type=TREASURY_BUYBACK_RESULT_FACT_TYPE,
            risk_factor="US_FISCAL_LIQUIDITY",
            minutes=2,
        ),
        background_fact(
            revision_id="fiscal-cash",
            fact_type="US_TREASURY_CASH_SNAPSHOT",
            risk_factor="US_FISCAL_LIQUIDITY",
            minutes=3,
        ),
        background_fact(
            revision_id="institutional-flow",
            fact_type=BTC_ETF_AGGREGATE_FLOW_FACT_TYPE,
            risk_factor="BTC_INSTITUTIONAL_FLOW",
            minutes=4,
            source_tier=SourceTier.AGGREGATOR,
        ),
        background_fact(
            revision_id="rates",
            fact_type="US_TREASURY_YIELD_CURVE_SNAPSHOT",
            risk_factor="US_INTEREST_RATES",
            minutes=5,
        ),
        background_fact(
            revision_id="dollar",
            fact_type="FED_BROAD_DOLLAR_SNAPSHOT",
            risk_factor="US_DOLLAR",
            minutes=6,
        ),
    )
    state = _state(
        market.as_of,
        account=replay_input.account,
        markets=(market,),
        features=features,
    ).model_copy(update={"fact_revision_ids": tuple(item.fact.revision_id for item in facts)})

    packet = DecisionPacketBuilder(
        DecisionPacketPolicy(
            version="packet-policy-causal-diversity-v1",
            schema_version="decision-packet-v12",
            maximum_facts=4,
        )
    ).build(
        mandate=AnalysisMandate(
            version="mandate-v1",
            analysis_scope="crypto-risk",
            question="Assess current causal transmission.",
            mandate_exposures=TEST_MANDATE_EXPOSURES,
            observation_assets=(
                ObservationAsset(
                    asset="BTC",
                    market_symbol="BTCUSDT",
                    horizons_minutes=(60,),
                ),
            ),
            required_risk_factors=(
                "US_INFLATION",
                "BTC_INSTITUTIONAL_FLOW",
                "US_DOLLAR",
                "US_FISCAL_LIQUIDITY",
                "US_INTEREST_RATES",
            ),
        ),
        state=state,
        deltas=(),
        review_requests=(
            PacketReviewRequest.create(
                requested_at=market.as_of,
                reason="Validate causal-channel diversity.",
            ),
        ),
        facts=facts,
        account=replay_input.account,
        markets=(market,),
        features=features,
    )

    assert tuple(item.revision_id for item in packet.facts) == (
        "pce-actual",
        "institutional-flow",
        "dollar",
        "fiscal-result",
    )
    assert packet.omitted_fact_revision_ids == ("fiscal-cash", "rates")


def test_analysis_projection_exposes_partial_observation_boundary(
    app_config,
    replay_input,
) -> None:
    _, packet = _packet(app_config, replay_input)
    current = DomainCoverageSnapshot(
        domain=CausalDomain.CROSS_ASSET_EXTERNAL,
        status=CoverageStatus.CURRENT,
        as_of=packet.as_of,
        source_stream_ids=("cross-asset",),
        covered_capabilities=("USD",),
        latest_success_at=packet.as_of,
        latest_publication_at=packet.as_of,
        latest_poll_refs=("source-poll-current",),
    )
    partial = DomainCoverageSnapshot(
        domain=CausalDomain.FISCAL_DEBT,
        status=CoverageStatus.PARTIAL,
        as_of=packet.as_of,
        source_stream_ids=("treasury-buyback-schedule",),
        covered_capabilities=("DEBT_REPURCHASE",),
        missing_capabilities=("DEBT_ISSUANCE",),
        latest_success_at=packet.as_of,
        latest_publication_at=packet.as_of,
        latest_poll_refs=("source-poll-1",),
    )
    failed = DomainCoverageSnapshot(
        domain=CausalDomain.MONETARY_INFLATION,
        status=CoverageStatus.SOURCE_FAILED,
        as_of=packet.as_of,
        source_stream_ids=("macro-release",),
        covered_capabilities=("POLICY_DECISIONS",),
        missing_capabilities=("INFLATION_SURPRISE",),
        latest_success_at=packet.as_of,
        latest_publication_at=packet.as_of,
        latest_poll_refs=("source-poll-failed",),
    )
    projected = decision_packet_analysis_projection(
        packet.model_copy(update={"information_coverage": (current, partial, failed)})
    )

    assert projected["capability_summary"] == {
        "FISCAL_DEBT": {"missing": ("DEBT_ISSUANCE",)},
        "MONETARY_INFLATION": {
            "status": "SOURCE_FAILED",
            "missing": ("INFLATION_SURPRISE",),
        },
    }
    assert "information_coverage" not in projected
    assert "coverage_gap_codes" not in projected
    assert "data_quality_codes" not in projected


def test_analysis_projection_removes_redundant_market_and_prior_cut_fields(
    app_config,
    replay_input,
) -> None:
    previous = _previous_world_model(
        replay_input.market.as_of,
        assessment_id="assessment-prior-dense",
    )
    _, packet = _packet(app_config, replay_input, previous_context=previous)
    derivative = PacketDerivativeState(
        evidence_ref="f" * 64,
        asset="BTC",
        market_symbol="BTCUSDT",
        observed_at=packet.as_of,
        mark_index_premium_bps=Decimal("1.2"),
        executable_short_basis_bps=Decimal("0.8"),
        perpetual_spread_bps=Decimal("0.4"),
        last_funding_rate_bps=Decimal("0.1"),
        trailing_funding_rate_mean_bps=Decimal("0.08"),
        trailing_funding_rate_sum_bps=Decimal("0.24"),
        funding_settlement_count=3,
        funding_window_hours=24,
        next_funding_time=packet.as_of + timedelta(hours=4),
        spot_flow_observed_at=packet.as_of,
        spot_flow_window_minutes=60,
        spot_taker_buy_sell_ratio=Decimal("1.1"),
        spot_taker_buy_volume=Decimal("110"),
        spot_taker_sell_volume=Decimal("100"),
        positioning_observed_at=packet.as_of,
        positioning_window_minutes=60,
        open_interest=Decimal("1000"),
        open_interest_value=Decimal("100000"),
        open_interest_change_fraction=Decimal("0.02"),
        global_long_short_account_ratio=Decimal("1.5"),
        global_long_account_fraction=Decimal("0.6"),
        global_short_account_fraction=Decimal("0.4"),
        taker_buy_sell_ratio=Decimal("0.9"),
        taker_buy_volume=Decimal("90"),
        taker_sell_volume=Decimal("100"),
    )
    projected = decision_packet_analysis_projection(
        packet.model_copy(update={"derivative_states": (derivative,)})
    )
    asset_columns = projected["asset_states"]["columns"]
    asset_row = dict(zip(asset_columns, projected["asset_states"]["rows"][0], strict=True))
    derivative_columns = projected["derivative_states"]["columns"]
    derivative_row = dict(
        zip(derivative_columns, projected["derivative_states"]["rows"][0], strict=True)
    )

    assert "packet_id" not in projected
    assert "bid" not in asset_row
    assert "ask" not in asset_row
    assert "spot_taker_buy_volume" not in derivative_row
    assert "spot_taker_sell_volume" not in derivative_row
    assert "global_short_account_fraction" not in derivative_row
    assert "global_long_short_account_ratio" not in derivative_row
    assert "contradictions" not in projected["previous_context"]
    assert "data_gaps" not in projected["previous_context"]


def test_analysis_projection_keeps_decision_precision_not_raw_decimal_noise(
    app_config,
    replay_input,
) -> None:
    _, packet = _packet(app_config, replay_input)
    derivative = PacketDerivativeState(
        evidence_ref="f" * 64,
        asset="BTC",
        market_symbol="BTCUSDT",
        observed_at=packet.as_of,
        mark_index_premium_bps=Decimal("0.30027777844133069626657"),
        executable_short_basis_bps=Decimal("-0.512459279172454423072182"),
        perpetual_spread_bps=Decimal("0.01290894738506680057548"),
        last_funding_rate_bps=Decimal("1.00000000"),
        trailing_funding_rate_mean_bps=Decimal("0.86046666666666666666666"),
        trailing_funding_rate_sum_bps=Decimal("2.5814"),
        funding_settlement_count=3,
        funding_window_hours=24,
        next_funding_time=packet.as_of + timedelta(hours=4),
    )

    table = decision_packet_analysis_projection(
        packet.model_copy(update={"derivative_states": (derivative,)})
    )["derivative_states"]
    projected = dict(zip(table["columns"], table["rows"][0], strict=True))

    assert projected["mark_index_premium_bps"] == "0.300278"
    assert projected["executable_short_basis_bps"] == "-0.512459"
    assert projected["perpetual_spread_bps"] == "0.0129089"
    assert projected["last_funding_rate_bps"] == "1"
    assert projected["trailing_funding_rate_mean_bps"] == "0.860467"


def test_analysis_projection_compacts_fact_audit_fields_but_keeps_warnings(
    app_config,
    replay_input,
) -> None:
    _, packet = _packet(app_config, replay_input)
    compact = decision_packet_analysis_projection(packet)["facts"][0]

    assert tuple(compact) == (
        "revision_id",
        "fact_type",
        "event_time",
        "claim",
        "risk_factors",
        "decision_materiality",
        "directly_triggered",
    )

    warned_fact = packet.facts[0].model_copy(
        update={
            "status": FactRevisionStatus.CONFLICTED,
            "highest_source_tier": SourceTier.AGGREGATOR,
            "independent_source_count": 2,
            "prompt_injection_suspected": True,
        }
    )
    warned = decision_packet_analysis_projection(
        packet.model_copy(update={"facts": (warned_fact,)})
    )["facts"][0]

    assert warned["status"] == "CONFLICTED"
    assert warned["highest_source_tier"] == "AGGREGATOR"
    assert warned["independent_source_count"] == 2
    assert warned["prompt_injection_suspected"] is True


def test_analysis_projection_compacts_prior_world_verification_without_losing_state(
    app_config,
    replay_input,
) -> None:
    predicate = PacketPreviousVerificationPredicate(
        operator="GT",
        value=Decimal("1"),
        persistence_observations=2,
    )
    previous = PacketPreviousContext(
        assessment_id="assessment-prior-world-model",
        analysis_scope="crypto-risk",
        mandate_version="mandate-v1",
        analysis_behavior_hash="a" * 64,
        decision_packet_hash="b" * 64,
        as_of=replay_input.market.as_of - timedelta(hours=1),
        available_at=replay_input.market.as_of - timedelta(minutes=59),
        synthesis="主动买盘正在抵消价格下行，但尚未形成反转。",
        synthesis_horizon_hours=24,
        mechanisms=(
            PacketPreviousMechanism(
                mechanism_id="mechanism-prior",
                relationship="OFFSETS",
                claim="主动买盘正在抵消价格下行。",
                horizon_hours=24,
                causal_chain=(
                    PacketPreviousCausalNode(
                        statement="主动买卖比上升。",
                        evidence_ids=("fact-1",),
                    ),
                    PacketPreviousCausalNode(
                        statement="价格下行幅度收窄。",
                        evidence_ids=("fact-2",),
                    ),
                ),
                transmission_stage="PROPAGATING",
                verification_tests=(
                    PacketPreviousVerificationTest(
                        feature_selector="derivative_state:BTC.taker_buy_sell_ratio",
                        evaluation_window_minutes=60,
                        supports_predicate=predicate,
                        contradicts_predicate=predicate.model_copy(update={"operator": "LTE"}),
                        latest_observation=PacketPreviousVerificationObservation(
                            observed_at=replay_input.market.as_of - timedelta(minutes=1),
                            value=Decimal("1.2"),
                            match="SUPPORTS",
                            support_streak=2,
                            contradiction_streak=0,
                            resolution="SUPPORTED",
                        ),
                    ),
                ),
                invalidation_conditions=("主动买卖比连续低于或等于1。",),
                next_review_at=replay_input.market.as_of + timedelta(hours=1),
            ),
        ),
    )
    _, packet = _packet(app_config, replay_input, previous_context=previous)

    previous_projection = decision_packet_analysis_projection(packet)["previous_context"]
    mechanism = previous_projection["mechanisms"][0]
    test = mechanism["tests"][0]

    assert "synthesis" not in previous_projection
    assert "synthesis_horizon_hours" not in previous_projection
    assert "invalidation_conditions" not in mechanism
    assert test == 0
    assert previous_projection["test_catalog"][test] == (
        "derivative_state:BTC.taker_buy_sell_ratio",
        60,
        ("GT", "1", 2),
        ("LTE", "1", 2),
        ("1.2", "SUPPORTS", 2, 0, "SUPPORTED"),
    )


def test_replacing_previous_context_refreezes_packet_identity(
    app_config,
    replay_input,
) -> None:
    previous = _previous_world_model(
        replay_input.market.as_of,
        assessment_id="assessment-refrozen",
    )
    _, packet = _packet(app_config, replay_input)

    refrozen = replace_packet_previous_context(
        packet,
        previous,
        maximum_analysis_characters=16_000,
    )

    assert refrozen.previous_context == previous
    assert refrozen.packet_id != packet.packet_id
    assert refrozen.content_hash != packet.content_hash
    assert refrozen.state_id == packet.state_id
    assert refrozen.trigger_ids == packet.trigger_ids


def test_assess_schema_has_one_world_model_and_no_trade_or_legacy_fields(
    app_config,
    replay_input,
) -> None:
    _, packet = _packet(app_config, replay_input)
    output_schema = assess_output_schema(packet)
    schema = canonical_json(output_schema)
    prompt = build_assess_prompt(packet)

    definitions = WorldModelStructuredOutput.model_json_schema()["$defs"]
    draft_properties = definitions["WorldModelDraft"]["properties"]
    mechanism_properties = definitions["ContextMechanismDraft"]["properties"]
    assert draft_properties["mechanisms"]["maxItems"] == MAX_ACTIVE_WORLD_MECHANISMS
    assert mechanism_properties["causal_chain"]["maxItems"] == MAX_WORLD_CAUSAL_NODES
    assert mechanism_properties["verification_tests"]["maxItems"] == MAX_WORLD_VERIFICATION_TESTS
    assert (
        mechanism_properties["invalidation_conditions"]["maxItems"]
        == MAX_WORLD_INVALIDATION_CONDITIONS
    )

    for forbidden in (
        "suggested_action",
        "order_type",
        "target_notional",
        "market_mechanism",
        "mechanism_evidence_ids",
        '"drivers"',
        '"views"',
        "data_gaps",
        "contradictions",
    ):
        assert forbidden not in schema
    assert '"mechanisms"' in schema
    assert '"retired_mechanisms"' in schema
    assert '"synthesis"' in schema
    assert '"verification_tests"' in schema
    assert '"causal_chain"' in schema
    assert "联合因果解释" in prompt
    assert "结构化字段中的资产代码、数值和枚举必须遵守 Schema" in prompt
    assert "不得把 GTE、LTE、BETWEEN、SUPPORTS 等结构枚举当作中文叙述" in prompt
    assert "有界假设状态" in prompt
    assert "decision_packet_json=" in prompt


def test_assess_schema_restricts_retirement_to_current_evidence(
    app_config,
    replay_input,
) -> None:
    previous = _previous_world_model(
        replay_input.market.as_of,
        assessment_id="assessment-prior-retirement-schema",
    )
    _, packet = _packet(app_config, replay_input, previous_context=previous)

    schema = assess_output_schema(packet)
    retirement_ids = schema["$defs"]["ContextMechanismRetirement"]["properties"]["evidence_ids"][
        "items"
    ]["enum"]

    assert set(retirement_ids) == set(assessment_current_evidence_ids(packet))
    assert "old-1" not in retirement_ids
    assert "old-2" not in retirement_ids


def test_assess_schema_exposes_weak_event_for_review_but_forbids_persistence(
    app_config,
    replay_input,
) -> None:
    event = IntelligenceEvent(
        evidence_id="weak-attention-lead",
        normalizer_version="test-normalizer-v1",
        acquisition_route="aggregator",
        event_time=replay_input.market.as_of - timedelta(minutes=1),
        observed_at=replay_input.market.as_of,
        source="aggregator-flash",
        title="重大但尚未独立核验的外部线索",
        body="该线索需要立即复核，但不能单独支持方向。",
        symbols=("BTCUSDT", "ETHUSDT"),
        relevance=Decimal("0.85"),
        impact=Decimal("0.8415"),
        source_reliability=Decimal("0.60"),
        novelty=Decimal("1"),
    )
    _, packet = _packet(
        app_config,
        replay_input,
        intelligence_events=(event,),
        review_requests=(
            PacketReviewRequest.create(
                requested_at=replay_input.market.as_of,
                reason="复核高优先级待核验线索。",
                evidence_ids=(event.evidence_id,),
            ),
        ),
    )
    packet_event = packet.intelligence_events[0]
    projected_event = decision_packet_analysis_projection(packet)["intelligence_events"][0]
    assert projected_event["body"] == event.body
    for audit_only in (
        "attention_priority",
        "directly_triggered",
        "impact",
        "novelty",
        "observed_at",
        "relevance",
        "source_reliability",
        "symbols",
        "url",
    ):
        assert audit_only not in projected_event
    assert packet_event.directional_support_eligible is False
    assert packet_event.evidence_ref in assessment_visible_evidence_ids(packet)
    assert packet_event.evidence_ref not in assessment_world_model_evidence_ids(packet)
    assert packet_event.evidence_ref not in assessment_current_evidence_ids(packet)

    schema = assess_output_schema(packet)
    definitions = schema["$defs"]
    causal_ids = definitions["ContextCausalNode"]["properties"]["evidence_ids"]["items"]["enum"]
    conflicting_ids = definitions["ContextMechanismDraft"]["properties"][
        "conflicting_evidence_ids"
    ]["items"]["enum"]
    retirement_ids = definitions["ContextMechanismRetirement"]["properties"]["evidence_ids"][
        "items"
    ]["enum"]
    for allowed_ids in (causal_ids, conflicting_ids, retirement_ids):
        assert packet_event.evidence_ref not in allowed_ids
    assert "event_relevance_updates" not in definitions["WorldModelDraft"]["properties"]
    assert "入选本身不是现实影响大小" in build_assess_prompt(packet)
    assert "directional_support_eligible=false" in build_assess_prompt(packet)


def test_event_lifecycle_is_derived_from_current_mechanism_references(
    app_config,
    replay_input,
) -> None:
    event_id = "e" * 64
    previous = _previous_world_model(
        replay_input.market.as_of,
        assessment_id="assessment-prior-event",
    ).model_copy(
        update={
            "event_references": (
                PacketPreviousEventReference(
                    evidence_id=event_id,
                    source="official-source",
                    title="上一轮仍参与解释的正式事件",
                    event_time=replay_input.market.as_of - timedelta(hours=2),
                    impact_state="ACTIVE",
                    rationale="该事件上一轮仍在影响风险溢价。",
                ),
            )
        }
    )
    _, packet = _packet(app_config, replay_input, previous_context=previous)
    base = _world_model_output()
    retirement = ContextMechanismRetirement(
        previous_mechanism_id=previous.mechanisms[0].mechanism_id,
        rationale="当前事实支持新的解释，上一轮机制不再具有决策价值。",
        evidence_ids=("revision-1",),
    )

    assessment = finalize_world_model(
        output=base.model_copy(
            update={
                "world_model": base.world_model.model_copy(
                    update={"retired_mechanisms": (retirement,)}
                )
            }
        ),
        packet=packet,
        analysis_behavior_hash=HASH,
        available_at=packet.as_of + timedelta(seconds=20),
    )

    assert len(assessment.event_references) == 1
    assert assessment.event_references[0].evidence_id == event_id
    assert assessment.event_references[0].impact_state.value == "STALE"
    assert assessment.event_references[0].stale_at == packet.as_of


def test_finalize_assessment_writes_only_current_world_model_schema(
    app_config,
    replay_input,
) -> None:
    _, packet = _packet(app_config, replay_input)
    assessment = finalize_world_model(
        output=_world_model_output(),
        packet=packet,
        analysis_behavior_hash=HASH,
        available_at=packet.as_of + timedelta(seconds=20),
    )

    assert assessment.schema_version == "world-model-assessment-v3"
    assert assessment.analysis_behavior_hash == HASH
    assert assessment.decision_packet_hash == packet.content_hash
    assert assessment.trigger_ids == packet.trigger_ids
    assert assessment.synthesis is not None
    assert len(assessment.mechanisms) == 1
    assert assessment.mechanisms[0].relationship == ContextMechanismRelationship.SUPPORTS


def test_previous_world_model_projection_bounds_historical_test_inventory(
    app_config,
    replay_input,
) -> None:
    _, packet = _packet(app_config, replay_input)
    assessment = finalize_world_model(
        output=_world_model_output(),
        packet=packet,
        analysis_behavior_hash=HASH,
        available_at=packet.as_of + timedelta(seconds=20),
    )
    base = assessment.mechanisms[0].verification_tests[0]
    tests = tuple(
        base.model_copy(update={"feature_selector": selector})
        for selector in (
            "fact_state:US_TREASURY_CASH_SNAPSHOT.tga_change_5d_usd_m",
            "derivative_state:BTC.open_interest_change_fraction",
            "asset_state:BTC.return_fraction",
            "fact_state:FED_BROAD_DOLLAR_SNAPSHOT.broad_dollar_change_1d_pct",
            "derivative_state:BTC.spot_taker_buy_sell_ratio",
        )
    )
    oversized = assessment.model_copy(
        update={
            "mechanisms": (
                assessment.mechanisms[0].model_copy(
                    update={
                        "claim": "因果假设" * 300,
                        "verification_tests": tests,
                    }
                ),
            )
        }
    )

    previous = _previous_context(oversized)

    assert previous is not None
    mechanism = previous.mechanisms[0]
    assert len(mechanism.claim) == 600
    assert tuple(item.feature_selector for item in mechanism.verification_tests) == tuple(
        item.feature_selector for item in tests[:3]
    )


def test_world_model_continuous_cause_requires_connected_fact_test(
    app_config,
    replay_input,
) -> None:
    _, packet = _packet(app_config, replay_input)
    continuous = packet.facts[0].model_copy(
        update={
            "fact_type": "US_TREASURY_CASH_SNAPSHOT",
            "claim": "tga_change_5d_usd_m=-31510 USD_MILLIONS.",
        }
    )
    packet = packet.model_copy(update={"facts": (continuous, *packet.facts[1:])})
    mechanism = _world_model_output().world_model.mechanisms[0]
    causal_chain = tuple(
        node.model_copy(update={"evidence_ids": (continuous.revision_id,)})
        for node in mechanism.causal_chain
    )
    output = _world_model_output().model_copy(
        update={
            "world_model": _world_model_output().world_model.model_copy(
                update={
                    "mechanisms": (mechanism.model_copy(update={"causal_chain": causal_chain}),)
                }
            )
        }
    )

    with pytest.raises(
        ContextAssessmentContractError,
        match="同一事实类型的数值测试因果路径",
    ):
        finalize_world_model(
            output=output,
            packet=packet,
            analysis_behavior_hash=HASH,
            available_at=packet.as_of + timedelta(seconds=20),
        )


def test_world_model_rejects_unknown_evidence(app_config, replay_input) -> None:
    _, packet = _packet(app_config, replay_input)
    base = _world_model_output()
    mechanism = base.world_model.mechanisms[0]
    output = base.model_copy(
        update={
            "world_model": base.world_model.model_copy(
                update={
                    "mechanisms": (
                        mechanism.model_copy(
                            update={
                                "causal_chain": (
                                    mechanism.causal_chain[0],
                                    mechanism.causal_chain[1].model_copy(
                                        update={"evidence_ids": ("not-visible",)}
                                    ),
                                )
                            }
                        ),
                    )
                }
            )
        }
    )

    with pytest.raises(
        ContextAssessmentContractError,
        match="不可见证据",
    ):
        finalize_world_model(
            output=output,
            packet=packet,
            analysis_behavior_hash=HASH,
            available_at=packet.as_of + timedelta(seconds=20),
        )


def test_world_model_continuity_can_only_reference_previous_mechanism(
    app_config,
    replay_input,
) -> None:
    previous = _previous_world_model(
        replay_input.market.as_of,
        assessment_id="assessment-prior-continuity",
    )
    _, packet = _packet(app_config, replay_input, previous_context=previous)
    base = _world_model_output()
    output = base.model_copy(
        update={
            "world_model": base.world_model.model_copy(
                update={
                    "mechanisms": (
                        base.world_model.mechanisms[0].model_copy(
                            update={"continuity_ref": "unknown-mechanism"}
                        ),
                    )
                }
            )
        }
    )

    with pytest.raises(ContextAssessmentContractError, match="不可见的上一轮机制"):
        finalize_world_model(
            output=output,
            packet=packet,
            analysis_behavior_hash=HASH,
            available_at=packet.as_of + timedelta(seconds=20),
        )


def test_world_model_requires_one_disposition_for_every_previous_mechanism(
    app_config,
    replay_input,
) -> None:
    previous = _previous_world_model(
        replay_input.market.as_of,
        assessment_id="assessment-prior-unresolved",
    )
    _, packet = _packet(app_config, replay_input, previous_context=previous)

    with pytest.raises(
        ContextAssessmentContractError,
        match="必须延续或明确退休",
    ):
        finalize_world_model(
            output=_world_model_output(),
            packet=packet,
            analysis_behavior_hash=HASH,
            available_at=packet.as_of + timedelta(seconds=20),
        )


def test_world_model_cannot_silently_drop_one_live_macro_mechanism(
    app_config,
    replay_input,
) -> None:
    previous = _previous_world_model(
        replay_input.market.as_of,
        assessment_id="assessment-before-partial-update",
    )
    macro = previous.mechanisms[0].model_copy(
        update={
            "mechanism_id": "live-macro-rates-mechanism",
            "claim": "政策利率与长端收益率仍在约束广义风险偏好。",
        }
    )
    crypto = previous.mechanisms[0].model_copy(
        update={
            "mechanism_id": "live-crypto-flow-mechanism",
            "claim": "加密内部资金结构仍在传导。",
        }
    )
    previous = previous.model_copy(update={"mechanisms": (macro, crypto)})
    _, packet = _packet(app_config, replay_input, previous_context=previous)
    output = _world_model_output()
    continued_crypto = output.world_model.mechanisms[0].model_copy(
        update={"continuity_ref": crypto.mechanism_id}
    )

    with pytest.raises(
        ContextAssessmentContractError,
        match=macro.mechanism_id,
    ):
        finalize_world_model(
            output=output.model_copy(
                update={
                    "world_model": output.world_model.model_copy(
                        update={"mechanisms": (continued_crypto,)}
                    )
                }
            ),
            packet=packet,
            analysis_behavior_hash=HASH,
            available_at=packet.as_of + timedelta(seconds=20),
        )


def test_world_model_can_continue_every_previous_mechanism(
    app_config,
    replay_input,
) -> None:
    previous = _previous_world_model(
        replay_input.market.as_of,
        assessment_id="assessment-prior-continued",
    )
    _, packet = _packet(app_config, replay_input, previous_context=previous)
    output = _world_model_output()
    continued = output.world_model.mechanisms[0].model_copy(
        update={"continuity_ref": previous.mechanisms[0].mechanism_id}
    )

    assessment = finalize_world_model(
        output=output.model_copy(
            update={
                "world_model": output.world_model.model_copy(update={"mechanisms": (continued,)})
            }
        ),
        packet=packet,
        analysis_behavior_hash=HASH,
        available_at=packet.as_of + timedelta(seconds=20),
    )

    assert assessment.mechanisms[0].continuity_ref == previous.mechanisms[0].mechanism_id
    assert assessment.retired_mechanisms == ()


def test_world_model_records_evidence_bound_mechanism_retirement(
    app_config,
    replay_input,
) -> None:
    previous = _previous_world_model(
        replay_input.market.as_of,
        assessment_id="assessment-prior-retired",
    )
    _, packet = _packet(app_config, replay_input, previous_context=previous)
    output = _world_model_output()
    retirement = ContextMechanismRetirement(
        previous_mechanism_id=previous.mechanisms[0].mechanism_id,
        rationale="本轮正式事实已使上一轮传导解释失去当前决策价值。",
        evidence_ids=("revision-1",),
    )

    assessment = finalize_world_model(
        output=output.model_copy(
            update={
                "world_model": output.world_model.model_copy(
                    update={"retired_mechanisms": (retirement,)}
                )
            }
        ),
        packet=packet,
        analysis_behavior_hash=HASH,
        available_at=packet.as_of + timedelta(seconds=20),
    )

    assert assessment.retired_mechanisms == (retirement,)
    assert assessment.schema_version == "world-model-assessment-v3"


def test_world_model_retirement_requires_current_evidence(
    app_config,
    replay_input,
) -> None:
    previous = _previous_world_model(
        replay_input.market.as_of,
        assessment_id="assessment-prior-retirement-old-evidence",
    )
    _, packet = _packet(app_config, replay_input, previous_context=previous)
    output = _world_model_output()
    retirement = ContextMechanismRetirement(
        previous_mechanism_id=previous.mechanisms[0].mechanism_id,
        rationale="错误地只使用上一轮旧证据退休机制。",
        evidence_ids=("old-1",),
    )

    with pytest.raises(
        ContextAssessmentContractError,
        match="只能引用本轮可见证据",
    ):
        finalize_world_model(
            output=output.model_copy(
                update={
                    "world_model": output.world_model.model_copy(
                        update={"retired_mechanisms": (retirement,)}
                    )
                }
            ),
            packet=packet,
            analysis_behavior_hash=HASH,
            available_at=packet.as_of + timedelta(seconds=20),
        )


def test_packet_rejects_future_derivative_observation(app_config, replay_input) -> None:
    _, packet = _packet(app_config, replay_input)
    derivative = PacketDerivativeState(
        evidence_ref="f" * 64,
        asset="BTC",
        market_symbol="BTCUSDT",
        observed_at=packet.as_of + timedelta(seconds=1),
        mark_index_premium_bps=Decimal("1"),
        executable_short_basis_bps=Decimal("1"),
        perpetual_spread_bps=Decimal("1"),
        last_funding_rate_bps=Decimal("1"),
        trailing_funding_rate_mean_bps=Decimal("1"),
        trailing_funding_rate_sum_bps=Decimal("3"),
        funding_settlement_count=3,
        funding_window_hours=24,
        next_funding_time=packet.as_of + timedelta(hours=4),
    )
    payload = packet.model_dump()
    payload["derivative_states"] = (
        derivative,
        derivative.model_copy(
            update={
                "evidence_ref": "e" * 64,
                "asset": "ETH",
                "market_symbol": "ETHUSDT",
            }
        ),
    )
    with pytest.raises(ValidationError, match="不能晚于 as_of"):
        DecisionPacket.model_validate(payload)


def test_assessment_output_rejects_smuggled_order() -> None:
    payload = _world_model_output().model_dump()
    payload["world_model"]["order_type"] = "MARKET"

    with pytest.raises(ValidationError, match="extra_forbidden"):
        WorldModelStructuredOutput.model_validate(payload)


class _StaticRouter:
    def __init__(self, result: AnalystResult | tuple[AnalystResult, ...]) -> None:
        self.results = list(result if isinstance(result, tuple) else (result,))
        self.bundles = []

    def run(self, bundle):
        self.bundles.append(bundle)
        return self.results.pop(0)


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
    assert bundle.analysis_behavior_hash == _assess_bundle_builder(app_config).behavior_hash(packet)
    schema = (bundle.path / "output.schema.json").read_text(encoding="utf-8")
    assert "suggested_action" not in schema
    assert "target_notional" not in schema
    assert json.loads(schema) == assess_output_schema(packet)


def test_context_analyst_reports_prompt_capacity_before_calling_codex(
    app_config, replay_input, tmp_path, monkeypatch
) -> None:
    _, packet = _packet(app_config, replay_input)
    monkeypatch.setattr(
        "investment_manager.forecast.context.analyst.build_assess_prompt",
        lambda _packet: "x" * (app_config.codex_runtime.maximum_prompt_characters + 1),
    )
    router = _StaticRouter(AnalystResult(False, None, "UNUSED_ROUTER_RESULT"))
    analyst = CodexContextAnalyst(
        tmp_path,
        _assess_bundle_builder(app_config),
        router,
    )

    result = analyst.assess(packet)

    assert not result.success
    assert result.reason_code == "CODEX_PROMPT_CAPACITY_EXCEEDED"
    assert router.bundles == []


def test_configured_assessment_behavior_matches_packets_from_same_config(
    app_config, replay_input
) -> None:
    _, packet = _packet(app_config, replay_input)
    matching_config = app_config.model_copy(
        update={
            "decision_state": app_config.decision_state.model_copy(
                update={
                    "packet_policy": DecisionPacketPolicy(
                        version=packet.policy_version,
                        schema_version=packet.schema_version,
                    )
                }
            ),
            "assessment": app_config.assessment.model_copy(update={"mandate": _mandate()}),
        }
    )

    assert configured_assess_behavior_hash(matching_config) == _assess_bundle_builder(
        matching_config
    ).behavior_hash(packet)


def test_assessment_behavior_hash_includes_schema_retry_contract(app_config, replay_input) -> None:
    _, packet = _packet(app_config, replay_input)
    one_attempt = app_config.codex_runtime.model_copy(update={"max_account_switches": 0})
    three_attempts = app_config.codex_runtime.model_copy(update={"max_account_switches": 2})

    assert AssessRunBundleBuilder(
        one_attempt,
        code_version="test-code",
        configuration_hash=HASH,
    ).behavior_hash(packet) != AssessRunBundleBuilder(
        three_attempts,
        code_version="test-code",
        configuration_hash=HASH,
    ).behavior_hash(packet)


def test_context_analyst_finalizes_assessment_without_trade_authority(
    app_config, replay_input, tmp_path
) -> None:
    _, packet = _packet(app_config, replay_input)
    completed_at = packet.as_of + timedelta(seconds=20)
    router = _StaticRouter(
        AnalystResult(
            True,
            _world_model_output(),
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
    payload = _world_model_output().model_dump()
    payload["world_model"]["mechanisms"][0]["causal_chain"][0]["evidence_ids"] = ("not-visible",)
    router = _StaticRouter(
        AnalystResult(
            True,
            WorldModelStructuredOutput.model_validate(payload),
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
    assert result.reason_code == "WORLD_MODEL_EVIDENCE_NOT_VISIBLE"
    assert len(router.bundles) == 1


def test_context_analyst_retries_same_frozen_bundle_after_output_schema_failure(
    app_config, replay_input, tmp_path
) -> None:
    _, packet = _packet(app_config, replay_input)
    router = _StaticRouter(
        (
            AnalystResult(
                False,
                None,
                "CODEX_SCHEMA_INVALID",
                ".codex",
                1,
                {"total_tokens": 100},
                packet.as_of + timedelta(seconds=20),
                "run-invalid",
            ),
            AnalystResult(
                True,
                _world_model_output(),
                "CODEX_ANALYSIS_SUCCEEDED",
                ".codex2",
                1,
                {"total_tokens": 120},
                packet.as_of + timedelta(seconds=30),
                "run-valid",
            ),
        )
    )
    analyst = CodexContextAnalyst(
        tmp_path,
        _assess_bundle_builder(app_config),
        router,
        maximum_schema_attempts=2,
    )

    result = analyst.assess(packet)

    assert result.success
    assert result.account_id == ".codex2"
    assert result.run_id == "run-valid"
    assert result.attempts == 2
    assert result.usage == {"total_tokens": 220}
    assert len(router.bundles) == 2
    assert router.bundles[0] == router.bundles[1]


def test_context_analyst_stops_immediately_after_deterministic_semantic_failure(
    app_config, replay_input, tmp_path
) -> None:
    _, packet = _packet(app_config, replay_input)
    invalid = _world_model_output().model_copy(
        update={
            "world_model": _world_model_output().world_model.model_copy(
                update={
                    "mechanisms": (
                        _world_model_output()
                        .world_model.mechanisms[0]
                        .model_copy(
                            update={
                                "verification_tests": (
                                    _world_model_output()
                                    .world_model.mechanisms[0]
                                    .verification_tests[0]
                                    .model_copy(update={"feature_selector": "unknown:value"}),
                                )
                            }
                        ),
                    )
                }
            )
        }
    )
    router = _StaticRouter(
        tuple(
            AnalystResult(
                True,
                invalid,
                "CODEX_ANALYSIS_SUCCEEDED",
                account,
                1,
                {"total_tokens": tokens},
                packet.as_of + timedelta(seconds=offset),
                run_id,
            )
            for account, tokens, offset, run_id in (
                (".codex", 100, 20, "run-invalid-1"),
                (".codex2", 120, 30, "run-invalid-2"),
            )
        )
    )
    analyst = CodexContextAnalyst(
        tmp_path,
        _assess_bundle_builder(app_config),
        router,
        maximum_schema_attempts=2,
    )

    result = analyst.assess(packet)

    assert not result.success
    assert result.reason_code == "WORLD_MODEL_FEATURE_SELECTOR_NOT_AVAILABLE"
    assert result.run_id == "run-invalid-1"
    assert result.attempts == 1
    assert result.usage == {"total_tokens": 100}
    assert len(router.bundles) == 1


def test_context_assessment_store_is_immutable_and_idempotent(app_config, replay_input) -> None:
    _, packet = _packet(app_config, replay_input)
    assessment = finalize_world_model(
        output=_world_model_output(),
        packet=packet,
        analysis_behavior_hash=HASH,
        available_at=packet.as_of + timedelta(seconds=20),
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    store = SqlContextAssessmentStore(engine)

    assert store.record_packet(packet) == packet
    assert store.record_packet(packet) == packet
    assert store.record_assessment(packet.packet_id, assessment) == assessment
    assert store.record_assessment(packet.packet_id, assessment) == assessment
    assert store.packet(packet.packet_id) == packet
    assert store.assessment(assessment.assessment_id) == assessment
    assert (
        store.latest_before(
            analysis_scope=assessment.analysis_scope,
            as_of=assessment.available_at - timedelta(microseconds=1),
        )
        is None
    )
    assert (
        store.latest_before(
            analysis_scope=assessment.analysis_scope,
            as_of=assessment.available_at,
        )
        == assessment
    )
    assert (
        store.assessment_for(
            packet_id=packet.packet_id,
            analysis_behavior_hash=assessment.analysis_behavior_hash,
        )
        == assessment
    )


def test_context_assessment_store_rejects_second_output_for_same_behavior(
    app_config, replay_input
) -> None:
    _, packet = _packet(app_config, replay_input)
    first = finalize_world_model(
        output=_world_model_output(),
        packet=packet,
        analysis_behavior_hash=HASH,
        available_at=packet.as_of + timedelta(seconds=20),
    )
    retry = finalize_world_model(
        output=_world_model_output(),
        packet=packet,
        analysis_behavior_hash=HASH,
        available_at=packet.as_of + timedelta(seconds=30),
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    store = SqlContextAssessmentStore(engine)
    store.record_packet(packet)
    store.record_assessment(packet.packet_id, first)

    with pytest.raises(ValueError, match="已有不同的权威"):
        store.record_assessment(packet.packet_id, retry)

    assert (
        store.assessment_for(
            packet_id=packet.packet_id,
            analysis_behavior_hash=HASH,
        )
        == first
    )


def test_context_assessment_store_rejects_packet_mismatch(app_config, replay_input) -> None:
    _, packet = _packet(app_config, replay_input)
    assessment = finalize_world_model(
        output=_world_model_output(),
        packet=packet,
        analysis_behavior_hash=HASH,
        available_at=packet.as_of + timedelta(seconds=20),
    ).model_copy(update={"decision_packet_hash": "b" * 64})
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    store = SqlContextAssessmentStore(engine)
    store.record_packet(packet)

    with pytest.raises(ValueError, match="身份不一致"):
        store.record_assessment(packet.packet_id, assessment)
