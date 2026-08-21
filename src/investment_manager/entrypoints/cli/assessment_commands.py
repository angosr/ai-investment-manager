from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from investment_manager.entrypoints.cli.root import app
from investment_manager.entrypoints.cli.support import (
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
from investment_manager.governance.repository import SqlGovernanceRepository
from investment_manager.kernel.identity import content_hash
from investment_manager.settings import load_config


@app.command("register-assessment-forward-plan")
def register_assessment_forward_plan(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[
        str,
        typer.Option(envvar="INVESTMENT_MANAGER_DATABASE_URL", help="EvaluationPlan 事实库"),
    ],
    plan_id: Annotated[str, typer.Option()],
    signal_window_start: Annotated[str, typer.Option()],
    signal_window_end: Annotated[str, typer.Option()],
    analysis_behavior_hash: Annotated[str | None, typer.Option()] = None,
    minimum_non_overlapping_samples: Annotated[int, typer.Option(min=2)] = 30,
) -> None:
    """在首个结果发生前冻结 ContextAssessment 前向评价窗口。"""

    loaded = load_config(config)
    expected_behavior_hash = configured_assess_behavior_hash(loaded)
    if (
        analysis_behavior_hash is not None
        and analysis_behavior_hash != expected_behavior_hash
    ):
        raise typer.BadParameter(
            "analysis-behavior-hash 与所加载配置的实际行为哈希不一致",
            param_hint="analysis-behavior-hash",
        )
    registered_at = datetime.now(UTC)
    try:
        spec = AssessmentForwardEvaluationSpec(
            plan_id=plan_id,
            analysis_scope=loaded.assessment.mandate.analysis_scope,
            analysis_behavior_hash=expected_behavior_hash,
            outcome_evaluation_version=loaded.outcome_evaluation.assessment_version,
            signal_window_start=parse_utc_option(
                signal_window_start, name="signal-window-start"
            ),
            signal_window_end=parse_utc_option(
                signal_window_end, name="signal-window-end"
            ),
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
            settlement_grace_minutes=(
                loaded.outcome_evaluation.settlement_grace_minutes
            ),
        )
        engine = runtime_engine(database_url)
        governance = SqlGovernanceRepository(engine)
        plan = build_assessment_forward_plan(
            spec=spec,
            base_manifest_id=governance.get_champion().manifest_id,
            registered_at=registered_at,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    governance.register_plan(plan)
    typer.echo(
        json.dumps(
            {
                "evaluation_plan": plan.model_dump(mode="json"),
                "assessment_spec": spec.model_dump(mode="json"),
                "assessment_spec_hash": content_hash(spec),
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
    evaluation_catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/context-assessment-forward-evaluations"
    ),
) -> None:
    """只按预登记 signal-time 窗口评价 ContextAssessment。"""

    publication = parse_utc_option(published_at, name="published-at")
    if publication > datetime.now(UTC):
        raise typer.BadParameter("published-at 不能晚于当前时间")
    engine = runtime_engine(database_url)
    governance = SqlGovernanceRepository(engine)
    plan = governance.get_plan(plan_id)
    if plan is None or plan.candidate_spec_snapshot is None:
        raise typer.BadParameter("前向预测 EvaluationPlan 不存在", param_hint="plan-id")
    reject_invalidated_evaluation_plan(governance, plan_id)
    try:
        spec = AssessmentForwardEvaluationSpec.model_validate(
            plan.candidate_spec_snapshot
        )
        validate_assessment_forward_plan(
            spec=spec,
            plan=plan,
            champion_manifest_id=governance.get_champion().manifest_id,
            published_at=publication,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="plan-id") from exc
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
    result_path = AssessmentForwardEvaluationCatalog(evaluation_catalog).store(
        result
    )
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
