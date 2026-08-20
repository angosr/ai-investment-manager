from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, func, select, update
from sqlalchemy.pool import StaticPool
from temporalio.testing import WorkflowEnvironment

from investment_manager.governance import (
    ChangeProposal,
    ChangeType,
    EvaluationPlan,
    EvaluationStage,
    EvaluationTarget,
    ReleaseApprovalStatus,
    ReleaseManifest,
    StageOutcome,
    StageResult,
    SystemConstitution,
    build_evaluation_result,
    build_governance_snapshot,
)
from investment_manager.governance_agent import SqlGovernorDecisionStore
from investment_manager.persistence import (
    SqlGovernanceRepository,
    release_approval_requests,
    release_manifests,
)
from investment_manager.release_runtime import (
    ReleaseActivities,
    ReleaseTemporalCoordinator,
    ReleaseTemporalWorker,
    ReleaseWorkflowStatus,
    build_release_workflow_request,
)
from investment_manager.schema import create_schema


def _case(now: datetime):
    champion = ReleaseManifest(
        manifest_id="release-champion-v1",
        created_at=now - timedelta(days=10),
        status="CHAMPION",
        code_version="champion-commit",
        component_versions=(("pipeline", "pipeline-v1"),),
        constitution_version="constitution-v1",
        complexity_score=2,
    )
    plan = EvaluationPlan(
        plan_id="release-plan-1",
        registered_at=now - timedelta(days=2),
        base_manifest_id=champion.manifest_id,
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
        proposal_id="release-change-1",
        created_at=now - timedelta(days=1),
        change_type=ChangeType.PANEL_POLICY,
        base_manifest_id=champion.manifest_id,
        hypothesis="减少重复信息可以降低无效换手并提高成本后的净收益",
        evidence_ids=("outcome-window-1",),
        affected_layers=("panel_policy",),
        expected_effects=("turnover_down",),
        economic_case="减少无效交易会降低手续费并改善成本后的净收益表现",
        simplest_alternative="只延长观察期无法消除已经识别出的重复信息输入",
        guardrails=plan.hard_guardrails,
        evaluation_plan_id=plan.plan_id,
        rollback_to_manifest_id=champion.manifest_id,
        complexity_delta=0,
        sunset_condition="连续两个前推窗口没有改善时删除这个候选版本",
    )
    candidate = ReleaseManifest(
        manifest_id="release-challenger-v1",
        created_at=now - timedelta(hours=12),
        status="CHALLENGER",
        code_version="candidate-commit",
        component_versions=(("pipeline", "pipeline-v2"),),
        constitution_version=champion.constitution_version,
        parent_manifest_id=champion.manifest_id,
        complexity_score=2,
    )
    target = EvaluationTarget(
        proposal=proposal,
        plan=plan,
        candidate=candidate,
        artifact_hash="0123456789abcdef0123456789abcdef",
    )
    evaluation = build_evaluation_result(
        target=target,
        completed_at=now - timedelta(hours=1),
        stage_results=tuple(
            StageResult(
                stage=stage,
                outcome=StageOutcome.PASSED,
                artifact_hash=target.artifact_hash,
                evidence_set_version=(
                    plan.fixed_regression_suite_version
                    if stage == EvaluationStage.FIXED_REGRESSION
                    else f"dataset-{stage.value}-v1"
                ),
                evidence_hashes=(f"evidence-{stage.value}",),
                sample_size=40,
                safety_violations=0,
            )
            for stage in plan.required_stages
        ),
    )
    return champion, target, evaluation


def _engine():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_schema(engine)
    return engine


def _register_target(
    repository: SqlGovernanceRepository,
    engine,
    champion: ReleaseManifest,
    target: EvaluationTarget,
    now: datetime,
) -> None:
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


def test_release_workflow_only_creates_idempotent_human_approval_request(
    app_config,
) -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 18, 12, tzinfo=UTC)
        engine = _engine()
        repository = SqlGovernanceRepository(engine)
        champion, target, evaluation = _case(now)
        _register_target(repository, engine, champion, target, now)
        repository.record_evaluation(evaluation)
        policy = app_config.temporal.model_copy(
            update={"release_task_queue": "release-approval-test"}
        )
        request = build_release_workflow_request(
            target=target,
            evaluation=evaluation,
            current_champion=champion,
            complexity_limit=10,
            requested_at=now,
            temporal_policy=policy,
        )
        async with await WorkflowEnvironment.start_time_skipping() as env:
            coordinator = ReleaseTemporalCoordinator(env.client, policy)
            async with ReleaseTemporalWorker(
                env.client,
                policy,
                ReleaseActivities(repository),
            ):
                first = await coordinator.execute(request)
                replayed = await coordinator.execute(request)
        assert first == replayed
        assert first.status == ReleaseWorkflowStatus.COMPLETED
        assert first.reason_code == "HUMAN_APPROVAL_REQUIRED"
        assert first.decision is not None
        assert first.decision.status == ReleaseApprovalStatus.AWAITING_HUMAN_APPROVAL
        with engine.connect() as connection:
            assert (
                connection.scalar(select(func.count()).select_from(release_approval_requests)) == 1
            )
            assert (
                connection.scalar(
                    select(release_manifests.c.status).where(
                        release_manifests.c.manifest_id == champion.manifest_id
                    )
                )
                == "CHAMPION"
            )

    asyncio.run(scenario())


def test_release_workflow_blocks_incomplete_evaluation_and_changed_champion(
    app_config,
) -> None:
    async def scenario() -> None:
        now = datetime(2026, 8, 18, 13, tzinfo=UTC)
        engine = _engine()
        repository = SqlGovernanceRepository(engine)
        champion, target, evaluation = _case(now)
        incomplete = evaluation.model_copy(update={"stage_results": evaluation.stage_results[:-1]})
        changed_champion = champion.model_copy(
            update={
                "manifest_id": "release-champion-v2",
                "created_at": now - timedelta(minutes=30),
            }
        )
        _register_target(repository, engine, champion, target, now)
        repository.record_evaluation(incomplete)
        with engine.begin() as connection:
            connection.execute(
                update(release_manifests)
                .where(release_manifests.c.manifest_id == champion.manifest_id)
                .values(status="PREVIOUS_STABLE")
            )
        repository.record_release(changed_champion)
        policy = app_config.temporal.model_copy(
            update={"release_task_queue": "release-blocked-test"}
        )
        request = build_release_workflow_request(
            target=target,
            evaluation=incomplete,
            current_champion=changed_champion,
            complexity_limit=10,
            requested_at=now,
            temporal_policy=policy,
        )
        async with await WorkflowEnvironment.start_time_skipping() as env:
            coordinator = ReleaseTemporalCoordinator(env.client, policy)
            async with ReleaseTemporalWorker(
                env.client,
                policy,
                ReleaseActivities(repository),
            ):
                result = await coordinator.execute(request)
        assert result.status == ReleaseWorkflowStatus.COMPLETED
        assert result.reason_code == "RELEASE_BLOCKED"
        assert result.decision is not None
        assert result.decision.status == ReleaseApprovalStatus.BLOCKED
        assert set(result.decision.reason_codes) >= {
            "CHAMPION_CHANGED_SINCE_PLAN",
            "CANDIDATE_PARENT_IS_NOT_CHAMPION",
            "EVALUATION_STAGE_SET_MISMATCH",
            "MISSING_STAGE:WALK_FORWARD",
        }

    asyncio.run(scenario())
