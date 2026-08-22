from datetime import UTC, datetime, timedelta
from decimal import Decimal

from investment_manager.forecast.models import (
    BaseForecast,
    ContextAssessment,
    ContextCapitalRelevance,
    ContextCapitalRelevanceStatus,
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
    validate_context_capital_runtime_plan,
)
from investment_manager.governance.models import ReleaseManifest
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.market.models import InstrumentId
from investment_manager.settings import load_config

START = datetime(2026, 9, 1, tzinfo=UTC)


def _spec() -> ContextCapitalForwardSpec:
    return ContextCapitalForwardSpec(
        plan_id="context-carry-forward-v1",
        analysis_scope="primary-portfolio",
        analysis_behavior_hash="a" * 64,
        objective_id="btc-calendar-carry-entry-veto-v1",
        producer_id="btc-spot-perp-carry",
        producer_version="btc-carry-monthly-first-open-v4",
        forecast_family="delta-neutral-funding-carry",
        forecast_evaluation_version="analysis-forecast-v3",
        signal_window_start=START,
        signal_window_end=START + timedelta(days=84),
        minimum_opportunity_count=3,
        round_trip_cost_bps=Decimal("20"),
    )


def _forecast_and_outcome(index: int, *, gross_bps: Decimal):
    available_at = START + timedelta(days=index * 28)
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
    forecast_id = f"forecast-{index}"
    forecast = BaseForecast(
        forecast_id=forecast_id,
        producer_id="btc-spot-perp-carry",
        producer_version="btc-carry-monthly-first-open-v4",
        forecast_family="delta-neutral-funding-carry",
        target=target,
        horizon_minutes=60,
        direction=DirectionalView.UP,
        reference_prices=(
            ForecastReferencePrice(
                instrument_id=instrument.key,
                price=Decimal("100"),
            ),
        ),
        observed_at=available_at,
        available_at=available_at,
        valid_until=available_at + timedelta(minutes=30),
        raw_score=Decimal("1"),
        input_refs=(f"input-{index}",),
    )
    outcome = ForecastOutcome(
        outcome_id=stable_id(
            "forecast_outcome",
            forecast_id,
            "analysis-forecast-v3",
        ),
        forecast_id=forecast_id,
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


def _assessment(index: int, status: ContextCapitalRelevanceStatus):
    available_at = START + timedelta(days=index * 28, hours=-1)
    return ContextAssessment(
        assessment_id=f"assessment-{index}",
        analysis_scope="primary-portfolio",
        mandate_version="primary-portfolio-mandate-v6",
        as_of=available_at - timedelta(minutes=1),
        available_at=available_at,
        analysis_behavior_hash="a" * 64,
        decision_packet_hash=f"{index + 1:064x}",
        trigger_ids=(f"trigger-{index}",),
        market_mechanism="外生风险通过交易场所完整性影响双腿 carry。",
        capital_relevance=ContextCapitalRelevance(
            objective_id="btc-calendar-carry-entry-veto-v1",
            status=status,
            thesis="当前增量风险需要在自然机会中配对验证。",
            transmission="外生事件可能破坏双腿同时成交与资金费率持续性。",
            evidence_ids=(f"evidence-{index}",),
            invalidation_conditions=("双腿流动性与 funding 恢复稳定",),
        ),
    )


def test_context_veto_is_evaluated_only_as_paired_incremental_return() -> None:
    spec = _spec()
    inputs = tuple(
        _forecast_and_outcome(index, gross_bps=Decimal("-10"))
        for index in range(3)
    )
    assessments = tuple(
        _assessment(index, ContextCapitalRelevanceStatus.ENTRY_VETO_CANDIDATE)
        for index in range(3)
    )

    result = evaluate_context_capital_forward_plan(
        spec=spec,
        forecasts_and_outcomes=inputs,
        assessments=assessments,
        incomplete_forecast_ids=(),
        published_at=spec.signal_window_end + timedelta(days=40),
    )

    assert result.outcome == ContextCapitalForwardOutcome.PASSED
    assert result.natural_opportunity_count == 3
    assert result.paired_opportunity_count == 3
    assert result.veto_count == 3
    assert result.fallback_count == 0
    assert result.base_average_net_return_bps == Decimal("-30")
    assert result.context_average_net_return_bps == Decimal("0")
    assert result.return_delta_lower_bound_bps == Decimal("30.00")


def test_missing_context_falls_back_to_program_and_cannot_create_alpha() -> None:
    spec = _spec()
    inputs = tuple(
        _forecast_and_outcome(index, gross_bps=Decimal("50"))
        for index in range(3)
    )

    result = evaluate_context_capital_forward_plan(
        spec=spec,
        forecasts_and_outcomes=inputs,
        assessments=(),
        incomplete_forecast_ids=(),
        published_at=spec.signal_window_end + timedelta(days=40),
    )

    assert result.outcome == ContextCapitalForwardOutcome.FAILED
    assert result.fallback_count == 3
    assert result.average_return_delta_bps == Decimal("0")
    assert result.return_delta_lower_bound_bps == Decimal("0.00")


def test_incomplete_natural_opportunity_prevents_a_false_pass() -> None:
    spec = _spec()
    inputs = tuple(
        _forecast_and_outcome(index, gross_bps=Decimal("-10"))
        for index in range(3)
    )
    assessments = tuple(
        _assessment(index, ContextCapitalRelevanceStatus.ENTRY_VETO_CANDIDATE)
        for index in range(3)
    )

    result = evaluate_context_capital_forward_plan(
        spec=spec,
        forecasts_and_outcomes=inputs,
        assessments=assessments,
        incomplete_forecast_ids=("forecast-unscorable",),
        published_at=spec.signal_window_end + timedelta(days=40),
    )

    assert result.outcome == ContextCapitalForwardOutcome.INCONCLUSIVE
    assert result.natural_opportunity_count == 4
    assert result.paired_opportunity_count == 3
    assert result.incomplete_forecast_ids == ("forecast-unscorable",)
    assert "PROGRAM_FORECAST_OUTCOMES_INCOMPLETE" in result.reason_codes


def test_context_capital_plan_is_registered_before_the_first_opportunity() -> None:
    spec = _spec()

    plan = build_context_capital_forward_plan(
        spec=spec,
        base_manifest_id="release-context-v72",
        registered_at=START - timedelta(minutes=1),
    )

    assert plan.minimum_sample_size == 3
    assert plan.primary_metric == "paired_net_return_delta_lower_bound_bps"
    assert plan.candidate_spec_hash


def test_context_worker_can_start_before_the_signal_window() -> None:
    from investment_manager.forecast.context.analyst import (
        configured_assess_behavior_hash,
    )

    config = load_config("config/investment-manager.yaml")
    objective = config.assessment.mandate.capital_objective
    assert objective is not None
    spec = _spec().model_copy(
        update={
            "analysis_behavior_hash": configured_assess_behavior_hash(config),
            "analysis_scope": config.assessment.mandate.analysis_scope,
            "objective_id": objective.objective_id,
            "producer_id": objective.producer_id,
            "producer_version": objective.producer_version,
            "forecast_family": objective.forecast_family,
            "forecast_evaluation_version": config.outcome_evaluation.forecast_version,
        }
    )
    manifest = ReleaseManifest(
        manifest_id="release-context-test",
        created_at=START - timedelta(days=2),
        status="CHALLENGER",
        code_version="test-code",
        configuration_hash=content_hash(config),
        component_versions=(),
        constitution_version="constitution-v1",
    )
    plan = build_context_capital_forward_plan(
        spec=spec,
        base_manifest_id=manifest.manifest_id,
        registered_at=START - timedelta(days=2),
    )

    selected_spec, selected_plan = validate_context_capital_runtime_plan(
        config=config,
        manifest=manifest,
        plans=(plan,),
        started_at=START - timedelta(days=1),
    )

    assert selected_spec == spec
    assert selected_plan == plan
