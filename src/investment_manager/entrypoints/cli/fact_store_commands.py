from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import typer

from investment_manager.entrypoints.cli.root import app
from investment_manager.entrypoints.cli.support import runtime_engine
from investment_manager.platform.fact_store import (
    FactStoreRole,
    SqlFactCohortQuarantineStore,
    build_fact_cohort_quarantine,
)


@app.command("quarantine-wrong-store-cohort")
def quarantine_wrong_store_cohort(
    database_url: Annotated[str, typer.Option(envvar="INVESTMENT_MANAGER_DATABASE_URL")],
    manifest_id: Annotated[str, typer.Option()],
    pipeline_id: Annotated[str, typer.Option()],
    expected_role: Annotated[FactStoreRole, typer.Option()],
    evidence_ref: Annotated[str, typer.Option()],
    analysis_behavior_hash: Annotated[str | None, typer.Option()] = None,
) -> None:
    """追加错库 cohort 隔离事实；不删除或改写任何原始记录。"""

    store = SqlFactCohortQuarantineStore(runtime_engine(database_url))
    store_id, observed_role = store.current_identity()
    try:
        quarantine = build_fact_cohort_quarantine(
            store_id=store_id,
            observed_role=observed_role,
            expected_role=expected_role,
            manifest_id=manifest_id,
            pipeline_id=pipeline_id,
            analysis_behavior_hash=analysis_behavior_hash,
            quarantined_at=datetime.now(UTC),
            evidence_ref=evidence_ref,
        )
        store.record(quarantine)
    except (RuntimeError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(quarantine.model_dump_json(indent=2))
