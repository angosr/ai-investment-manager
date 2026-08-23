import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine

from investment_manager.forecast.codex.bundle import verify_bundle
from investment_manager.forecast.codex.router import AnalystResult
from investment_manager.forecast.context.analyst import (
    AssessRunBundleBuilder,
    CodexContextAnalyst,
    assess_output_schema,
    configured_assess_behavior_hash,
)
from investment_manager.forecast.context.contract import (
    AssessStructuredOutput,
    ContextAssessmentContractError,
    ContextAssessmentDraft,
    ContextHypothesisDraft,
    ContextMechanismDraft,
    ContextVerificationTestDraft,
    WorldModelDraft,
    WorldModelStructuredOutput,
    assessment_available_feature_selectors,
    build_assess_prompt,
    finalize_context_assessment,
    finalize_world_model,
)
from investment_manager.forecast.context.repository import SqlContextAssessmentStore
from investment_manager.forecast.context.verification import packet_feature_values
from investment_manager.forecast.models import (
    ContextCapitalEffect,
    ContextCapitalImplication,
    ContextCausalNode,
    ContextEventImpactState,
    ContextHypothesisRole,
    ContextMechanismRelationship,
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
    CapitalContextObjective,
    DecisionPacket,
    DecisionPacketBuilder,
    DecisionPacketCapacityError,
    MandateAsset,
    PacketDerivativeState,
    PacketPreviousCausalNode,
    PacketPreviousContext,
    PacketPreviousDriver,
    PacketPreviousEventReference,
    PacketPreviousHypothesis,
    PacketPreviousMechanism,
    PacketPreviousVerificationObservation,
    PacketPreviousVerificationPredicate,
    PacketPreviousVerificationTest,
    PacketPreviousView,
    PacketReviewRequest,
    VisibleFact,
    continuous_fact_numeric_values,
    decision_packet_analysis_projection,
    replace_packet_previous_context,
)
from investment_manager.state.facts import (
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
        account=replay_input.account,
        markets=(market_eth, market_btc),
        features=(feature_eth, feature_btc),
        previous_context=previous_context,
    )
    return builder, packet


def test_packet_carries_latest_world_model_as_derived_evidence(
    app_config,
    replay_input,
) -> None:
    previous = PacketPreviousContext(
        schema_version="world-model-assessment-v1",
        assessment_id="assessment-prior-1",
        analysis_scope="crypto-risk",
        mandate_version="mandate-v1",
        analysis_behavior_hash="a" * 64,
        decision_packet_hash="b" * 64,
        as_of=replay_input.market.as_of - timedelta(hours=1),
        available_at=replay_input.market.as_of - timedelta(minutes=59),
        hypotheses=(
            PacketPreviousHypothesis(
                hypothesis_id="world-hypothesis-prior",
                role="PRIMARY",
                claim="财政流动性变化正在通过期限溢价影响风险资产估值。",
                horizon_hours=168,
                causal_chain=(
                    PacketPreviousCausalNode(
                        statement="财政流动性条件发生变化。",
                        evidence_ids=("revision-prior",),
                    ),
                    PacketPreviousCausalNode(
                        statement="期限溢价成为风险资产的关键中介。",
                        evidence_ids=("market-prior",),
                    ),
                ),
                next_observation="观察长端收益率与美元的同步响应。",
                invalidation_conditions=("长端收益率持续反向变化",),
                next_review_at=replay_input.market.as_of + timedelta(hours=6),
            ),
        ),
    )

    _, packet = _packet(app_config, replay_input, previous_context=previous)

    assert packet.previous_context == previous
    projected = decision_packet_analysis_projection(packet)
    assert projected["previous_context"]["hypotheses"][0]["claim"] == (previous.hypotheses[0].claim)
    assert "causal_chain" not in projected["previous_context"]["hypotheses"][0]
    assert "conflicting_evidence_ids" not in projected["previous_context"]["hypotheses"][0]
    assert previous.assessment_id in build_assess_prompt(packet)


@pytest.mark.parametrize(
    ("scope", "available_offset", "message"),
    (
        ("other-scope", -1, "scope 不一致"),
        ("crypto-risk", 1, "尚不可见"),
    ),
)
def test_packet_rejects_future_or_cross_scope_previous_context(
    app_config,
    replay_input,
    scope,
    available_offset,
    message,
) -> None:
    as_of = replay_input.market.as_of
    previous = PacketPreviousContext(
        assessment_id="assessment-invalid-context",
        analysis_scope=scope,
        mandate_version="mandate-v1",
        analysis_behavior_hash="a" * 64,
        decision_packet_hash="b" * 64,
        as_of=as_of - timedelta(minutes=2),
        available_at=as_of + timedelta(minutes=available_offset),
        market_mechanism="用于验证点时与作用域门禁的历史认知。",
    )

    with pytest.raises(ValueError, match=message):
        _packet(app_config, replay_input, previous_context=previous)


def test_event_reference_is_active_only_when_used_and_expires_one_day_after_stale(
    app_config,
    replay_input,
) -> None:
    event = replay_input.events[0].model_copy(update={"impact": Decimal("0.90")})
    event_ref = content_hash(event)
    _, packet = _packet(app_config, replay_input, intelligence_events=(event,))
    base = _assessment_output()
    primary = base.assessment.hypotheses[0]
    event_node = primary.causal_chain[1].model_copy(update={"evidence_ids": (event_ref,)})
    output = base.model_copy(
        update={
            "assessment": base.assessment.model_copy(
                update={
                    "hypotheses": (
                        primary.model_copy(
                            update={"causal_chain": (primary.causal_chain[0], event_node)}
                        ),
                    )
                }
            )
        }
    )

    assessment = finalize_context_assessment(
        output=output,
        packet=packet,
        analysis_behavior_hash=HASH,
        available_at=packet.as_of + timedelta(seconds=10),
    )

    assert assessment.event_references[0].evidence_id == event_ref
    assert assessment.event_references[0].impact_state == ContextEventImpactState.ACTIVE

    stale_previous = PacketPreviousContext(
        schema_version="world-model-assessment-v1",
        assessment_id=assessment.assessment_id,
        analysis_scope=assessment.analysis_scope,
        mandate_version=assessment.mandate_version,
        analysis_behavior_hash=assessment.analysis_behavior_hash,
        decision_packet_hash=assessment.decision_packet_hash,
        as_of=assessment.as_of,
        available_at=assessment.available_at,
        hypotheses=(
            PacketPreviousHypothesis(
                hypothesis_id=assessment.hypotheses[0].hypothesis_id,
                role="PRIMARY",
                claim=assessment.hypotheses[0].claim,
                horizon_hours=assessment.hypotheses[0].horizon_hours,
                causal_chain=tuple(
                    PacketPreviousCausalNode(
                        statement=node.statement,
                        evidence_ids=node.evidence_ids,
                    )
                    for node in assessment.hypotheses[0].causal_chain
                ),
                next_observation=assessment.hypotheses[0].next_observation,
                invalidation_conditions=assessment.hypotheses[0].invalidation_conditions,
                next_review_at=assessment.hypotheses[0].next_review_at,
            ),
        ),
        event_references=(
            PacketPreviousEventReference(
                evidence_id=event_ref,
                source=event.source,
                title=event.title,
                event_time=event.event_time,
                impact_state="STALE",
                rationale="该事件的未来边际影响已经完全反映。",
                stale_at=packet.as_of,
            ),
        ),
    )
    _, after_expiry = _packet(
        app_config,
        replay_input,
        previous_context=stale_previous,
        as_of=packet.as_of + timedelta(days=1, seconds=1),
    )

    assert after_expiry.previous_context is not None
    assert after_expiry.previous_context.event_references == ()


def test_ineligible_event_cannot_be_promoted_into_world_model(
    app_config,
    replay_input,
) -> None:
    event = replay_input.events[0].model_copy(update={"impact": Decimal("0.90")})
    event_ref = content_hash(event)
    _, packet = _packet(app_config, replay_input, intelligence_events=(event,))
    packet = packet.model_copy(
        update={
            "intelligence_events": (
                packet.intelligence_events[0].model_copy(
                    update={"directional_support_eligible": False}
                ),
            )
        }
    )
    base = _assessment_output()
    primary = base.assessment.hypotheses[0]
    output = base.model_copy(
        update={
            "assessment": base.assessment.model_copy(
                update={
                    "hypotheses": (
                        primary.model_copy(
                            update={
                                "causal_chain": (
                                    primary.causal_chain[0],
                                    primary.causal_chain[1].model_copy(
                                        update={"evidence_ids": (event_ref,)}
                                    ),
                                )
                            }
                        ),
                    )
                }
            )
        }
    )

    with pytest.raises(ContextAssessmentContractError, match="低质量或低影响线索"):
        finalize_context_assessment(
            output=output,
            packet=packet,
            analysis_behavior_hash=HASH,
            available_at=packet.as_of + timedelta(seconds=10),
        )


def _assessment_output() -> AssessStructuredOutput:
    return AssessStructuredOutput(
        assessment=ContextAssessmentDraft(
            hypotheses=(
                ContextHypothesisDraft(
                    role=ContextHypothesisRole.PRIMARY,
                    claim="监管日程变化正在通过风险溢价影响加密资产定价。",
                    horizon_hours=168,
                    causal_chain=(
                        ContextCausalNode(
                            statement="官方监管日程发生了可验证变化。",
                            evidence_ids=("revision-1",),
                        ),
                        ContextCausalNode(
                            statement="该变化构成未来风险溢价重新定价的外生输入。",
                            evidence_ids=("delta-1",),
                        ),
                    ),
                    next_observation="观察正式会议结果与跨资产风险溢价响应。",
                    invalidation_conditions=("官方撤回日程或市场响应否定该传导",),
                    next_review_at=datetime(2026, 9, 1, tzinfo=UTC),
                ),
            ),
        )
    )


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
            assets=(
                MandateAsset(
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
    assert packet.omitted_fact_revision_ids
    assert set(packet.omitted_fact_revision_ids).isdisjoint(
        item.revision_id for item in packet.facts
    )


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
            assets=(
                MandateAsset(
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
            assets=(
                MandateAsset(
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
    assert tuple(item["revision_id"] for item in projection["facts"]) == (
        "revision-1",
    )
    assert projection["state_features"] == {
        "algorithm_version": "continuous-fact-state-v1",
        "regime_states": (
            {
                "type": "US_TREASURY_CASH_SNAPSHOT",
                "at": metric.fact.event_time.isoformat(),
                "state": metric.fact.claim.rstrip(". "),
                "ref": "revision-tga",
            },
        ),
        "flow_states": (),
    }
    numeric_metric = packet.facts[1].model_copy(
        update={
            "claim": (
                "effective_date=2026-08-16; tga_balance_usd_m=800000 USD_MILLIONS; "
                "tga_change_5d_usd_m=-31510 USD_MILLIONS."
            )
        }
    )
    numeric_packet = packet.model_copy(
        update={"facts": (packet.facts[0], numeric_metric)}
    )

    assert continuous_fact_numeric_values(numeric_metric) == {
        "tga_balance_usd_m": Decimal("800000"),
        "tga_change_5d_usd_m": Decimal("-31510"),
    }
    selector = "fact_state:US_TREASURY_CASH_SNAPSHOT.tga_change_5d_usd_m"
    assert selector in assessment_available_feature_selectors(numeric_packet)
    assert packet_feature_values(numeric_packet)[selector] == Decimal("-31510")


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
            assets=(
                MandateAsset(
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
            assets=(
                MandateAsset(
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
            revision_id="fiscal-result",
            fact_type=TREASURY_BUYBACK_RESULT_FACT_TYPE,
            risk_factor="US_FISCAL_LIQUIDITY",
            minutes=1,
        ),
        background_fact(
            revision_id="fiscal-cash",
            fact_type="US_TREASURY_CASH_SNAPSHOT",
            risk_factor="US_FISCAL_LIQUIDITY",
            minutes=2,
        ),
        background_fact(
            revision_id="institutional-flow",
            fact_type=BTC_ETF_AGGREGATE_FLOW_FACT_TYPE,
            risk_factor="BTC_INSTITUTIONAL_FLOW",
            minutes=3,
            source_tier=SourceTier.AGGREGATOR,
        ),
        background_fact(
            revision_id="rates",
            fact_type="US_TREASURY_YIELD_CURVE_SNAPSHOT",
            risk_factor="US_INTEREST_RATES",
            minutes=4,
        ),
        background_fact(
            revision_id="dollar",
            fact_type="FED_BROAD_DOLLAR_SNAPSHOT",
            risk_factor="US_DOLLAR",
            minutes=5,
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
            assets=(
                MandateAsset(
                    asset="BTC",
                    market_symbol="BTCUSDT",
                    horizons_minutes=(60,),
                ),
            ),
            required_risk_factors=(
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
        "fiscal-result",
        "institutional-flow",
        "dollar",
        "rates",
    )
    assert packet.omitted_fact_revision_ids == ("fiscal-cash",)


def test_analysis_projection_compacts_healthy_coverage_to_decision_boundary(
    app_config,
    replay_input,
) -> None:
    _, packet = _packet(app_config, replay_input)
    coverage = DomainCoverageSnapshot(
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
    projected = decision_packet_analysis_projection(
        packet.model_copy(update={"information_coverage": (coverage,)})
    )

    assert projected["capability_summary"] == ()


def test_analysis_projection_removes_redundant_market_and_prior_cut_fields(
    app_config,
    replay_input,
) -> None:
    previous = PacketPreviousContext(
        assessment_id="assessment-prior-dense",
        analysis_scope="crypto-risk",
        mandate_version="mandate-v1",
        analysis_behavior_hash="a" * 64,
        decision_packet_hash="b" * 64,
        as_of=replay_input.market.as_of - timedelta(hours=1),
        available_at=replay_input.market.as_of - timedelta(minutes=59),
        market_mechanism="上一轮仍有效的因果基准。",
        drivers=(
            PacketPreviousDriver(
                statement="仍在影响定价的主导因素。",
                status="INFERRED",
                transmission="该因素仍可能改变风险溢价。",
                invalidation_condition="当前传导被新事实证伪",
            ),
        ),
        views=(),
        contradictions=("上一时点矛盾",),
        data_gaps=("上一时点缺口",),
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

    assert "packet_id" not in projected
    assert "bid" not in projected["asset_states"][0]
    assert "ask" not in projected["asset_states"][0]
    assert "spot_taker_buy_volume" not in projected["derivative_states"][0]
    assert "spot_taker_sell_volume" not in projected["derivative_states"][0]
    assert "global_short_account_fraction" not in projected["derivative_states"][0]
    assert "global_long_short_account_ratio" not in projected["derivative_states"][0]
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

    projected = decision_packet_analysis_projection(
        packet.model_copy(update={"derivative_states": (derivative,)})
    )["derivative_states"][0]

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


def test_analysis_projection_does_not_anchor_new_model_on_legacy_prose(
    app_config,
    replay_input,
) -> None:
    previous = PacketPreviousContext(
        assessment_id="assessment-prior-no-edge",
        analysis_scope="crypto-risk",
        mandate_version="mandate-v1",
        analysis_behavior_hash="a" * 64,
        decision_packet_hash="b" * 64,
        as_of=replay_input.market.as_of - timedelta(hours=1),
        available_at=replay_input.market.as_of - timedelta(minutes=59),
        market_mechanism="上一轮也没有识别到主导驱动。",
        drivers=(),
        views=tuple(
            PacketPreviousView(
                asset=asset,
                horizon_minutes=horizon,
                direction="UNCERTAIN",
                already_priced="UNKNOWN",
                uncertainty="HIGH",
            )
            for asset in ("BTC", "ETH")
            for horizon in (60, 240)
        ),
        contradictions=(),
        data_gaps=(),
    )
    _, packet = _packet(app_config, replay_input, previous_context=previous)

    projected = decision_packet_analysis_projection(packet)
    prompt = build_assess_prompt(packet)

    assert packet.previous_context == previous
    assert projected["previous_context"] == {
        "schema_version": "legacy-context-assessment-v1",
        "event_references": (),
    }
    assert previous.assessment_id not in prompt
    assert previous.market_mechanism not in prompt


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
        schema_version="world-model-assessment-v2",
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

    mechanism = decision_packet_analysis_projection(packet)["previous_context"]["mechanisms"][0]
    test = mechanism["tests"][0]

    assert "invalidation_conditions" not in mechanism
    assert test == (
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
    previous = PacketPreviousContext(
        assessment_id="assessment-refrozen",
        analysis_scope="crypto-risk",
        mandate_version="mandate-v1",
        analysis_behavior_hash="a" * 64,
        decision_packet_hash="b" * 64,
        as_of=replay_input.market.as_of - timedelta(hours=1),
        available_at=replay_input.market.as_of - timedelta(minutes=59),
        market_mechanism="仅用于验证不可变 Packet 重新冻结。",
        views=(),
    )
    _, packet = _packet(app_config, replay_input)

    refrozen = replace_packet_previous_context(packet, previous)

    assert refrozen.previous_context == previous
    assert refrozen.packet_id != packet.packet_id
    assert refrozen.content_hash != packet.content_hash
    assert refrozen.state_id == packet.state_id
    assert refrozen.trigger_ids == packet.trigger_ids


def _capital_mandate() -> AnalysisMandate:
    return _mandate().model_copy(
        update={
            "capital_objective": CapitalContextObjective(
                objective_id="program-entry-veto-v1",
                producer_id="program-candidate",
                producer_version="program-candidate-v1",
                forecast_family="program-opportunity",
                base_decision_inputs=(
                    "BASIS",
                    "COSTS",
                    "FUNDING",
                    "LIQUIDITY",
                ),
            )
        }
    )


def _capital_assessment_output() -> AssessStructuredOutput:
    base = _assessment_output()
    return base.model_copy(
        update={
            "assessment": base.assessment.model_copy(
                update={
                    "capital_implication": ContextCapitalImplication(
                        objective_id="program-entry-veto-v1",
                        effect=ContextCapitalEffect.NEUTRAL,
                        incremental_reason="当前世界证据没有增加程序基线尚未覆盖的入场风险。",
                        transmission=(
                            "已见外生事件尚未形成破坏 funding、basis 或双腿流动性的完整传导。"
                        ),
                        evidence_ids=("revision-1",),
                        invalidation_conditions=("交易场所完整性或双腿流动性出现外生恶化",),
                    )
                }
            )
        }
    )


def test_assess_schema_has_one_world_model_and_no_trade_or_legacy_fields(
    app_config,
    replay_input,
) -> None:
    _, packet = _packet(app_config, replay_input)
    schema = canonical_json(assess_output_schema(packet))
    prompt = build_assess_prompt(packet)

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
    assert '"synthesis"' in schema
    assert '"verification_tests"' in schema
    assert '"causal_chain"' in schema
    assert "联合因果解释" in prompt
    assert "decision_packet_json=" in prompt


def test_world_model_analyzer_rejects_capital_review_packet(
    app_config,
    replay_input,
) -> None:
    _, packet = _packet(
        app_config,
        replay_input,
        packet_schema_version="decision-packet-v13",
        mandate=_capital_mandate(),
    )
    with pytest.raises(ValueError, match="不接受 CAPITAL_REVIEW"):
        assess_output_schema(packet)


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

    assert assessment.schema_version.value == "world-model-assessment-v2"
    assert assessment.analysis_behavior_hash == HASH
    assert assessment.decision_packet_hash == packet.content_hash
    assert assessment.trigger_ids == packet.trigger_ids
    assert assessment.synthesis is not None
    assert len(assessment.mechanisms) == 1
    assert assessment.mechanisms[0].relationship == ContextMechanismRelationship.SUPPORTS
    assert assessment.hypotheses == ()
    assert assessment.market_mechanism is None
    assert assessment.drivers == ()
    assert assessment.data_gaps == ()
    assert assessment.views == ()


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
                    "mechanisms": (
                        mechanism.model_copy(update={"causal_chain": causal_chain}),
                    )
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


def test_world_model_requires_exactly_one_primary() -> None:
    base = _assessment_output().assessment.hypotheses[0]
    with pytest.raises(ValidationError, match="只能包含一个 PRIMARY"):
        ContextAssessmentDraft(
            hypotheses=(
                base,
                base.model_copy(update={"claim": "另一个重复的主假设。"}),
            )
        )


def test_world_model_rejects_unknown_evidence(app_config, replay_input) -> None:
    _, packet = _packet(app_config, replay_input)
    base = _assessment_output()
    primary = base.assessment.hypotheses[0]
    output = base.model_copy(
        update={
            "assessment": base.assessment.model_copy(
                update={
                    "hypotheses": (
                        primary.model_copy(
                            update={
                                "causal_chain": (
                                    primary.causal_chain[0],
                                    primary.causal_chain[1].model_copy(
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
        finalize_context_assessment(
            output=output,
            packet=packet,
            analysis_behavior_hash=HASH,
            available_at=packet.as_of + timedelta(seconds=20),
        )


def test_world_model_continuity_can_only_reference_previous_hypothesis(
    app_config,
    replay_input,
) -> None:
    previous = PacketPreviousContext(
        schema_version="world-model-assessment-v1",
        assessment_id="assessment-prior-continuity",
        analysis_scope="crypto-risk",
        mandate_version="mandate-v1",
        analysis_behavior_hash="a" * 64,
        decision_packet_hash="b" * 64,
        as_of=replay_input.market.as_of - timedelta(hours=1),
        available_at=replay_input.market.as_of - timedelta(minutes=59),
        hypotheses=(
            PacketPreviousHypothesis(
                hypothesis_id="known-hypothesis",
                role="PRIMARY",
                claim="上一轮主解释。",
                horizon_hours=24,
                causal_chain=(
                    PacketPreviousCausalNode(
                        statement="上一轮原因。",
                        evidence_ids=("old-1",),
                    ),
                    PacketPreviousCausalNode(
                        statement="上一轮响应。",
                        evidence_ids=("old-2",),
                    ),
                ),
                next_observation="等待下一项事实。",
                invalidation_conditions=("出现反向事实",),
                next_review_at=replay_input.market.as_of + timedelta(hours=1),
            ),
        ),
    )
    _, packet = _packet(app_config, replay_input, previous_context=previous)
    base = _assessment_output()
    output = base.model_copy(
        update={
            "assessment": base.assessment.model_copy(
                update={
                    "hypotheses": (
                        base.assessment.hypotheses[0].model_copy(
                            update={"continuity_ref": "unknown-hypothesis"}
                        ),
                    )
                }
            )
        }
    )

    with pytest.raises(ContextAssessmentContractError, match="不可见的上一轮假设"):
        finalize_context_assessment(
            output=output,
            packet=packet,
            analysis_behavior_hash=HASH,
            available_at=packet.as_of + timedelta(seconds=20),
        )


def test_primary_hypothesis_must_be_refreshed_by_current_point_in_time_evidence(
    app_config,
    replay_input,
) -> None:
    event = replay_input.events[0].model_copy(update={"impact": Decimal("0.90")})
    event_ref = content_hash(event)
    previous = PacketPreviousContext(
        assessment_id="legacy-with-active-event",
        analysis_scope="crypto-risk",
        mandate_version="mandate-v1",
        as_of=replay_input.market.as_of - timedelta(hours=1),
        available_at=replay_input.market.as_of - timedelta(minutes=59),
        market_mechanism="历史模型。",
        event_references=(
            PacketPreviousEventReference(
                evidence_id=event_ref,
                source=event.source,
                title=event.title,
                event_time=event.event_time,
                impact_state="ACTIVE",
                rationale="仍需验证。",
            ),
        ),
    )
    _, packet = _packet(app_config, replay_input, previous_context=previous)
    base = _assessment_output()
    primary = base.assessment.hypotheses[0]
    event_only = primary.model_copy(
        update={
            "causal_chain": tuple(
                node.model_copy(update={"evidence_ids": (event_ref,)})
                for node in primary.causal_chain
            )
        }
    )
    output = base.model_copy(
        update={"assessment": base.assessment.model_copy(update={"hypotheses": (event_only,)})}
    )

    with pytest.raises(ContextAssessmentContractError, match="本轮点时证据"):
        finalize_context_assessment(
            output=output,
            packet=packet,
            analysis_behavior_hash=HASH,
            available_at=packet.as_of + timedelta(seconds=20),
        )


def test_decision_blocker_requires_different_actions() -> None:
    from investment_manager.forecast.models import ContextDecisionBlocker

    with pytest.raises(ValidationError, match="两种观测结果必须改变资本动作"):
        ContextDecisionBlocker(
            question="正式规则是否生效？",
            action_if_yes="保持程序基线",
            action_if_no="保持程序基线",
            observation_needed="官方最终规则及生效日期",
        )


def test_analysis_projection_exposes_compact_capability_summary_not_gap_wall(
    app_config,
    replay_input,
) -> None:
    _, packet = _packet(app_config, replay_input)
    coverage = DomainCoverageSnapshot(
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

    projected = decision_packet_analysis_projection(
        packet.model_copy(update={"information_coverage": (coverage,)})
    )

    assert projected["capability_summary"] == ()
    assert "information_coverage" not in projected
    assert "coverage_gap_codes" not in projected
    assert "data_quality_codes" not in projected


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
    payload = _assessment_output().model_dump()
    payload["assessment"]["order_type"] = "MARKET"

    with pytest.raises(ValidationError, match="extra_forbidden"):
        AssessStructuredOutput.model_validate(payload)


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
    assessment = finalize_context_assessment(
        output=_assessment_output(),
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
    assessment = finalize_context_assessment(
        output=_assessment_output(),
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
