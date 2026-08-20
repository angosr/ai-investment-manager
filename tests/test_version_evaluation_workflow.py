from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool
from temporalio.testing import WorkflowEnvironment

from investment_manager.governance.change.agent import SqlGovernorDecisionStore
from investment_manager.governance.evaluation.version_service import (
    VersionEvaluationActivities,
    VersionEvaluationTemporalCoordinator,
    VersionEvaluationTemporalWorker,
    VersionEvaluationWorkflowStatus,
    build_version_evaluation_workflow_request,
)
from investment_manager.governance.models import (
    ChangeProposal,
    ChangeType,
    EvaluationPlan,
    EvaluationStage,
    EvaluationTarget,
    ReleaseManifest,
    StageOutcome,
    StageResult,
    SystemConstitution,
    build_governance_snapshot,
)
from investment_manager.governance.repository import SqlGovernanceRepository
from investment_manager.governance.tables import evaluation_results
from investment_manager.schema import create_schema


def _target(now: datetime) -> EvaluationTarget:
    plan = EvaluationPlan(
        plan_id="version-plan-1",
        registered_at=now - timedelta(hours=1),
        base_manifest_id="release-bootstrap-v1",
        primary_metric="net_pnl_after_trade_costs",
        minimum_sample_size=30,
        hard_guardrails=("rule_violation_eq_0",),
        required_stages=(
            EvaluationStage.STATIC,
            EvaluationStage.FIXED_REGRESSION,
            EvaluationStage.WALK_FORWARD,
        ),
        fixed_regression_suite_version="phase-a-regression-v1",
    )
    proposal = ChangeProposal(
        proposal_id="change-version-1",
        created_at=now,
        change_type=ChangeType.PANEL_POLICY,
        base_manifest_id=plan.base_manifest_id,
        hypothesis="减少低价值重复证据可以降低无效换手并改善净收益",
        evidence_ids=("window-1",),
        affected_layers=("panel_policy",),
        expected_effects=("turnover_down",),
        economic_case="减少无效交易可以直接降低费用并提升成本后净收益",
        simplest_alternative="只延长观察期不能消除已发现的重复输入成本",
        guardrails=plan.hard_guardrails,
        evaluation_plan_id=plan.plan_id,
        rollback_to_manifest_id=plan.base_manifest_id,
        complexity_delta=0,
        sunset_condition="两个前推窗口无改善或回撤恶化时删除候选",
    )
    candidate = ReleaseManifest(
        manifest_id="release-challenger-v1",
        created_at=now,
        status="CHALLENGER",
        code_version="candidate-commit-1",
        component_versions=(("panel", "panel-policy-v2"),),
        constitution_version="constitution-v1",
        parent_manifest_id=plan.base_manifest_id,
        complexity_score=0,
    )
    return EvaluationTarget(
        proposal=proposal,
        plan=plan,
        candidate=candidate,
        artifact_hash="0123456789abcdef0123456789abcdef",
    )


class FixedStageRunner:
    def __init__(self, *, fail_at: EvaluationStage | None = None) -> None:
        self.fail_at = fail_at
        self.calls: list[EvaluationStage] = []

    def run(self, stage, target):
        self.calls.append(stage)
        failed = stage == self.fail_at
        return StageResult(
            stage=stage,
            outcome=StageOutcome.FAILED if failed else StageOutcome.PASSED,
            artifact_hash=target.artifact_hash,
            evidence_set_version=(
                target.plan.fixed_regression_suite_version
                if stage == EvaluationStage.FIXED_REGRESSION
                else f"dataset-{stage.value}-v1"
            ),
            evidence_hashes=(f"evidence-hash-{stage.value}",),
            sample_size=30,
            safety_violations=0,
            metric_values=(("net_pnl", "1.5"),),
            reason_codes=("PRIMARY_METRIC_FAILED",) if failed else (),
        )


def _register_target(
    repository: SqlGovernanceRepository,
    engine,
    target: EvaluationTarget,
    now: datetime,
) -> None:
    champion = ReleaseManifest(
        manifest_id=target.plan.base_manifest_id,
        created_at=now - timedelta(days=10),
        status="CHAMPION",
        code_version="champion-commit",
        component_versions=(("panel", "panel-policy-v1"),),
        constitution_version=target.candidate.constitution_version,
    )
    constitution = SystemConstitution(
        version=champion.constitution_version,
        objective="以受约束且可验证的方式改善成本后风险调整收益",
        immutable_rules=("不得绕过风险与发布门禁",),
        human_reserved_powers=("实盘发布批准",),
        agent_forbidden_changes=("系统宪法",),
    )
    snapshot = build_governance_snapshot(
        as_of=now,
        constitution=constitution,
        champion=champion,
        available_evaluation_plans=(target.plan,),
    )
    repository.record_release(champion)
    repository.record_release(target.candidate)
    repository.register_plan(target.plan)
    repository.record_snapshot(snapshot)
    SqlGovernorDecisionStore(engine).record(snapshot, target.proposal)


def test_version_evaluation_runs_only_preregistered_stages_and_replays(
    app_config,
) -> None:
    async def scenario() -> None:
        engine = create_engine(
            "sqlite+pysqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        create_schema(engine)
        runner = FixedStageRunner()
        now = datetime(2026, 8, 18, 8, tzinfo=UTC)
        repository = SqlGovernanceRepository(engine)
        activities = VersionEvaluationActivities(runner=runner, repository=repository)
        target = _target(now)
        _register_target(repository, engine, target, now)
        async with await WorkflowEnvironment.start_time_skipping() as env:
            policy = app_config.temporal.model_copy(
                update={"version_evaluation_task_queue": "version-evaluation-test"}
            )
            request = build_version_evaluation_workflow_request(
                target=target,
                temporal_policy=policy,
            )
            coordinator = VersionEvaluationTemporalCoordinator(env.client, policy)
            async with VersionEvaluationTemporalWorker(env.client, policy, activities):
                first = await coordinator.execute(request)
                replayed = await coordinator.execute(request)
            assert first.status == VersionEvaluationWorkflowStatus.COMPLETED
            assert first.reason_code == "ALL_REQUIRED_STAGES_PASSED"
            assert first.evaluation is not None
            assert [item.stage for item in first.evaluation.stage_results] == list(
                target.plan.required_stages
            )
            assert replayed == first
            assert runner.calls == list(target.plan.required_stages)
        with engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(evaluation_results)) == 1

    asyncio.run(scenario())


def test_version_evaluation_stops_after_failed_stage(app_config) -> None:
    async def scenario() -> None:
        engine = create_engine(
            "sqlite+pysqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        create_schema(engine)
        runner = FixedStageRunner(fail_at=EvaluationStage.FIXED_REGRESSION)
        now = datetime(2026, 8, 18, 9, tzinfo=UTC)
        repository = SqlGovernanceRepository(engine)
        activities = VersionEvaluationActivities(runner=runner, repository=repository)
        target = _target(now)
        _register_target(repository, engine, target, now)
        async with await WorkflowEnvironment.start_time_skipping() as env:
            policy = app_config.temporal.model_copy(
                update={"version_evaluation_task_queue": "version-evaluation-fail-test"}
            )
            coordinator = VersionEvaluationTemporalCoordinator(env.client, policy)
            request = build_version_evaluation_workflow_request(
                target=target,
                temporal_policy=policy,
            )
            async with VersionEvaluationTemporalWorker(env.client, policy, activities):
                result = await coordinator.execute(request)
            assert result.status == VersionEvaluationWorkflowStatus.COMPLETED
            assert result.reason_code == "EVALUATION_STAGE_FAILED"
            assert result.evaluation is not None
            assert [item.stage for item in result.evaluation.stage_results] == [
                EvaluationStage.STATIC,
                EvaluationStage.FIXED_REGRESSION,
            ]
            assert runner.calls == [
                EvaluationStage.STATIC,
                EvaluationStage.FIXED_REGRESSION,
            ]

    asyncio.run(scenario())


def test_version_evaluation_rejects_unregistered_governance_target(app_config) -> None:
    async def scenario() -> None:
        engine = create_engine(
            "sqlite+pysqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        create_schema(engine)
        runner = FixedStageRunner()
        activities = VersionEvaluationActivities(
            runner=runner,
            repository=SqlGovernanceRepository(engine),
        )
        target = _target(datetime(2026, 8, 18, 10, tzinfo=UTC))
        async with await WorkflowEnvironment.start_time_skipping() as env:
            policy = app_config.temporal.model_copy(
                update={"version_evaluation_task_queue": "unregistered-target-test"}
            )
            coordinator = VersionEvaluationTemporalCoordinator(env.client, policy)
            request = build_version_evaluation_workflow_request(
                target=target,
                temporal_policy=policy,
            )
            async with VersionEvaluationTemporalWorker(env.client, policy, activities):
                result = await coordinator.execute(request)
        assert result.status == VersionEvaluationWorkflowStatus.FAILED
        assert result.reason_code == "EVALUATION_ACTIVITY_FAILED"
        assert runner.calls == []

    asyncio.run(scenario())
