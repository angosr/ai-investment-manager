from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from investment_manager.forecast.context.settlement import AssessmentViewOutcome
from investment_manager.forecast.models import (
    AssessmentUncertainty,
    DirectionalView,
    ForecastOutcomeStatus,
    PricedState,
)
from investment_manager.governance.evaluation.assessment import (
    AssessmentEvaluationScope,
    AssessmentForwardEvaluationCatalog,
    AssessmentForwardEvaluationSpec,
    AssessmentForwardOutcome,
    build_assessment_forward_plan,
    evaluate_assessment_forward_plan,
    failed_assessment_forward_experiment,
    validate_assessment_forward_plan,
    validate_assessment_runtime_plan,
)
from investment_manager.governance.models import load_release_manifest
from investment_manager.kernel.identity import stable_id
from investment_manager.settings import load_config

START = datetime(2026, 9, 1, tzinfo=UTC)


def _spec(**updates) -> AssessmentForwardEvaluationSpec:
    values = {
        "plan_id": "context-assessment-forward-v1",
        "analysis_scope": "crypto-portfolio",
        "analysis_behavior_hash": "a" * 64,
        "outcome_evaluation_version": "context-assessment-outcome-v1",
        "signal_window_start": START,
        "signal_window_end": START + timedelta(hours=4),
        "scopes": (
            AssessmentEvaluationScope(
                asset="BTC",
                symbol="BTCUSDT",
                horizon_minutes=60,
            ),
        ),
        "minimum_non_overlapping_samples": 2,
        "settlement_grace_minutes": 10,
    }
    values.update(updates)
    return AssessmentForwardEvaluationSpec(**values)


def _outcome(
    index: int,
    *,
    signal_at: datetime,
    status: ForecastOutcomeStatus = ForecastOutcomeStatus.SETTLED,
    market_return_bps: Decimal = Decimal("-10"),
    asset: str = "BTC",
    symbol: str = "BTCUSDT",
) -> AssessmentViewOutcome:
    assessment_id = f"assessment-{index}"
    evaluation_at = signal_at + timedelta(minutes=60)
    scoreable = status != ForecastOutcomeStatus.UNSCORABLE
    abstained = status == ForecastOutcomeStatus.ABSTAINED
    direction = DirectionalView.UNCERTAIN if abstained else DirectionalView.DOWN
    reference_price = Decimal("100") if scoreable else None
    exit_price = (
        Decimal("100")
        * (Decimal("1") + market_return_bps / Decimal("10000"))
        if scoreable
        else None
    )
    directional_return = (
        -market_return_bps if status == ForecastOutcomeStatus.SETTLED else None
    )
    return AssessmentViewOutcome(
        outcome_id=stable_id(
            "assessment_view_outcome",
            assessment_id,
            asset,
            60,
            "context-assessment-outcome-v1",
        ),
        assessment_id=assessment_id,
        decision_packet_hash="b" * 64,
        analysis_scope="crypto-portfolio",
        analysis_behavior_hash="a" * 64,
        evaluation_version="context-assessment-outcome-v1",
        asset=asset,
        symbol=symbol,
        horizon_minutes=60,
        direction=direction,
        already_priced=PricedState.PARTIAL,
        uncertainty=AssessmentUncertainty.MEDIUM,
        status=status,
        signal_observed_at=signal_at,
        evaluation_at=evaluation_at,
        settled_at=evaluation_at + timedelta(minutes=1),
        reference_price=reference_price,
        exit_price=exit_price,
        exit_event_time=evaluation_at if scoreable else None,
        market_return_bps=market_return_bps if scoreable else None,
        directional_return_bps=directional_return,
        direction_correct=(directional_return > 0 if directional_return is not None else None),
        reason_code=(
            "MARKET_DATA_MISSING"
            if status == ForecastOutcomeStatus.UNSCORABLE
            else "DIRECTIONAL_VIEW_ABSTAINED"
            if abstained
            else "DIRECTIONAL_RETURN_AVAILABLE"
        ),
    )


def test_assessment_forward_plan_is_registered_before_a_feasible_window() -> None:
    spec = _spec()
    plan = build_assessment_forward_plan(
        spec=spec,
        base_manifest_id="champion-v1",
        registered_at=START - timedelta(seconds=1),
    )
    publication = spec.signal_window_end + timedelta(minutes=70)

    validate_assessment_forward_plan(
        spec=spec,
        plan=plan,
        champion_manifest_id="champion-v1",
        published_at=publication,
    )
    assert plan.minimum_sample_size == 2
    assert plan.fixed_regression_suite_version == (
        "context-assessment-forward-regression-v1"
    )
    with pytest.raises(ValueError, match="首个信号生成前"):
        build_assessment_forward_plan(
            spec=spec,
            base_manifest_id="champion-v1",
            registered_at=START,
        )
    with pytest.raises(ValueError, match="完整到期"):
        validate_assessment_forward_plan(
            spec=spec,
            plan=plan,
            champion_manifest_id="champion-v1",
            published_at=publication - timedelta(seconds=1),
        )


def test_world_model_without_capital_authority_does_not_require_a_fake_plan() -> None:
    config = load_config("config/investment-manager.yaml")
    manifest = load_release_manifest("config/release-manifest.yaml").model_copy(
        update={"manifest_id": "release-context-runtime-test"}
    )
    start = datetime(2026, 8, 21, 17, tzinfo=UTC)
    assert config.assessment.mandate.capital_objective is None
    assert validate_assessment_runtime_plan(
        config=config,
        manifest=manifest,
        plans=(),
        started_at=start,
    ) is None
def test_assessment_forward_gate_scores_abstention_as_cash_against_always_up() -> None:
    spec = _spec()
    outcomes = (
        _outcome(1, signal_at=START),
        _outcome(
            2,
            signal_at=START + timedelta(minutes=60),
            status=ForecastOutcomeStatus.ABSTAINED,
        ),
    )

    result = evaluate_assessment_forward_plan(
        spec=spec,
        outcomes=outcomes,
        published_at=spec.signal_window_end + timedelta(minutes=70),
    )

    assert result.outcome == AssessmentForwardOutcome.PASSED
    scope = result.scopes[0]
    assert scope.non_overlapping_scoreable_count == 2
    assert scope.abstention_fraction == Decimal("0.5")
    assert scope.average_strategy_return_bps == Decimal("5")
    assert scope.always_up_average_return_bps == Decimal("-10")
    assert scope.average_return_delta_bps_vs_always_up == Decimal("15")
    assert scope.return_delta_bps_lower_bound_vs_always_up > 0


def test_assessment_forward_gate_is_inconclusive_when_scope_is_missing(
    tmp_path,
) -> None:
    spec = _spec(
        scopes=(
            AssessmentEvaluationScope(
                asset="BTC",
                symbol="BTCUSDT",
                horizon_minutes=60,
            ),
            AssessmentEvaluationScope(
                asset="ETH",
                symbol="ETHUSDT",
                horizon_minutes=60,
            ),
        )
    )
    result = evaluate_assessment_forward_plan(
        spec=spec,
        outcomes=(
            _outcome(1, signal_at=START),
            _outcome(2, signal_at=START + timedelta(minutes=60)),
        ),
        published_at=spec.signal_window_end + timedelta(minutes=70),
    )

    assert result.outcome == AssessmentForwardOutcome.INCONCLUSIVE
    assert "EXPECTED_SCOPE_MISSING" in result.reason_codes
    catalog = AssessmentForwardEvaluationCatalog(tmp_path / "forward")
    catalog.store(result)
    assert catalog.load(result.result_id) == result
    with pytest.raises(ValueError, match="只有证据充分"):
        failed_assessment_forward_experiment(
            result,
            rejected_at=result.published_at,
        )


def test_assessment_forward_gate_records_only_evidence_sufficient_failure() -> None:
    spec = _spec()
    result = evaluate_assessment_forward_plan(
        spec=spec,
        outcomes=(
            _outcome(
                1,
                signal_at=START,
                market_return_bps=Decimal("10"),
            ),
            _outcome(
                2,
                signal_at=START + timedelta(minutes=60),
                market_return_bps=Decimal("10"),
            ),
        ),
        published_at=spec.signal_window_end + timedelta(minutes=70),
    )

    assert result.outcome == AssessmentForwardOutcome.FAILED
    failed = failed_assessment_forward_experiment(
        result,
        rejected_at=result.published_at,
    )
    assert failed.reason_codes == (
        "CONTEXT_ASSESSMENT_FORWARD_FAILED",
        *result.reason_codes,
    )


def test_assessment_forward_gate_rejects_out_of_contract_outcomes() -> None:
    spec = _spec()
    wrong_behavior = _outcome(1, signal_at=START).model_copy(
        update={"analysis_behavior_hash": "c" * 64}
    )

    with pytest.raises(ValueError, match="作用域外"):
        evaluate_assessment_forward_plan(
            spec=spec,
            outcomes=(wrong_behavior,),
            published_at=spec.signal_window_end + timedelta(minutes=70),
        )
