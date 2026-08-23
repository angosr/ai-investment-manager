from datetime import UTC, datetime, timedelta
from decimal import Decimal

from investment_manager.forecast.context.review import (
    ContextOverlayPolicy,
    MechanismOpportunityEffect,
    MechanismOpportunityImpact,
    OpportunityAssessmentDraft,
    OpportunityReviewInput,
    OpportunityReviewStructuredOutput,
    apply_context_overlay,
    finalize_opportunity_assessment,
)
from investment_manager.forecast.models import (
    BaseForecast,
    ContextAssessment,
    ContextAssessmentSchemaVersion,
    ContextCapitalEffect,
    ContextCausalNode,
    ContextMechanism,
    ContextMechanismRelationship,
    ContextTransmissionStage,
    ContextVerificationPredicate,
    ContextVerificationTest,
    DirectionalView,
    ExposureDirection,
    ForecastLeg,
    ForecastReferencePrice,
    ForecastTarget,
)
from investment_manager.market.models import InstrumentId

NOW = datetime(2026, 8, 23, 6, tzinfo=UTC)


def _forecast() -> BaseForecast:
    instrument = InstrumentId.binance_spot(
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
    )
    target = ForecastTarget.create(
        (
            ForecastLeg(
                instrument=instrument,
                direction=ExposureDirection.LONG,
                gross_weight=Decimal("1"),
            ),
        )
    )
    return BaseForecast(
        forecast_id="forecast-1",
        producer_id="program-1",
        producer_version="program-1-v1",
        forecast_family="program-opportunity",
        target=target,
        horizon_minutes=240,
        direction=DirectionalView.UP,
        reference_prices=(
            ForecastReferencePrice(instrument_id=instrument.key, price=Decimal("100")),
        ),
        observed_at=NOW,
        available_at=NOW,
        valid_until=NOW + timedelta(minutes=30),
        raw_score=Decimal("30"),
        input_refs=("market-input-1",),
    )


def _world_model() -> ContextAssessment:
    mechanism = ContextMechanism(
        mechanism_id="mechanism-1",
        relationship=ContextMechanismRelationship.THREATENS,
        claim="流动性收缩可能破坏程序机会。",
        horizon_hours=24,
        causal_chain=(
            ContextCausalNode(statement="美元流动性下降。", evidence_ids=("fact-1",)),
            ContextCausalNode(statement="风险资产承压。", evidence_ids=("market-1",)),
        ),
        transmission_stage=ContextTransmissionStage.PROPAGATING,
        verification_tests=(
            ContextVerificationTest(
                feature_selector="asset_state:BTC.return_fraction",
                evaluation_window_minutes=240,
                supports_predicate=ContextVerificationPredicate(
                    operator="LT", value=Decimal("-0.01")
                ),
                contradicts_predicate=ContextVerificationPredicate(
                    operator="GT", value=Decimal("0.01")
                ),
            ),
        ),
        invalidation_conditions=("美元流动性恢复。",),
        next_review_at=NOW + timedelta(hours=1),
    )
    return ContextAssessment(
        schema_version=ContextAssessmentSchemaVersion.WORLD_MODEL_V2,
        assessment_id="world-model-1",
        analysis_scope="primary-portfolio",
        mandate_version="mandate-v1",
        as_of=NOW - timedelta(minutes=10),
        available_at=NOW - timedelta(minutes=5),
        analysis_behavior_hash="a" * 64,
        decision_packet_hash="b" * 64,
        trigger_ids=("trigger-1",),
        synthesis="流动性收缩主导，部分资金流形成抵消。",
        synthesis_horizon_hours=24,
        mechanisms=(mechanism,),
    )


def test_candidate_review_is_bound_to_exact_forecast_world_model_and_evidence() -> None:
    review = OpportunityReviewInput.create(
        forecast=_forecast(),
        world_model=_world_model(),
        estimated_variable_cost_bps=Decimal("20"),
        baseline_net_edge_bps=Decimal("10"),
        portfolio_id="research-overlay",
        account_snapshot_id="account-1",
        account_equity=Decimal("10000"),
        created_at=NOW + timedelta(minutes=1),
    )
    output = OpportunityReviewStructuredOutput(
        opportunity_assessment=OpportunityAssessmentDraft(
            effect=ContextCapitalEffect.OPPOSE,
            incremental_reason="当前传导会使该方向机会的尾部风险高于程序基线。",
            mechanism_impacts=(
                MechanismOpportunityImpact(
                    mechanism_id="mechanism-1",
                    effect=MechanismOpportunityEffect.OPPOSES,
                    transmission_to_opportunity="流动性收缩直接压低标的需求。",
                    evidence_ids=("fact-1", "market-1"),
                ),
            ),
            invalidation_conditions=("美元流动性恢复且价格响应反转。",),
        )
    )
    assessment = finalize_opportunity_assessment(
        output=output,
        review=review,
        analysis_behavior_hash="c" * 64,
        available_at=NOW + timedelta(minutes=2),
    )
    assert assessment.opportunity_id == "forecast-1"
    assert assessment.world_model_id == "world-model-1"

    decision = apply_context_overlay(
        assessment,
        baseline_allocation_fraction=Decimal("0.1"),
        policy=ContextOverlayPolicy(),
    )
    assert decision.baseline_allocation_fraction == Decimal("0.1")
    assert decision.overlay_allocation_fraction == 0
