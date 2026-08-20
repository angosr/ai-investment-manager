from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool
from temporalio.testing import WorkflowEnvironment

from investment_manager.governance.agent import (
    CodexGovernor,
    GovernorBundleBuilder,
    SqlGovernorDecisionStore,
)
from investment_manager.governance.context import GovernanceSnapshotAssembler
from investment_manager.governance.models import NoChange, ReleaseManifest
from investment_manager.governance.runtime import (
    GovernanceActivities,
    GovernanceTemporalCoordinator,
    GovernanceTemporalWorker,
    GovernanceWorkflowStatus,
    build_governance_workflow_request,
)
from investment_manager.governance.tables import governance_decisions
from investment_manager.persistence import SqlGovernanceRepository
from investment_manager.schema import create_schema


class UnusedRouter:
    def run(self, bundle):
        raise AssertionError("没有预登记计划时不应调用 Codex")


def test_governance_workflow_freezes_snapshot_and_records_no_change(app_config, tmp_path) -> None:
    async def scenario() -> None:
        engine = create_engine(
            "sqlite+pysqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        create_schema(engine)
        SqlGovernanceRepository(engine).record_release(
            ReleaseManifest(
                manifest_id="release-bootstrap-v1",
                created_at=datetime(2026, 8, 17, tzinfo=UTC),
                status="CHAMPION",
                code_version="historical-bootstrap-v1",
                component_versions=(("pipeline", "off-pipeline-v1"),),
                constitution_version="constitution-v1",
            )
        )
        governor = CodexGovernor(
            bundle_root=tmp_path,
            bundle_builder=GovernorBundleBuilder(
                app_config.codex_runtime,
                prompt_path=Path("config/governor_prompt.md"),
            ),
            router=UnusedRouter(),  # type: ignore[arg-type]
            decisions=SqlGovernorDecisionStore(engine),
        )
        activities = GovernanceActivities(
            snapshots=GovernanceSnapshotAssembler(
                engine,
                app_config,
                project_root=Path("."),
            ),
            governor=governor,
        )
        async with await WorkflowEnvironment.start_time_skipping() as env:
            policy = app_config.temporal.model_copy(
                update={"governance_task_queue": "governance-workflow-test"}
            )
            config = app_config.model_copy(update={"temporal": policy})
            request = build_governance_workflow_request(
                as_of=datetime(2026, 8, 18, tzinfo=UTC),
                config=config,
                expected_champion_manifest_id="release-bootstrap-v1",
            )
            coordinator = GovernanceTemporalCoordinator(env.client, policy)
            async with GovernanceTemporalWorker(env.client, policy, activities):
                first = await coordinator.execute(request)
                replayed = await coordinator.execute(request)
            assert first.status == GovernanceWorkflowStatus.COMPLETED
            assert isinstance(first.decision, NoChange)
            assert first.decision.reason_codes == ("NO_PREREGISTERED_EVALUATION_PLAN",)
            assert replayed == first
        with engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(governance_decisions)) == 1

    asyncio.run(scenario())
