from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from investment_manager.entrypoints.cli.root import app
from investment_manager.entrypoints.cli.support import (
    load_runtime_release,
    parse_utc_option,
    runtime_engine,
)
from investment_manager.governance.evaluation.capital import (
    CapitalShadowEvaluationSpec,
    build_capital_shadow_evaluation_plan,
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
            observation_start=parse_utc_option(
                observation_start, name="observation-start"
            ),
            observation_end=parse_utc_option(observation_end, name="observation-end"),
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    governance = SqlGovernanceRepository(runtime_engine(database_url))
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
