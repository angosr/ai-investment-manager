import json
from datetime import timedelta
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
    ContextEventReferenceUpdate,
    assessment_visible_evidence_ids,
    build_assess_prompt,
    finalize_context_assessment,
)
from investment_manager.forecast.context.repository import SqlContextAssessmentStore
from investment_manager.forecast.models import (
    AssessmentUncertainty,
    ContextDriver,
    ContextDriverStatus,
    ContextEventImpactState,
    ContextView,
    DirectionalView,
    PricedState,
)
from investment_manager.information.models import IntelligenceEvent, SourceTier
from investment_manager.kernel.identity import canonical_json, content_hash
from investment_manager.market.features import FeatureEngine
from investment_manager.schema import create_schema
from investment_manager.state.decision.packet import (
    AnalysisMandate,
    DecisionPacket,
    DecisionPacketBuilder,
    DecisionPacketCapacityError,
    MandateAsset,
    PacketDerivativeState,
    PacketPreviousContext,
    PacketPreviousDriver,
    PacketPreviousEventReference,
    PacketPreviousView,
    PacketReviewRequest,
    VisibleFact,
    decision_packet_analysis_projection,
)
from investment_manager.state.models import (
    CanonicalFactRevision,
    DeltaCategory,
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
        mandate=_mandate(),
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
        assessment_id="assessment-prior-1",
        analysis_scope="crypto-risk",
        mandate_version="mandate-v1",
        analysis_behavior_hash="a" * 64,
        decision_packet_hash="b" * 64,
        as_of=replay_input.market.as_of - timedelta(hours=1),
        available_at=replay_input.market.as_of - timedelta(minutes=59),
        market_mechanism="财政流动性变化先影响长端利率，再改变风险资产贴现率。",
        drivers=(
            PacketPreviousDriver(
                statement="长端流动性支持正在改变期限溢价预期。",
                status="INFERRED",
                transmission="期限溢价下降会缓解风险资产估值压力。",
                invalidation_condition="长端收益率持续上行且流动性指标恶化",
            ),
        ),
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
        data_gaps=("缺少结构化 ETF 资金流",),
    )

    _, packet = _packet(app_config, replay_input, previous_context=previous)

    assert packet.previous_context == previous
    prompt = build_assess_prompt(packet)
    assert previous.assessment_id in prompt
    assert "上一轮仍可追溯的世界模型" in prompt

    inherited_only = _assessment_output()
    inherited_only = inherited_only.model_copy(
        update={
            "assessment": inherited_only.assessment.model_copy(
                update={
                    "drivers": (
                        inherited_only.assessment.drivers[0].model_copy(
                            update={
                                "status": ContextDriverStatus.INFERRED,
                                "evidence_ids": (previous.assessment_id,),
                            }
                        ),
                    )
                }
            )
        }
    )
    with pytest.raises(ValueError, match="不能单独证明"):
        finalize_context_assessment(
            output=inherited_only,
            packet=packet,
            analysis_behavior_hash=HASH,
            available_at=packet.as_of + timedelta(seconds=20),
        )


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
        drivers=(),
        views=(),
        contradictions=(),
        data_gaps=(),
    )

    with pytest.raises(ValueError, match=message):
        _packet(app_config, replay_input, previous_context=previous)


def test_world_cognition_event_reference_becomes_stale_then_leaves_future_context(
    app_config,
    replay_input,
) -> None:
    event = replay_input.events[0].model_copy(update={"impact": Decimal("0.90")})
    event_ref = content_hash(event)
    _, first_packet = _packet(
        app_config,
        replay_input,
        intelligence_events=(event,),
    )
    event_reference_updates_schema = assess_output_schema(first_packet)["$defs"][
        "ContextAssessmentDraft"
    ]["properties"]["event_reference_updates"]
    assert event_reference_updates_schema["maxItems"] == 1
    active_output = _assessment_output().model_copy(
        update={
            "assessment": _assessment_output().assessment.model_copy(
                update={
                    "event_reference_updates": (
                        ContextEventReferenceUpdate(
                            evidence_id=event_ref,
                            impact_state=ContextEventImpactState.ACTIVE,
                            rationale="该事件的政策传导仍可能改变未来风险溢价。",
                        ),
                    ),
                }
            )
        }
    )
    active = finalize_context_assessment(
        output=active_output,
        packet=first_packet,
        analysis_behavior_hash=HASH,
        available_at=first_packet.as_of + timedelta(seconds=10),
    )
    assert active.event_references[0].stale_at is None

    second_as_of = first_packet.as_of + timedelta(hours=2)
    previous = PacketPreviousContext(
        assessment_id=active.assessment_id,
        analysis_scope=active.analysis_scope,
        mandate_version=active.mandate_version,
        analysis_behavior_hash=active.analysis_behavior_hash,
        decision_packet_hash=active.decision_packet_hash,
        as_of=active.as_of,
        available_at=active.available_at,
        market_mechanism=active.market_mechanism,
        drivers=(),
        event_references=(
            PacketPreviousEventReference(
                evidence_id=event_ref,
                source=event.source,
                title=event.title,
                event_time=event.event_time,
                impact_state="ACTIVE",
                rationale=active.event_references[0].rationale,
            ),
        ),
        views=(),
        contradictions=(),
        data_gaps=(),
    )
    _, second_packet = _packet(
        app_config,
        replay_input,
        previous_context=previous,
        as_of=second_as_of,
    )
    stale_output = _assessment_output().model_copy(
        update={
            "assessment": _assessment_output().assessment.model_copy(
                update={
                    "event_reference_updates": (
                        ContextEventReferenceUpdate(
                            evidence_id=event_ref,
                            impact_state=ContextEventImpactState.STALE,
                            rationale="预期传导已经完成，新增价格形成不再依赖该事件。",
                        ),
                    ),
                }
            )
        }
    )
    stale = finalize_context_assessment(
        output=stale_output,
        packet=second_packet,
        analysis_behavior_hash=HASH,
        available_at=second_as_of + timedelta(seconds=10),
    )
    assert stale.event_references[0].stale_at == second_as_of

    stale_previous = previous.model_copy(
        update={
            "assessment_id": stale.assessment_id,
            "as_of": stale.as_of,
            "available_at": stale.available_at,
            "event_references": (
                previous.event_references[0].model_copy(
                    update={
                        "impact_state": "STALE",
                        "rationale": stale.event_references[0].rationale,
                        "stale_at": second_as_of,
                    }
                ),
            ),
        }
    )
    _, within_grace = _packet(
        app_config,
        replay_input,
        previous_context=stale_previous,
        as_of=second_as_of + timedelta(hours=12),
    )
    projected_previous = decision_packet_analysis_projection(within_grace)[
        "previous_context"
    ]
    assert projected_previous["event_references"] == ()
    assert event_ref not in assessment_visible_evidence_ids(within_grace)
    _, after_grace = _packet(
        app_config,
        replay_input,
        previous_context=stale_previous,
        as_of=second_as_of + timedelta(days=1),
    )
    assert after_grace.previous_context is not None
    assert after_grace.previous_context.event_references == ()


def test_new_driver_event_citation_registers_active_reference_without_duplicate_update(
    app_config,
    replay_input,
) -> None:
    event = replay_input.events[0].model_copy(update={"impact": Decimal("0.90")})
    event_ref = content_hash(event)
    _, packet = _packet(
        app_config,
        replay_input,
        intelligence_events=(event,),
    )
    base = _assessment_output()
    driver = base.assessment.drivers[0].model_copy(
        update={
            "status": ContextDriverStatus.UNVERIFIED,
            "evidence_ids": (event_ref,),
        }
    )
    output = base.model_copy(
        update={"assessment": base.assessment.model_copy(update={"drivers": (driver,)})}
    )

    assessment = finalize_context_assessment(
        output=output,
        packet=packet,
        analysis_behavior_hash=HASH,
        available_at=packet.as_of + timedelta(seconds=10),
    )

    assert len(assessment.event_references) == 1
    assert assessment.event_references[0].evidence_id == event_ref
    assert assessment.event_references[0].impact_state == ContextEventImpactState.ACTIVE
    assert assessment.event_references[0].rationale == driver.statement


def test_world_cognition_inherits_omitted_active_event_update(
    app_config,
    replay_input,
) -> None:
    event = replay_input.events[0]
    event_ref = content_hash(event)
    as_of = replay_input.market.as_of
    previous = PacketPreviousContext(
        assessment_id="assessment-with-active-event",
        analysis_scope="crypto-risk",
        mandate_version="mandate-v1",
        analysis_behavior_hash="a" * 64,
        decision_packet_hash="b" * 64,
        as_of=as_of - timedelta(hours=1),
        available_at=as_of - timedelta(minutes=59),
        market_mechanism="该事件仍可能通过风险溢价影响未来定价。",
        drivers=(),
        event_references=(
            PacketPreviousEventReference(
                evidence_id=event_ref,
                source=event.source,
                title=event.title,
                event_time=event.event_time,
                impact_state="ACTIVE",
                rationale="未来影响尚未完全消退。",
            ),
        ),
        views=(),
        contradictions=(),
        data_gaps=(),
    )
    _, packet = _packet(
        app_config,
        replay_input,
        previous_context=previous,
    )
    assessment = finalize_context_assessment(
        output=_assessment_output(),
        packet=packet,
        analysis_behavior_hash=HASH,
        available_at=as_of + timedelta(seconds=10),
    )

    assert assessment.event_references[0].evidence_id == event_ref
    assert assessment.event_references[0].impact_state == ContextEventImpactState.ACTIVE
    assert assessment.event_references[0].rationale == "未来影响尚未完全消退。"


def test_known_weak_event_is_retired_from_current_world_cognition(
    app_config,
    replay_input,
) -> None:
    event = replay_input.events[0].model_copy(
        update={"impact": Decimal("0.95"), "source_reliability": Decimal("0.60")}
    )
    event_ref = content_hash(event)
    as_of = replay_input.market.as_of
    previous = PacketPreviousContext(
        assessment_id="assessment-with-weak-event",
        analysis_scope="crypto-risk",
        mandate_version="mandate-v1",
        analysis_behavior_hash="a" * 64,
        decision_packet_hash="b" * 64,
        as_of=as_of - timedelta(hours=1),
        available_at=as_of - timedelta(minutes=59),
        market_mechanism="该线索此前被误纳入当前世界认知。",
        drivers=(),
        event_references=(
            PacketPreviousEventReference(
                evidence_id=event_ref,
                source=event.source,
                title=event.title,
                event_time=event.event_time,
                impact_state="ACTIVE",
                rationale="此前认为仍可能影响风险偏好。",
            ),
        ),
        views=(),
        contradictions=(),
        data_gaps=(),
    )

    _, packet = _packet(
        app_config,
        replay_input,
        previous_context=previous,
        intelligence_events=(event,),
    )

    assert packet.previous_context is not None
    retired = packet.previous_context.event_references[0]
    assert retired.impact_state == "STALE"
    assert retired.stale_at == as_of
    assert "永久事件账本" in retired.rationale


def test_ineligible_event_cannot_be_promoted_by_ai_driver(app_config, replay_input) -> None:
    event = replay_input.events[0].model_copy(update={"impact": Decimal("0.90")})
    event_ref = content_hash(event)
    _, packet = _packet(app_config, replay_input, intelligence_events=(event,))
    packet_event = packet.intelligence_events[0].model_copy(
        update={"directional_support_eligible": False}
    )
    packet = packet.model_copy(update={"intelligence_events": (packet_event,)})
    base = _assessment_output()
    driver = base.assessment.drivers[0].model_copy(
        update={"status": ContextDriverStatus.UNVERIFIED, "evidence_ids": (event_ref,)}
    )
    output = base.model_copy(
        update={"assessment": base.assessment.model_copy(update={"drivers": (driver,)})}
    )

    with pytest.raises(
        ContextAssessmentContractError,
        match="低质量或低影响线索",
    ):
        finalize_context_assessment(
            output=output,
            packet=packet,
            analysis_behavior_hash=HASH,
            available_at=packet.as_of + timedelta(seconds=10),
        )


def _assessment_output() -> AssessStructuredOutput:
    return AssessStructuredOutput(
        assessment=ContextAssessmentDraft(
            market_mechanism="监管确定性变化可能改变市场要求的风险溢价。",
            drivers=(
                ContextDriver(
                    statement="监管日程发生了可验证变化。",
                    status=ContextDriverStatus.CONFIRMED,
                    transmission="监管确定性可能经由风险溢价影响加密资产估值。",
                    evidence_ids=("revision-1",),
                    invalidation_conditions=("官方撤回或修订相关信息",),
                ),
            ),
            views=tuple(
                ContextView(
                    asset=asset,
                    horizon_minutes=horizon,
                    direction=DirectionalView.UNCERTAIN,
                    already_priced=PricedState.UNKNOWN,
                    uncertainty=AssessmentUncertainty.HIGH,
                    evidence_ids=("revision-1",),
                    invalidation_conditions=("官方撤回或修订相关信息",),
                )
                for asset in ("BTC", "ETH")
                for horizon in (60, 240)
            ),
            data_gaps=("市场反应尚未充分形成",),
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


def test_assess_schema_has_no_trade_action_fields(app_config, replay_input) -> None:
    _, packet = _packet(app_config, replay_input)
    schema = canonical_json(assess_output_schema(packet))
    prompt = build_assess_prompt(packet)

    assert "suggested_action" not in schema
    assert "order_type" not in schema
    assert "target_notional" not in schema
    assert 'required_views_output_order_json=[{"asset":"BTC","horizon_minutes":60}' in prompt
    assert 'allowed_evidence_ids_json=["delta-1","delta-2","feature-btc"' in prompt
    assert "decision_packet_json=" in prompt


def test_assess_schema_constrains_packet_views_and_evidence(app_config, replay_input) -> None:
    _, packet = _packet(app_config, replay_input)
    schema = assess_output_schema(packet)
    views = schema["$defs"]["ContextAssessmentDraft"]["properties"]["views"]

    assert views["minItems"] == 4
    assert views["maxItems"] == 4
    branches = views["items"]["anyOf"]
    assert tuple(
        (
            branch["properties"]["asset"]["enum"][0],
            branch["properties"]["horizon_minutes"]["enum"][0],
        )
        for branch in branches
    ) == (("BTC", 60), ("BTC", 240), ("ETH", 60), ("ETH", 240))
    assert all(
        branch["properties"]["evidence_ids"]["items"]["enum"]
        == ["delta-1", "delta-2", "feature-btc", "feature-eth", "revision-1"]
        for branch in branches
    )
    drivers = schema["$defs"]["ContextAssessmentDraft"]["properties"]["drivers"]
    assert drivers["minItems"] == 0
    assert drivers["maxItems"] == 8


def test_assessment_accepts_no_driver_when_all_views_are_uncertain(
    app_config, replay_input
) -> None:
    _, packet = _packet(app_config, replay_input)
    base = _assessment_output()
    output = base.model_copy(
        update={
            "assessment": base.assessment.model_copy(
                update={"drivers": ()}
            )
        }
    )

    assessment = finalize_context_assessment(
        output=output,
        packet=packet,
        analysis_behavior_hash=HASH,
        available_at=packet.as_of + timedelta(seconds=20),
    )

    assert assessment.drivers == ()


def test_historical_packet_payloads_remain_readable_after_context_provenance(
    app_config,
    replay_input,
) -> None:
    as_of = replay_input.market.as_of
    previous = PacketPreviousContext(
        assessment_id="assessment-v6",
        analysis_scope="crypto-risk",
        mandate_version="mandate-v1",
        analysis_behavior_hash="a" * 64,
        decision_packet_hash="b" * 64,
        as_of=as_of - timedelta(minutes=2),
        available_at=as_of - timedelta(minutes=1),
        market_mechanism="用于验证历史输入包仍可读取。",
        drivers=(),
        views=(),
        contradictions=(),
        data_gaps=(),
    )
    _, packet = _packet(
        app_config,
        replay_input,
        previous_context=previous,
        packet_schema_version="decision-packet-v6",
    )
    payload = packet.model_dump(mode="json")
    for field_name in (
        "analysis_scope",
        "mandate_version",
        "analysis_behavior_hash",
        "decision_packet_hash",
        "event_references",
    ):
        payload["previous_context"].pop(field_name)

    restored = DecisionPacket.model_validate(payload)

    assert restored.content_hash == packet.content_hash
    assert restored.previous_context is not None
    assert restored.previous_context.analysis_scope is None


def test_finalize_assessment_binds_authoritative_runtime_metadata(app_config, replay_input) -> None:
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


def test_finalize_assessment_canonicalizes_complete_reordered_views(
    app_config, replay_input
) -> None:
    _, packet = _packet(app_config, replay_input)
    output = _assessment_output()
    reordered = output.model_copy(
        update={
            "assessment": output.assessment.model_copy(
                update={"views": tuple(reversed(output.assessment.views))}
            )
        }
    )

    assessment = finalize_context_assessment(
        output=reordered,
        packet=packet,
        analysis_behavior_hash=HASH,
        available_at=packet.as_of + timedelta(seconds=20),
    )

    assert tuple((item.asset, item.horizon_minutes) for item in assessment.views) == tuple(
        (item.asset, item.horizon_minutes) for item in packet.required_views
    )


def test_finalize_assessment_rejects_duplicate_view(app_config, replay_input) -> None:
    _, packet = _packet(app_config, replay_input)
    output = _assessment_output()
    duplicate = output.model_copy(
        update={
            "assessment": output.assessment.model_copy(
                update={"views": (*output.assessment.views[:-1], output.assessment.views[0])}
            )
        }
    )

    with pytest.raises(ValueError, match="required_views 不一致"):
        finalize_context_assessment(
            output=duplicate,
            packet=packet,
            analysis_behavior_hash=HASH,
            available_at=packet.as_of + timedelta(seconds=20),
        )


def test_finalize_assessment_rejects_unknown_evidence(app_config, replay_input) -> None:
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


def test_finalize_assessment_rejects_derivative_state_as_only_driver(
    app_config, replay_input
) -> None:
    _, packet = _packet(app_config, replay_input)
    evidence_ref = "f" * 64
    derivative = PacketDerivativeState(
        evidence_ref=evidence_ref,
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
    )
    payload = {
        name: getattr(packet, name)
        for name in packet.__class__.model_fields
        if name not in {"packet_id", "content_hash"}
    }
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
    derivative_packet = DecisionPacket.create(**payload)
    output = _assessment_output()
    driver = output.assessment.drivers[0].model_copy(
        update={
            "statement": "BTC 永续资金费率已由交易所数据直接确认。",
            "evidence_ids": (evidence_ref,),
        }
    )
    output = output.model_copy(
        update={"assessment": output.assessment.model_copy(update={"drivers": (driver,)})}
    )

    with pytest.raises(
        ContextAssessmentContractError,
        match="不得单独冒充为主导驱动",
    ):
        finalize_context_assessment(
            output=output,
            packet=derivative_packet,
            analysis_behavior_hash=HASH,
            available_at=packet.as_of + timedelta(seconds=20),
        )


def test_packet_allows_independent_spot_and_perpetual_observation_times(
    app_config, replay_input
) -> None:
    _, packet = _packet(app_config, replay_input)
    derivative = PacketDerivativeState(
        evidence_ref="f" * 64,
        asset="BTC",
        market_symbol="BTCUSDT",
        observed_at=packet.as_of - timedelta(seconds=5),
        mark_index_premium_bps=Decimal("1.2"),
        executable_short_basis_bps=Decimal("0.8"),
        perpetual_spread_bps=Decimal("0.4"),
        last_funding_rate_bps=Decimal("0.1"),
        trailing_funding_rate_mean_bps=None,
        trailing_funding_rate_sum_bps=None,
        funding_settlement_count=0,
        funding_window_hours=24,
        next_funding_time=packet.as_of + timedelta(hours=4),
        spot_flow_observed_at=packet.as_of,
        spot_flow_window_minutes=10,
        spot_taker_buy_sell_ratio=Decimal("1.1"),
        spot_taker_buy_volume=Decimal("10"),
        spot_taker_sell_volume=Decimal("9"),
    )
    payload = {
        name: getattr(packet, name)
        for name in packet.__class__.model_fields
        if name not in {"packet_id", "content_hash"}
    }
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

    rebuilt = DecisionPacket.create(**payload)

    assert rebuilt.derivative_states[0].spot_flow_observed_at == packet.as_of


def test_packet_rejects_future_spot_flow_observation(app_config, replay_input) -> None:
    _, packet = _packet(app_config, replay_input)
    derivative = PacketDerivativeState(
        evidence_ref="f" * 64,
        asset="BTC",
        market_symbol="BTCUSDT",
        observed_at=packet.as_of,
        mark_index_premium_bps=Decimal("1.2"),
        executable_short_basis_bps=Decimal("0.8"),
        perpetual_spread_bps=Decimal("0.4"),
        last_funding_rate_bps=Decimal("0.1"),
        trailing_funding_rate_mean_bps=None,
        trailing_funding_rate_sum_bps=None,
        funding_settlement_count=0,
        funding_window_hours=24,
        next_funding_time=packet.as_of + timedelta(hours=4),
        spot_flow_observed_at=packet.as_of + timedelta(seconds=1),
        spot_flow_window_minutes=10,
        spot_taker_buy_sell_ratio=Decimal("1.1"),
        spot_taker_buy_volume=Decimal("10"),
        spot_taker_sell_volume=Decimal("9"),
    )
    payload = {
        name: getattr(packet, name)
        for name in packet.__class__.model_fields
        if name not in {"packet_id", "content_hash"}
    }
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

    with pytest.raises(ValueError, match="不能晚于 as_of"):
        DecisionPacket.create(**payload)


def test_finalize_assessment_rejects_derivative_only_direction(app_config, replay_input) -> None:
    _, packet = _packet(app_config, replay_input)
    evidence_ref = "f" * 64
    derivative = PacketDerivativeState(
        evidence_ref=evidence_ref,
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
    )
    payload = {
        name: getattr(packet, name)
        for name in packet.__class__.model_fields
        if name not in {"packet_id", "content_hash"}
    }
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
    derivative_packet = DecisionPacket.create(**payload)
    output = _assessment_output()
    views = tuple(
        view.model_copy(
            update={
                "direction": DirectionalView.UP,
                "evidence_ids": (evidence_ref if view.asset == "BTC" else "e" * 64,),
            }
        )
        for view in output.assessment.views
    )
    output = output.model_copy(
        update={"assessment": output.assessment.model_copy(update={"views": views})}
    )

    with pytest.raises(ValueError, match="缺少衍生品状态之外"):
        finalize_context_assessment(
            output=output,
            packet=derivative_packet,
            analysis_behavior_hash=HASH,
            available_at=packet.as_of + timedelta(seconds=20),
        )


def test_finalize_assessment_rejects_missing_required_view(app_config, replay_input) -> None:
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
    assert result.reason_code == "ASSESSMENT_EVIDENCE_NOT_VISIBLE"
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
                _assessment_output(),
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
    invalid_payload = _assessment_output().model_dump()
    invalid_payload["assessment"]["views"] = invalid_payload["assessment"]["views"][:-1]
    invalid = AssessStructuredOutput.model_validate(invalid_payload)
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
    assert result.reason_code == "ASSESSMENT_VIEW_SET_INVALID"
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
