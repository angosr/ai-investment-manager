from datetime import UTC, datetime, timedelta
from decimal import Decimal

from investment_manager.forecast.context.verification import (
    observe_world_model,
    predicate_match,
)
from investment_manager.forecast.models import (
    ContextAssessment,
    ContextAssessmentSchemaVersion,
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
from investment_manager.state.decision.packet import DecisionPacket, PacketAssetState

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
        schema_version=ContextAssessmentSchemaVersion.WORLD_MODEL_V2,
        assessment_id="assessment-1",
        available_at=NOW,
        mechanisms=(mechanism,),
    )


def _packet(packet_id: str, *, at: datetime, return_fraction: str) -> DecisionPacket:
    asset = PacketAssetState(
        asset="BTC",
        market_symbol="BTCUSDT",
        observed_at=at,
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


def test_world_model_test_does_not_observe_after_frozen_window() -> None:
    assert observe_world_model(
        _assessment(),
        _packet("packet-late", at=NOW + timedelta(hours=5), return_fraction="0.02"),
    ) == ()
