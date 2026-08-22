from datetime import UTC, datetime
from decimal import Decimal

import pytest

from investment_manager.governance.evaluation.capital import (
    CapitalLedgerProjection,
    CapitalShadowEvaluationCatalog,
    CapitalShadowEvaluationSpec,
    CapitalShadowEvaluationStatus,
    build_capital_shadow_evaluation_plan,
    evaluate_capital_shadow_plan,
    validate_capital_shadow_evaluation_plan,
)
from investment_manager.governance.models import (
    EvaluationStage,
    ReleaseArtifact,
    load_release_manifest,
)
from investment_manager.kernel.identity import content_hash
from investment_manager.settings import load_config

PLAN_ID = "capital-shadow-dynamic-v1"


def _release():
    config = load_config("config/investment-manager.shadow.yaml")
    historical = load_release_manifest("config/release-manifest.yaml")
    evidence = config.carry_forecast.evidence
    assert evidence is not None
    manifest = historical.model_copy(
        update={
            "manifest_id": "release-capital-shadow-evaluation-test",
            "code_version": "a" * 40,
            "configuration_hash": content_hash(config),
            "component_versions": tuple(
                (name, getattr(config, name).version)
                for name, _version in historical.component_versions
            ),
            "artifacts": (
                ReleaseArtifact(
                    artifact_id=evidence.source_evaluation_id,
                    sha256=evidence.source_artifact_sha256,
                ),
            ),
        }
    )
    return config, manifest


def test_capital_shadow_plan_freezes_release_baselines_and_failure_rules() -> None:
    config, manifest = _release()
    start = datetime(2026, 9, 1, tzinfo=UTC)
    spec = CapitalShadowEvaluationSpec.freeze(
        plan_id=PLAN_ID,
        config=config,
        manifest=manifest,
        observation_start=start,
        observation_end=datetime(2027, 9, 1, tzinfo=UTC),
    )
    plan = build_capital_shadow_evaluation_plan(
        spec=spec,
        registered_at=datetime(2026, 8, 21, tzinfo=UTC),
    )

    assert spec.source_policy_version == "spot-perp-calendar-month-risk-30pct-v3"
    assert spec.version == "capital-shadow-evaluation-spec-v4"
    assert spec.equity_boundary_rule == ("EARLIEST_AUTHORITATIVE_REVISION_AT_OR_AFTER_BOUNDARY")
    assert "ONE_ACTIVE_DYNAMIC_PRODUCER" in spec.behavior_contract
    assert "MONTHLY_COUNTERFACTUAL_DOES_NOT_CONSUME_CAPITAL" in (spec.behavior_contract)
    assert spec.thresholds.calendar_months == 12
    assert spec.thresholds.minimum_forecast_available_months == 11
    assert spec.thresholds.minimum_decision_complete_months == 12
    assert spec.thresholds.maximum_duplicate_execution_groups == 0
    assert spec.thresholds.maximum_source_policy_underperformance_fraction == Decimal("0.005")
    assert spec.thresholds.maximum_account_boundary_delay_seconds == (
        config.trigger.heartbeat_minutes * 60 + config.temporal.activity_schedule_to_close_seconds
    )
    assert "COMPENSATION_LOSS" in spec.accounting_dimensions
    assert plan.base_manifest_id == manifest.manifest_id
    assert plan.required_stages[-1] == EvaluationStage.SHADOW
    assert "AUTHORITATIVE_ACCOUNT_BOUNDARIES_WITHIN_DELAY" in plan.hard_guardrails
    assert plan.fixed_regression_suite_version.endswith("v3")
    assert plan.candidate_spec_hash == content_hash(spec)


def test_capital_shadow_plan_rejects_short_or_retrospective_windows() -> None:
    config, manifest = _release()
    with pytest.raises(ValueError, match="至少需要十二个"):
        CapitalShadowEvaluationSpec.freeze(
            plan_id=PLAN_ID,
            config=config,
            manifest=manifest,
            observation_start=datetime(2026, 9, 1, tzinfo=UTC),
            observation_end=datetime(2027, 8, 1, tzinfo=UTC),
        )

    spec = CapitalShadowEvaluationSpec.freeze(
        plan_id=PLAN_ID,
        config=config,
        manifest=manifest,
        observation_start=datetime(2026, 9, 1, tzinfo=UTC),
        observation_end=datetime(2027, 9, 1, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="首个观察月开始前"):
        build_capital_shadow_evaluation_plan(
            spec=spec,
            registered_at=datetime(2026, 9, 1, tzinfo=UTC),
        )


def test_capital_shadow_startup_requires_one_exact_preregistered_contract() -> None:
    config, manifest = _release()
    spec = CapitalShadowEvaluationSpec.freeze(
        plan_id=PLAN_ID,
        config=config,
        manifest=manifest,
        observation_start=datetime(2026, 9, 1, tzinfo=UTC),
        observation_end=datetime(2027, 9, 1, tzinfo=UTC),
    )
    plan = build_capital_shadow_evaluation_plan(
        spec=spec,
        registered_at=datetime(2026, 8, 21, tzinfo=UTC),
    )

    assert validate_capital_shadow_evaluation_plan(
        config=config,
        manifest=manifest,
        plans=(plan,),
        started_at=datetime(2026, 8, 22, tzinfo=UTC),
    ) == (spec, plan)

    with pytest.raises(ValueError, match="恰好绑定一个"):
        validate_capital_shadow_evaluation_plan(
            config=config,
            manifest=manifest,
            plans=(),
            started_at=datetime(2026, 8, 22, tzinfo=UTC),
        )

    with pytest.raises(ValueError, match="完整合同不一致"):
        validate_capital_shadow_evaluation_plan(
            config=config,
            manifest=manifest,
            plans=(plan.model_copy(update={"primary_metric": "gross_return"}),),
            started_at=datetime(2026, 8, 22, tzinfo=UTC),
        )


def test_capital_shadow_startup_rejects_ambiguous_or_future_registration() -> None:
    config, manifest = _release()
    start = datetime(2026, 9, 1, tzinfo=UTC)
    first_spec = CapitalShadowEvaluationSpec.freeze(
        plan_id=PLAN_ID,
        config=config,
        manifest=manifest,
        observation_start=start,
        observation_end=datetime(2027, 9, 1, tzinfo=UTC),
    )
    first = build_capital_shadow_evaluation_plan(
        spec=first_spec,
        registered_at=datetime(2026, 8, 21, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="恰好绑定一个"):
        validate_capital_shadow_evaluation_plan(
            config=config,
            manifest=manifest,
            plans=(first, first),
            started_at=datetime(2026, 8, 22, tzinfo=UTC),
        )

    future = build_capital_shadow_evaluation_plan(
        spec=first_spec,
        registered_at=datetime(2026, 8, 30, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="晚于本次服务启动"):
        validate_capital_shadow_evaluation_plan(
            config=config,
            manifest=manifest,
            plans=(future,),
            started_at=datetime(2026, 8, 22, tzinfo=UTC),
        )


def _projection(
    *,
    plan_id: str,
    monthly_return: Decimal,
    counterfactual: Decimal | None,
    starting_equity: Decimal = Decimal("10000"),
) -> CapitalLedgerProjection:
    monthly = (monthly_return,) * 12
    starting = starting_equity
    ending = starting
    for value in monthly:
        ending *= Decimal("1") + value
    net = ending - starting
    funding = Decimal("100")
    fee = Decimal("50")
    return CapitalLedgerProjection.create(
        plan_id=plan_id,
        projected_at=datetime(2027, 9, 8, tzinfo=UTC),
        source_ids=("account-final", "cycles-window", "counterfactual-window"),
        monthly_net_return_fractions=monthly,
        forecast_available_months=12,
        decision_complete_months=12,
        late_entry_count=0,
        duplicate_execution_group_count=0,
        unresolved_execution_group_count=0,
        maximum_unhedged_seconds=1,
        maximum_group_recovery_seconds=2,
        starting_equity=starting,
        ending_equity=ending,
        price_pnl=net - funding + fee,
        funding_pnl=funding,
        fee_cost=fee,
        execution_slippage_cost=Decimal("5"),
        compensation_loss=Decimal("0"),
        net_pnl=net,
        maximum_drawdown_fraction=Decimal("0.01"),
        minimum_margin_buffer_fraction=Decimal("0.70"),
        source_counterfactual_annualized_return_fraction=counterfactual,
    )


def test_capital_evaluator_is_incomplete_before_frozen_window_matures() -> None:
    config, manifest = _release()
    spec = CapitalShadowEvaluationSpec.freeze(
        plan_id=PLAN_ID,
        config=config,
        manifest=manifest,
        observation_start=datetime(2026, 9, 1, tzinfo=UTC),
        observation_end=datetime(2027, 9, 1, tzinfo=UTC),
    )
    plan = build_capital_shadow_evaluation_plan(
        spec=spec,
        registered_at=datetime(2026, 8, 21, tzinfo=UTC),
    )

    result = evaluate_capital_shadow_plan(
        spec=spec,
        plan=plan,
        projection=None,
        published_at=datetime(2027, 9, 7, 23, 59, tzinfo=UTC),
    )

    assert result.status == CapitalShadowEvaluationStatus.INCOMPLETE
    assert result.metrics is None
    assert result.reason_codes == ("WINDOW_OR_SETTLEMENT_GRACE_NOT_MATURE",)


def test_capital_evaluator_passes_complete_fee_reconciled_projection() -> None:
    config, manifest = _release()
    spec = CapitalShadowEvaluationSpec.freeze(
        plan_id=PLAN_ID,
        config=config,
        manifest=manifest,
        observation_start=datetime(2026, 9, 1, tzinfo=UTC),
        observation_end=datetime(2027, 9, 1, tzinfo=UTC),
    )
    plan = build_capital_shadow_evaluation_plan(
        spec=spec,
        registered_at=datetime(2026, 8, 21, tzinfo=UTC),
    )
    projection = _projection(
        plan_id=PLAN_ID,
        monthly_return=Decimal("0.01"),
        counterfactual=Decimal("0.10"),
    )

    result = evaluate_capital_shadow_plan(
        spec=spec,
        plan=plan,
        projection=projection,
        published_at=datetime(2027, 9, 8, tzinfo=UTC),
    )

    assert result.status == CapitalShadowEvaluationStatus.PASSED
    assert result.reason_codes == ()
    assert result.metrics is not None
    assert result.metrics.annualized_net_return_fraction == (
        projection.ending_equity / projection.starting_equity - Decimal("1")
    )
    assert result.metrics.net_pnl == projection.net_pnl


def test_v4_evaluator_accepts_authoritative_observation_boundary_equity() -> None:
    config, manifest = _release()
    spec = CapitalShadowEvaluationSpec.freeze(
        plan_id=PLAN_ID,
        config=config,
        manifest=manifest,
        observation_start=datetime(2026, 9, 1, tzinfo=UTC),
        observation_end=datetime(2027, 9, 1, tzinfo=UTC),
    )
    plan = build_capital_shadow_evaluation_plan(
        spec=spec,
        registered_at=datetime(2026, 8, 21, tzinfo=UTC),
    )
    projection = _projection(
        plan_id=PLAN_ID,
        monthly_return=Decimal("0.01"),
        counterfactual=Decimal("0.10"),
        starting_equity=Decimal("9876.54"),
    )

    result = evaluate_capital_shadow_plan(
        spec=spec,
        plan=plan,
        projection=projection,
        published_at=datetime(2027, 9, 8, tzinfo=UTC),
    )

    assert result.status == CapitalShadowEvaluationStatus.PASSED
    assert result.metrics is not None
    assert result.metrics.starting_equity == Decimal("9876.54")
    assert spec.starting_equity == Decimal("10000")


def test_capital_evaluator_fails_without_positive_fee_after_cost_edge() -> None:
    config, manifest = _release()
    spec = CapitalShadowEvaluationSpec.freeze(
        plan_id=PLAN_ID,
        config=config,
        manifest=manifest,
        observation_start=datetime(2026, 9, 1, tzinfo=UTC),
        observation_end=datetime(2027, 9, 1, tzinfo=UTC),
    )
    plan = build_capital_shadow_evaluation_plan(
        spec=spec,
        registered_at=datetime(2026, 8, 21, tzinfo=UTC),
    )

    result = evaluate_capital_shadow_plan(
        spec=spec,
        plan=plan,
        projection=_projection(
            plan_id=PLAN_ID,
            monthly_return=Decimal("-0.01"),
            counterfactual=Decimal("0.02"),
        ),
        published_at=datetime(2027, 9, 8, tzinfo=UTC),
    )

    assert result.status == CapitalShadowEvaluationStatus.FAILED
    assert "ANNUALIZED_RETURN_LOWER_BOUND_NOT_POSITIVE" in result.reason_codes
    assert "SOURCE_COUNTERFACTUAL_UNDERPERFORMANCE_EXCEEDED" in result.reason_codes


def test_capital_evaluation_catalog_keeps_the_exact_ledger_projection(tmp_path) -> None:
    config, manifest = _release()
    spec = CapitalShadowEvaluationSpec.freeze(
        plan_id=PLAN_ID,
        config=config,
        manifest=manifest,
        observation_start=datetime(2026, 9, 1, tzinfo=UTC),
        observation_end=datetime(2027, 9, 1, tzinfo=UTC),
    )
    plan = build_capital_shadow_evaluation_plan(
        spec=spec,
        registered_at=datetime(2026, 8, 21, tzinfo=UTC),
    )
    projection = _projection(
        plan_id=PLAN_ID,
        monthly_return=Decimal("0.01"),
        counterfactual=Decimal("0.10"),
    )
    result = evaluate_capital_shadow_plan(
        spec=spec,
        plan=plan,
        projection=projection,
        published_at=datetime(2027, 9, 8, tzinfo=UTC),
    )

    catalog = CapitalShadowEvaluationCatalog(tmp_path)
    first = catalog.store(result, projection=projection)
    replay = catalog.store(result, projection=projection)
    loaded = catalog.load(result.result_id)

    assert replay == first
    assert loaded.result == result
    assert loaded.projection == projection
