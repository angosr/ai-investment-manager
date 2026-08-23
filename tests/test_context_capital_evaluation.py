from datetime import UTC, datetime, timedelta
from decimal import Decimal

from investment_manager.forecast.context.review import (
    MechanismOpportunityEffect,
    MechanismOpportunityImpact,
    OpportunityAssessment,
)
from investment_manager.forecast.models import (
    BaseForecast,
    ContextCapitalEffect,
    DirectionalView,
    ExposureDirection,
    ForecastKind,
    ForecastLeg,
    ForecastLegOutcome,
    ForecastOutcome,
    ForecastOutcomeStatus,
    ForecastReferencePrice,
    ForecastTarget,
)
from investment_manager.governance.evaluation.context_capital import (
    ContextCapitalForwardOutcome,
    ContextCapitalForwardSpec,
    build_context_capital_forward_plan,
    evaluate_context_capital_forward_plan,
)
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.market.models import InstrumentId

START = datetime(2026, 8, 23, tzinfo=UTC)
BEHAVIOR = "a" * 64


def _spec() -> ContextCapitalForwardSpec:
    return ContextCapitalForwardSpec(
        plan_id="candidate-context-forward-v2",
        opportunity_analysis_behavior_hash=BEHAVIOR,
        producer_id="program-candidate",
        producer_version="program-candidate-v1",
        forecast_family="program-opportunity",
        forecast_evaluation_version="analysis-forecast-v3",
        signal_window_start=START,
        signal_window_end=START + timedelta(days=4),
        minimum_opportunity_count=3,
        round_trip_cost_bps=Decimal("20"),
        lower_confidence_z=Decimal("0.1"),
    )


def _forecast_and_outcome(index: int, *, gross_bps: Decimal):
    available_at = START + timedelta(days=index)
    instrument = InstrumentId.binance_spot(
        symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT"
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
    forecast = BaseForecast(
        forecast_id=f"forecast-{index}",
        producer_id="program-candidate",
        producer_version="program-candidate-v1",
        forecast_family="program-opportunity",
        target=target,
        horizon_minutes=60,
        direction=DirectionalView.UP,
        reference_prices=(
            ForecastReferencePrice(instrument_id=instrument.key, price=Decimal("100")),
        ),
        observed_at=available_at,
        available_at=available_at,
        valid_until=available_at + timedelta(minutes=30),
        raw_score=Decimal("30"),
        input_refs=(f"input-{index}",),
    )
    outcome = ForecastOutcome(
        outcome_id=stable_id(
            "forecast_outcome", forecast.forecast_id, "analysis-forecast-v3"
        ),
        forecast_id=forecast.forecast_id,
        forecast_kind=ForecastKind.BASE,
        producer_id=forecast.producer_id,
        producer_version=forecast.producer_version,
        target_id=target.target_id,
        direction=DirectionalView.UP,
        horizon_minutes=60,
        evaluation_version="analysis-forecast-v3",
        status=ForecastOutcomeStatus.SETTLED,
        available_at=available_at,
        evaluation_at=available_at + timedelta(minutes=60),
        settled_at=available_at + timedelta(minutes=61),
        legs=(
            ForecastLegOutcome(
                instrument_id=instrument.key,
                direction=ExposureDirection.LONG,
                gross_weight=Decimal("1"),
                reference_price=Decimal("100"),
                exit_price=Decimal("100"),
                price_return_bps=gross_bps,
            ),
        ),
        gross_target_return_bps=gross_bps,
        directional_return_bps=gross_bps,
        reason_code="GROSS_TARGET_RETURN_AVAILABLE",
    )
    return forecast, outcome


def _assessment(
    forecast: BaseForecast,
    *,
    effect: ContextCapitalEffect,
    late: bool = False,
) -> OpportunityAssessment:
    available_at = forecast.valid_until + timedelta(seconds=1) if late else (
        forecast.available_at + timedelta(minutes=1)
    )
    payload = {
        "review_id": f"review-{forecast.forecast_id}",
        "opportunity_id": forecast.forecast_id,
        "world_model_id": f"world-{forecast.forecast_id}",
        "analysis_behavior_hash": BEHAVIOR,
        "available_at": available_at,
        "effect": effect,
        "incremental_reason": "世界机制相对程序基线改变了该机会的尾部风险。",
        "mechanism_impacts": (
            MechanismOpportunityImpact(
                mechanism_id="mechanism-1",
                effect=(
                    MechanismOpportunityEffect.OPPOSES
                    if effect == ContextCapitalEffect.OPPOSE
                    else MechanismOpportunityEffect.NEUTRAL
                ),
                transmission_to_opportunity="流动性传导作用于候选收益。",
                evidence_ids=("evidence-1",)
                if effect == ContextCapitalEffect.OPPOSE
                else (),
            ),
        ),
        "invalidation_conditions": ("流动性传导反转。",),
    }
    return OpportunityAssessment(
        assessment_id=stable_id("opportunity_assessment", content_hash(payload)),
        **payload,
    )


def test_exact_candidate_veto_can_only_pass_when_program_base_is_profitable() -> None:
    spec = _spec()
    inputs = (
        _forecast_and_outcome(0, gross_bps=Decimal("60")),
        _forecast_and_outcome(1, gross_bps=Decimal("60")),
        _forecast_and_outcome(2, gross_bps=Decimal("-10")),
    )
    assessments = tuple(
        _assessment(forecast, effect=effect)
        for (forecast, _), effect in zip(
            inputs,
            (
                ContextCapitalEffect.NEUTRAL,
                ContextCapitalEffect.NEUTRAL,
                ContextCapitalEffect.OPPOSE,
            ),
            strict=True,
        )
    )
    result = evaluate_context_capital_forward_plan(
        spec=spec,
        forecasts_and_outcomes=inputs,
        assessments=assessments,
        incomplete_forecast_ids=(),
        published_at=spec.signal_window_end + timedelta(days=40),
    )
    assert result.outcome == ContextCapitalForwardOutcome.PASSED
    assert result.veto_count == 1
    assert result.base_average_net_return_bps > 0
    assert result.context_average_net_return_bps > result.base_average_net_return_bps
    assert result.return_delta_lower_bound_bps > 0


def test_context_cannot_rescue_an_unprofitable_program_base() -> None:
    spec = _spec()
    inputs = tuple(
        _forecast_and_outcome(index, gross_bps=Decimal("-10")) for index in range(3)
    )
    assessments = tuple(
        _assessment(forecast, effect=ContextCapitalEffect.OPPOSE)
        for forecast, _ in inputs
    )
    result = evaluate_context_capital_forward_plan(
        spec=spec,
        forecasts_and_outcomes=inputs,
        assessments=assessments,
        incomplete_forecast_ids=(),
        published_at=spec.signal_window_end + timedelta(days=40),
    )
    assert result.outcome == ContextCapitalForwardOutcome.FAILED
    assert "PROGRAM_BASE_AVERAGE_NET_RETURN_NOT_POSITIVE" in result.reason_codes


def test_missing_or_late_ai_result_preserves_program_base() -> None:
    spec = _spec()
    inputs = tuple(
        _forecast_and_outcome(index, gross_bps=Decimal("50")) for index in range(3)
    )
    late = _assessment(inputs[0][0], effect=ContextCapitalEffect.OPPOSE, late=True)
    result = evaluate_context_capital_forward_plan(
        spec=spec,
        forecasts_and_outcomes=inputs,
        assessments=(late,),
        incomplete_forecast_ids=(),
        published_at=spec.signal_window_end + timedelta(days=40),
    )
    assert result.outcome == ContextCapitalForwardOutcome.FAILED
    assert result.fallback_count == 3
    assert result.average_return_delta_bps == 0


def test_incomplete_natural_opportunity_prevents_false_pass() -> None:
    spec = _spec()
    inputs = tuple(
        _forecast_and_outcome(index, gross_bps=Decimal("50")) for index in range(3)
    )
    result = evaluate_context_capital_forward_plan(
        spec=spec,
        forecasts_and_outcomes=inputs,
        assessments=(),
        incomplete_forecast_ids=("forecast-unscorable",),
        published_at=spec.signal_window_end + timedelta(days=40),
    )
    assert result.outcome == ContextCapitalForwardOutcome.INCONCLUSIVE
    assert "PROGRAM_FORECAST_OUTCOMES_INCOMPLETE" in result.reason_codes


def test_context_capital_plan_is_registered_before_first_opportunity() -> None:
    plan = build_context_capital_forward_plan(
        spec=_spec(),
        base_manifest_id="release-v1",
        registered_at=START - timedelta(minutes=1),
    )
    assert plan.primary_metric == "paired_net_return_delta_lower_bound_bps"
    assert "PROGRAM_BASE_AVERAGE_NET_RETURN_POSITIVE" in plan.hard_guardrails
