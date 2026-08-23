from datetime import UTC, datetime, timedelta
from decimal import Decimal

from investment_manager.forecast.context.verification import (
    observe_world_model,
    predicate_match,
)
from investment_manager.forecast.models import (
    ContextAssessment,
    ContextCausalNode,
    ContextMechanism,
    ContextMechanismRelationship,
    ContextPredicateOperator,
    ContextTransmissionStage,
    ContextVerificationMatch,
    ContextVerificationPredicate,
    ContextVerificationResolution,
    ContextVerificationTest,
)
from investment_manager.information.aggregated_flows import (
    BTC_ETF_AGGREGATE_FLOW_FACT_TYPE,
)
from investment_manager.information.models import SourceTier
from investment_manager.state.decision.packet import (
    DecisionPacket,
    PacketAssetState,
    PacketFact,
)
from investment_manager.state.models import FactDecisionMateriality, FactRevisionStatus

NOW = datetime(2026, 8, 23, 6, tzinfo=UTC)


def _predicate(
    operator: ContextPredicateOperator,
    value: str,
    *,
    persistence: int = 1,
) -> ContextVerificationPredicate:
    return ContextVerificationPredicate(
        operator=operator,
        value=Decimal(value),
        persistence_observations=persistence,
    )


def _assessment() -> ContextAssessment:
    test = ContextVerificationTest(
        feature_selector="asset_state:BTC.return_fraction",
        evaluation_window_minutes=240,
        supports_predicate=_predicate(
            ContextPredicateOperator.GT,
            "0.01",
            persistence=2,
        ),
        contradicts_predicate=_predicate(
            ContextPredicateOperator.LT,
            "-0.01",
            persistence=2,
        ),
    )
    mechanism = ContextMechanism(
        mechanism_id="mechanism-1",
        relationship=ContextMechanismRelationship.SUPPORTS,
        claim="风险偏好正在改善。",
        horizon_hours=4,
        causal_chain=(
            ContextCausalNode(statement="需求增加。", evidence_ids=("fact-1",)),
            ContextCausalNode(statement="价格响应。", evidence_ids=("fact-1",)),
        ),
        transmission_stage=ContextTransmissionStage.PROPAGATING,
        verification_tests=(test,),
        invalidation_conditions=("收益率持续转负。",),
        next_review_at=NOW + timedelta(hours=1),
    )
    return ContextAssessment.model_construct(
        assessment_id="assessment-1",
        available_at=NOW,
        mechanisms=(mechanism,),
    )


def _packet(
    packet_id: str,
    *,
    at: datetime,
    return_fraction: str,
    source_at: datetime | None = None,
) -> DecisionPacket:
    asset = PacketAssetState(
        asset="BTC",
        market_symbol="BTCUSDT",
        observed_at=source_at or at,
        bid=Decimal("100"),
        ask=Decimal("101"),
        last=Decimal("100.5"),
        return_fraction=Decimal(return_fraction),
        realized_volatility=Decimal("0.02"),
        atr=Decimal("2"),
        spread_bps=Decimal("1"),
        volume_ratio=Decimal("1"),
        regime="TRENDING_UP",
        market_age_seconds=0,
    )
    return DecisionPacket.model_construct(
        packet_id=packet_id,
        as_of=at,
        asset_states=(asset,),
        derivative_states=(),
    )


def _fact_packet(
    packet_id: str,
    *,
    at: datetime,
    revision_id: str,
    net_inflow: str,
) -> DecisionPacket:
    packet = _packet(packet_id, at=at, return_fraction="0")
    fact = PacketFact(
        fact_id="btc-etf-flow",
        revision_id=revision_id,
        fact_type=BTC_ETF_AGGREGATE_FLOW_FACT_TYPE,
        status=FactRevisionStatus.ACTIVE,
        event_time=at,
        observed_at=at,
        headline="比特币现货 ETF 合计流量",
        claim=f"finalized_daily_net_inflow_usd_m={net_inflow} USD_MILLIONS.",
        affected_assets=("BTC",),
        risk_factors=("BTC_INSTITUTIONAL_FLOW",),
        decision_materiality=FactDecisionMateriality.CANDIDATE,
        highest_source_tier=SourceTier.AGGREGATOR,
        independent_source_count=1,
        prompt_injection_suspected=False,
        directly_triggered=False,
    )
    return packet.model_copy(update={"facts": (fact,)})


def _fact_assessment() -> ContextAssessment:
    selector = (
        f"fact_state:{BTC_ETF_AGGREGATE_FLOW_FACT_TYPE}."
        "finalized_daily_net_inflow_usd_m"
    )
    assessment = _assessment()
    mechanism = assessment.mechanisms[0].model_copy(
        update={
            "verification_tests": (
                ContextVerificationTest(
                    feature_selector=selector,
                    evaluation_window_minutes=240,
                    supports_predicate=_predicate(
                        ContextPredicateOperator.GT, "0", persistence=2
                    ),
                    contradicts_predicate=_predicate(
                        ContextPredicateOperator.LT, "0", persistence=2
                    ),
                ),
            )
        }
    )
    return assessment.model_copy(update={"mechanisms": (mechanism,)})


def test_predicate_match_marks_overlap_ambiguous() -> None:
    assert (
        predicate_match(
            Decimal("1"),
            supports=_predicate(ContextPredicateOperator.GTE, "0"),
            contradicts=_predicate(ContextPredicateOperator.LTE, "2"),
        )
        == ContextVerificationMatch.AMBIGUOUS
    )


def test_world_model_test_requires_consecutive_point_in_time_observations() -> None:
    assessment = _assessment()
    first = observe_world_model(
        assessment,
        _packet("packet-1", at=NOW + timedelta(minutes=30), return_fraction="0.02"),
    )
    assert len(first) == 1
    assert first[0].match == ContextVerificationMatch.SUPPORTS
    assert first[0].resolution == ContextVerificationResolution.PENDING
    assert first[0].support_streak == 1

    second = observe_world_model(
        assessment,
        _packet("packet-2", at=NOW + timedelta(minutes=60), return_fraction="0.03"),
        previous=first,
    )
    assert second[0].resolution == ContextVerificationResolution.SUPPORTED
    assert second[0].support_streak == 2


def test_repeated_market_snapshot_does_not_fabricate_persistence() -> None:
    assessment = _assessment()
    source_at = NOW + timedelta(minutes=15)
    first = observe_world_model(
        assessment,
        _packet(
            "packet-1",
            at=NOW + timedelta(minutes=30),
            source_at=source_at,
            return_fraction="0.02",
        ),
    )

    repeated = observe_world_model(
        assessment,
        _packet(
            "packet-2",
            at=NOW + timedelta(minutes=60),
            source_at=source_at,
            return_fraction="0.02",
        ),
        previous=first,
    )

    assert repeated == ()


def test_feature_observed_before_assessment_is_not_future_evidence() -> None:
    assessment = _assessment()

    observation = observe_world_model(
        assessment,
        _packet(
            "packet-1",
            at=NOW + timedelta(minutes=30),
            source_at=NOW - timedelta(minutes=1),
            return_fraction="0.02",
        ),
    )

    assert observation == ()


def test_repeated_fact_revision_does_not_fabricate_persistence() -> None:
    assessment = _fact_assessment()
    first = observe_world_model(
        assessment,
        _fact_packet(
            "packet-1",
            at=NOW + timedelta(minutes=30),
            revision_id="flow-revision-1",
            net_inflow="100",
        ),
    )

    repeated = observe_world_model(
        assessment,
        _fact_packet(
            "packet-2",
            at=NOW + timedelta(minutes=60),
            revision_id="flow-revision-1",
            net_inflow="100",
        ),
        previous=first,
    )

    assert first[0].feature_observation_ref == "flow-revision-1"
    assert first[0].support_streak == 1
    assert repeated == ()


def test_fact_used_to_build_mechanism_is_not_future_confirmation() -> None:
    assessment = _fact_assessment()
    mechanism = assessment.mechanisms[0]
    baseline_chain = tuple(
        node.model_copy(update={"evidence_ids": ("flow-revision-1",)})
        for node in mechanism.causal_chain
    )
    assessment = assessment.model_copy(
        update={
            "mechanisms": (
                mechanism.model_copy(update={"causal_chain": baseline_chain}),
            )
        }
    )

    observation = observe_world_model(
        assessment,
        _fact_packet(
            "packet-1",
            at=NOW + timedelta(minutes=30),
            revision_id="flow-revision-1",
            net_inflow="100",
        ),
    )

    assert observation == ()


def test_new_fact_revision_can_confirm_persistence() -> None:
    assessment = _fact_assessment()
    first = observe_world_model(
        assessment,
        _fact_packet(
            "packet-1",
            at=NOW + timedelta(minutes=30),
            revision_id="flow-revision-1",
            net_inflow="100",
        ),
    )

    second = observe_world_model(
        assessment,
        _fact_packet(
            "packet-2",
            at=NOW + timedelta(minutes=60),
            revision_id="flow-revision-2",
            net_inflow="120",
        ),
        previous=first,
    )

    assert second[0].feature_observation_ref == "flow-revision-2"
    assert second[0].support_streak == 2
    assert second[0].resolution == ContextVerificationResolution.SUPPORTED


def test_world_model_test_does_not_observe_after_frozen_window() -> None:
    assert (
        observe_world_model(
            _assessment(),
            _packet("packet-late", at=NOW + timedelta(hours=5), return_fraction="0.02"),
        )
        == ()
    )


def test_world_model_streak_continues_across_explicit_mechanism_lineage() -> None:
    first_assessment = _assessment()
    first = observe_world_model(
        first_assessment,
        _packet("packet-1", at=NOW + timedelta(minutes=30), return_fraction="0.02"),
    )
    predecessor = first_assessment.mechanisms[0]
    successor = predecessor.model_copy(
        update={
            "mechanism_id": "mechanism-2",
            "continuity_ref": predecessor.mechanism_id,
        }
    )
    second_assessment = first_assessment.model_copy(
        update={
            "assessment_id": "assessment-2",
            "available_at": NOW + timedelta(minutes=31),
            "mechanisms": (successor,),
        }
    )

    second = observe_world_model(
        second_assessment,
        _packet("packet-2", at=NOW + timedelta(minutes=60), return_fraction="0.03"),
        previous=first,
    )

    assert second[0].assessment_id == "assessment-2"
    assert second[0].support_streak == 2
    assert second[0].resolution == ContextVerificationResolution.SUPPORTED


def test_identical_tests_do_not_share_streak_across_unrelated_mechanisms() -> None:
    assessment = _assessment()
    first = observe_world_model(
        assessment,
        _packet("packet-1", at=NOW + timedelta(minutes=30), return_fraction="0.02"),
    )
    predecessor = assessment.mechanisms[0]
    continued = predecessor.model_copy(
        update={
            "mechanism_id": "mechanism-2",
            "continuity_ref": predecessor.mechanism_id,
        }
    )
    unrelated = predecessor.model_copy(
        update={
            "mechanism_id": "mechanism-3",
            "continuity_ref": None,
            "relationship": ContextMechanismRelationship.ALTERNATIVE,
        }
    )
    successor = assessment.model_copy(
        update={
            "assessment_id": "assessment-2",
            "available_at": NOW + timedelta(minutes=31),
            "mechanisms": (continued, unrelated),
        }
    )

    observations = observe_world_model(
        successor,
        _packet("packet-2", at=NOW + timedelta(minutes=60), return_fraction="0.03"),
        previous=first,
    )

    assert observations[0].support_streak == 2
    assert observations[0].resolution == ContextVerificationResolution.SUPPORTED
    assert observations[1].support_streak == 1
    assert observations[1].resolution == ContextVerificationResolution.PENDING


def test_streak_resets_after_verification_policy_change() -> None:
    first_assessment = _assessment()
    first = observe_world_model(
        first_assessment,
        _packet("packet-1", at=NOW + timedelta(minutes=30), return_fraction="0.02"),
    )
    legacy_observation = first[0].model_copy(update={"verification_policy_version": None})
    predecessor = first_assessment.mechanisms[0]
    successor = predecessor.model_copy(
        update={
            "mechanism_id": "mechanism-2",
            "continuity_ref": predecessor.mechanism_id,
        }
    )
    second_assessment = first_assessment.model_copy(
        update={
            "assessment_id": "assessment-2",
            "available_at": NOW + timedelta(minutes=31),
            "mechanisms": (successor,),
        }
    )

    second = observe_world_model(
        second_assessment,
        _packet("packet-2", at=NOW + timedelta(minutes=60), return_fraction="0.03"),
        previous=(legacy_observation,),
    )

    assert second[0].support_streak == 1
    assert second[0].resolution == ContextVerificationResolution.PENDING
