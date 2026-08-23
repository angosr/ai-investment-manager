from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from investment_manager.entrypoints.cli.root import app
from investment_manager.entrypoints.cli.support import (
    configured_fact_store_role,
    load_runtime_release,
    parse_utc_option,
    reject_invalidated_evaluation_plan,
    runtime_engine,
)
from investment_manager.forecast.context.analyst import (
    configured_assess_behavior_hash,
)
from investment_manager.forecast.context.settlement import (
    SqlAssessmentViewOutcomeStore,
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
)
from investment_manager.governance.evaluation.context_capital import (
    ContextCapitalForwardCatalog,
    ContextCapitalForwardOutcome,
    ContextCapitalForwardSpec,
    build_context_capital_forward_plan,
    evaluate_context_capital_forward_plan,
    failed_context_capital_experiment,
    load_context_capital_inputs,
    validate_context_capital_forward_plan,
)
from investment_manager.governance.repository import SqlGovernanceRepository
from investment_manager.kernel.identity import content_hash
from investment_manager.platform.fact_store import FactStoreRole


@app.command("register-assessment-forward-plan")
def register_assessment_forward_plan(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[
        str,
        typer.Option(envvar="INVESTMENT_MANAGER_DATABASE_URL", help="EvaluationPlan 事实库"),
    ],
    release_manifest: Annotated[
        Path,
        typer.Option("--release-manifest", exists=True, dir_okay=False),
    ],
    plan_id: Annotated[str, typer.Option()],
    signal_window_start: Annotated[str, typer.Option()],
    signal_window_end: Annotated[str, typer.Option()],
    analysis_behavior_hash: Annotated[str | None, typer.Option()] = None,
    minimum_non_overlapping_samples: Annotated[int, typer.Option(min=2)] = 30,
    minimum_capital_opportunities: Annotated[int, typer.Option(min=3)] = 12,
) -> None:
    """在首个结果发生前冻结当前 Context 行为的前向评价窗口。"""

    loaded, manifest = load_runtime_release(config, release_manifest)
    expected_behavior_hash = configured_assess_behavior_hash(loaded)
    if analysis_behavior_hash is not None and analysis_behavior_hash != expected_behavior_hash:
        raise typer.BadParameter(
            "analysis-behavior-hash 与所加载配置的实际行为哈希不一致",
            param_hint="analysis-behavior-hash",
        )
    registered_at = datetime.now(UTC)
    try:
        start = parse_utc_option(signal_window_start, name="signal-window-start")
        end = parse_utc_option(signal_window_end, name="signal-window-end")
        objective = loaded.assessment.mandate.capital_objective
        if objective is not None:
            round_trip_cost_bps = sum(
                (item.fee_bps for item in loaded.capital.execution_specs),
                start=loaded.frequency.latency_bps
                + loaded.frequency.adverse_selection_bps
                + loaded.frequency.uncertainty_buffer_bps,
            )
            spec = ContextCapitalForwardSpec(
                plan_id=plan_id,
                analysis_scope=loaded.assessment.mandate.analysis_scope,
                analysis_behavior_hash=expected_behavior_hash,
                objective_id=objective.objective_id,
                producer_id=objective.producer_id,
                producer_version=objective.producer_version,
                forecast_family=objective.forecast_family,
                forecast_evaluation_version=(loaded.outcome_evaluation.forecast_version),
                signal_window_start=start,
                signal_window_end=end,
                minimum_opportunity_count=minimum_capital_opportunities,
                round_trip_cost_bps=round_trip_cost_bps,
                lower_confidence_z=loaded.calibration.lower_confidence_z,
            )
        else:
            spec = AssessmentForwardEvaluationSpec(
                plan_id=plan_id,
                analysis_scope=loaded.assessment.mandate.analysis_scope,
                analysis_behavior_hash=expected_behavior_hash,
                outcome_evaluation_version=(loaded.outcome_evaluation.assessment_version),
                signal_window_start=start,
                signal_window_end=end,
                scopes=tuple(
                    sorted(
                        (
                            AssessmentEvaluationScope(
                                asset=asset.asset,
                                symbol=asset.market_symbol,
                                horizon_minutes=horizon,
                            )
                            for asset in loaded.assessment.mandate.assets
                            for horizon in asset.horizons_minutes
                        ),
                        key=lambda item: (
                            item.asset,
                            item.symbol,
                            item.horizon_minutes,
                        ),
                    )
                ),
                minimum_non_overlapping_samples=minimum_non_overlapping_samples,
                settlement_grace_minutes=(loaded.outcome_evaluation.settlement_grace_minutes),
            )
        engine = runtime_engine(
            database_url,
            fact_store_role=configured_fact_store_role(loaded),
            claim_fact_store=True,
        )
        governance = SqlGovernanceRepository(engine)
        governance.record_release(manifest)
        plan = (
            build_context_capital_forward_plan(
                spec=spec,
                base_manifest_id=manifest.manifest_id,
                registered_at=registered_at,
            )
            if isinstance(spec, ContextCapitalForwardSpec)
            else build_assessment_forward_plan(
                spec=spec,
                base_manifest_id=manifest.manifest_id,
                registered_at=registered_at,
            )
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    governance.register_plan(plan)
    typer.echo(
        json.dumps(
            {
                "evaluation_plan": plan.model_dump(mode="json"),
                "context_spec": spec.model_dump(mode="json"),
                "context_spec_hash": content_hash(spec),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("evaluate-assessment-forward-plan")
def evaluate_assessment_forward_plan_command(
    database_url: Annotated[
        str,
        typer.Option(envvar="INVESTMENT_MANAGER_DATABASE_URL", help="EvaluationPlan 事实库"),
    ],
    plan_id: Annotated[str, typer.Option()],
    published_at: Annotated[str, typer.Option()],
    capital_database_url: Annotated[
        str | None,
        typer.Option(
            envvar="INVESTMENT_MANAGER_CAPITAL_DATABASE_URL",
            help="资本事实库；Context Capital 计划必填",
        ),
    ] = None,
    evaluation_catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/context-assessment-forward-evaluations"
    ),
) -> None:
    """只按预登记 signal-time 窗口评价 ContextAssessment。"""

    publication = parse_utc_option(published_at, name="published-at")
    if publication > datetime.now(UTC):
        raise typer.BadParameter("published-at 不能晚于当前时间")
    engine = runtime_engine(
        database_url,
        fact_store_role=FactStoreRole.CONTEXT,
        claim_fact_store=False,
    )
    governance = SqlGovernanceRepository(engine)
    plan = governance.get_plan(plan_id)
    if plan is None or plan.candidate_spec_snapshot is None:
        raise typer.BadParameter("前向预测 EvaluationPlan 不存在", param_hint="plan-id")
    reject_invalidated_evaluation_plan(governance, plan_id)
    snapshot = plan.candidate_spec_snapshot
    try:
        if snapshot.get("version") == "context-capital-forward-spec-v1":
            spec = ContextCapitalForwardSpec.model_validate(snapshot)
            validate_context_capital_forward_plan(
                spec=spec,
                plan=plan,
                base_manifest_id=plan.base_manifest_id,
                published_at=publication,
            )
        else:
            spec = AssessmentForwardEvaluationSpec.model_validate(snapshot)
            validate_assessment_forward_plan(
                spec=spec,
                plan=plan,
                champion_manifest_id=governance.get_champion().manifest_id,
                published_at=publication,
            )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="plan-id") from exc
    if isinstance(spec, ContextCapitalForwardSpec):
        if capital_database_url is None:
            raise typer.BadParameter(
                "Context Capital 计划必须显式提供资本事实库",
                param_hint="capital-database-url",
            )
        capital_engine = runtime_engine(
            capital_database_url,
            fact_store_role=FactStoreRole.CAPITAL,
            claim_fact_store=False,
        )
        paired_inputs, assessments, incomplete = load_context_capital_inputs(
            context_engine=engine,
            capital_engine=capital_engine,
            spec=spec,
            published_at=publication,
        )
        result = evaluate_context_capital_forward_plan(
            spec=spec,
            forecasts_and_outcomes=paired_inputs,
            assessments=assessments,
            incomplete_forecast_ids=incomplete,
            published_at=publication,
        )
        result_path = ContextCapitalForwardCatalog(evaluation_catalog).store(result)
        if result.outcome == ContextCapitalForwardOutcome.FAILED:
            governance.record_failed_experiment(
                failed_context_capital_experiment(result, rejected_at=publication)
            )
        payload = result.model_dump(mode="json")
        payload["result_path"] = str(result_path)
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        return
    store = SqlAssessmentViewOutcomeStore(engine)
    pending = store.pending_assessment_count(
        analysis_behavior_hash=spec.analysis_behavior_hash,
        evaluation_version=spec.outcome_evaluation_version,
        signal_window_start=spec.signal_window_start,
        signal_window_end=spec.signal_window_end,
    )
    if pending:
        raise typer.BadParameter(
            f"预登记 signal-time 窗口仍有 {pending} 个未完整结算的 Assessment",
            param_hint="plan-id",
        )
    outcomes = store.visible_outcomes(
        analysis_behavior_hash=spec.analysis_behavior_hash,
        evaluation_version=spec.outcome_evaluation_version,
        signal_window_start=spec.signal_window_start,
        signal_window_end=spec.signal_window_end,
        published_at=publication,
    )
    result = evaluate_assessment_forward_plan(
        spec=spec,
        outcomes=outcomes,
        published_at=publication,
    )
    result_path = AssessmentForwardEvaluationCatalog(evaluation_catalog).store(result)
    if result.outcome == AssessmentForwardOutcome.FAILED:
        governance.record_failed_experiment(
            failed_assessment_forward_experiment(result, rejected_at=publication)
        )
    payload = result.model_dump(mode="json")
    payload["result_path"] = str(result_path)
    typer.echo(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )
