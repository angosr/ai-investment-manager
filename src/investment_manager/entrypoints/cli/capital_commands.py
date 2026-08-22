from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
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
from investment_manager.governance.evaluation.capital import (
    CapitalShadowEvaluationCatalog,
    CapitalShadowEvaluationSpec,
    build_capital_shadow_evaluation_plan,
    evaluate_capital_shadow_plan,
    validate_capital_shadow_evaluation_plan,
)
from investment_manager.governance.evaluation.capital_ledger import (
    SqlCapitalLedgerProjector,
)
from investment_manager.governance.repository import SqlGovernanceRepository
from investment_manager.kernel.identity import content_hash


@app.command("register-capital-shadow-plan")
def register_capital_shadow_plan(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[
        str,
        typer.Option(envvar="INVESTMENT_MANAGER_DATABASE_URL", help="Capital 事实库"),
    ],
    release_manifest: Annotated[
        Path,
        typer.Option("--release-manifest", exists=True, dir_okay=False),
    ],
    plan_id: Annotated[str, typer.Option()],
    observation_start: Annotated[str, typer.Option()],
    observation_end: Annotated[str, typer.Option()],
) -> None:
    """在首笔 Shadow 订单前冻结精确 Release 的全年资本评价合同。"""

    loaded, manifest = load_runtime_release(config, release_manifest)
    try:
        spec = CapitalShadowEvaluationSpec.freeze(
            plan_id=plan_id,
            config=loaded,
            manifest=manifest,
            observation_start=parse_utc_option(observation_start, name="observation-start"),
            observation_end=parse_utc_option(observation_end, name="observation-end"),
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    governance = SqlGovernanceRepository(
        runtime_engine(
            database_url,
            fact_store_role=configured_fact_store_role(loaded),
            claim_fact_store=True,
        )
    )
    governance.record_release(manifest)
    existing = governance.get_plan(plan_id)
    if existing is None:
        plan = build_capital_shadow_evaluation_plan(
            spec=spec,
            registered_at=datetime.now(UTC),
        )
        governance.register_plan(plan)
    elif (
        existing.base_manifest_id != manifest.manifest_id
        or existing.candidate_spec_hash != content_hash(spec)
        or existing.candidate_spec_snapshot != spec.model_dump(mode="json")
    ):
        raise typer.BadParameter("同一 Capital plan-id 已绑定不同评价合同")
    else:
        plan = existing
    typer.echo(
        json.dumps(
            {
                "evaluation_plan": plan.model_dump(mode="json"),
                "capital_shadow_spec": spec.model_dump(mode="json"),
                "capital_shadow_spec_hash": content_hash(spec),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("evaluate-capital-shadow-plan")
def evaluate_capital_shadow_plan_command(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[
        str,
        typer.Option(envvar="INVESTMENT_MANAGER_DATABASE_URL", help="Capital 事实库"),
    ],
    release_manifest: Annotated[
        Path,
        typer.Option("--release-manifest", exists=True, dir_okay=False),
    ],
    plan_id: Annotated[str, typer.Option()],
    published_at: Annotated[str, typer.Option()],
    evaluation_catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/capital-shadow-evaluations"
    ),
) -> None:
    """从冻结 Capital 账本幂等生成预登记评价结果。"""

    publication = parse_utc_option(published_at, name="published-at")
    if publication > datetime.now(UTC):
        raise typer.BadParameter("published-at 不能晚于当前时间")
    loaded, manifest = load_runtime_release(config, release_manifest)
    engine = runtime_engine(
        database_url,
        fact_store_role=configured_fact_store_role(loaded),
        claim_fact_store=True,
    )
    governance = SqlGovernanceRepository(engine)
    plan = governance.get_plan(plan_id)
    if plan is None:
        raise typer.BadParameter("Capital EvaluationPlan 不存在", param_hint="plan-id")
    reject_invalidated_evaluation_plan(governance, plan_id)
    try:
        spec, plan = validate_capital_shadow_evaluation_plan(
            config=loaded,
            manifest=manifest,
            plans=(plan,),
            started_at=publication,
        )
        mature_at = spec.observation_end + timedelta(days=spec.settlement_grace_days)
        projection = (
            None
            if publication < mature_at
            else SqlCapitalLedgerProjector(engine, loaded).project(
                spec=spec,
                projected_at=publication,
            )
        )
        result = evaluate_capital_shadow_plan(
            spec=spec,
            plan=plan,
            projection=projection,
            published_at=publication,
        )
        result_path = CapitalShadowEvaluationCatalog(evaluation_catalog).store(
            result,
            projection=projection,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="plan-id") from exc
    payload = result.model_dump(mode="json")
    payload["projection_hash"] = projection.source_hash if projection is not None else None
    payload["result_path"] = str(result_path)
    typer.echo(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )
