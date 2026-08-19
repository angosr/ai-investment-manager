from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, func, select

from quant_core.governance import (
    ChangeProposal,
    ChangeType,
    EvaluationPlan,
    EvaluationResult,
    EvaluationStage,
    FailedExperiment,
    GovernanceGate,
    PromotionGate,
    ReleaseManifest,
    StageOutcome,
    StageResult,
    build_governance_snapshot,
    load_constitution,
    load_regression_suite,
)
from quant_core.persistence import (
    SqlGovernanceRepository,
    change_proposals,
    create_schema,
    evaluation_plans,
    failed_experiment_records,
    governance_snapshots,
)


def _manifest(now: datetime) -> ReleaseManifest:
    return ReleaseManifest(
        manifest_id="release-champion-v1",
        created_at=now - timedelta(days=10),
        status="CHAMPION",
        code_version="commit-1",
        component_versions=(("risk", "risk-v1"), ("pipeline", "off-v1")),
        constitution_version="constitution-v1",
    )


def _plan(now: datetime) -> EvaluationPlan:
    return EvaluationPlan(
        plan_id="eval-plan-1",
        registered_at=now - timedelta(hours=1),
        base_manifest_id="release-champion-v1",
        primary_metric="net_pnl_after_trade_costs",
        minimum_sample_size=30,
        hard_guardrails=("rule_violation_eq_0", "max_drawdown_not_worse"),
        required_stages=(
            EvaluationStage.STATIC,
            EvaluationStage.FIXED_REGRESSION,
            EvaluationStage.WALK_FORWARD,
        ),
        fixed_regression_suite_version="phase-a-regression-v1",
    )


def _proposal(now: datetime, **updates) -> ChangeProposal:
    values = {
        "proposal_id": "change-1",
        "created_at": now,
        "change_type": ChangeType.PANEL_POLICY,
        "base_manifest_id": "release-champion-v1",
        "hypothesis": "减少同源转载可以提升证据多样性并减少无效换手",
        "evidence_ids": ("experiment-81", "incident-24"),
        "affected_layers": ("panel_policy",),
        "expected_effects": ("source_diversity_up", "turnover_not_up"),
        "economic_case": "预期减少重复信号与手续费，且不会增加模型调用成本",
        "simplest_alternative": "只降低单来源配额，但历史回放改善不足且不稳定",
        "guardrails": ("rule_violation_eq_0", "max_drawdown_not_worse"),
        "evaluation_plan_id": "eval-plan-1",
        "rollback_to_manifest_id": "release-champion-v1",
        "complexity_delta": 0,
        "sunset_condition": "两个时间前推窗口均无净收益改善则删除该变更",
    }
    values.update(updates)
    return ChangeProposal(**values)


def test_constitution_and_fixed_regression_suite_are_typed_and_frozen() -> None:
    constitution = load_constitution("config/system-constitution.yaml")
    suite = load_regression_suite("config/regression-suite.yaml")

    assert constitution.version == "constitution-v1"
    assert suite.immutable
    assert {item.id for item in suite.cases} >= {
        "prompt_injection_is_data",
        "codex_schema_failure",
        "concurrent_risk_reservation",
    }


def test_governance_gate_accepts_single_layer_preregistered_challenger() -> None:
    now = datetime(2026, 8, 18, tzinfo=UTC)
    constitution = load_constitution("config/system-constitution.yaml")
    snapshot = build_governance_snapshot(
        as_of=now,
        constitution=constitution,
        champion=_manifest(now),
        complexity_used=3,
        complexity_limit=5,
    )

    result = GovernanceGate().validate(_proposal(now), _plan(now), snapshot)

    assert result.accepted
    assert not result.reason_codes


def test_governance_gate_blocks_local_trap_risk_change_and_repeated_failure() -> None:
    now = datetime(2026, 8, 18, tzinfo=UTC)
    constitution = load_constitution("config/system-constitution.yaml")
    proposal = _proposal(
        now,
        change_type=ChangeType.RISK_POLICY,
        complexity_delta=5,
    )
    failed = FailedExperiment(
        experiment_id="failed-1",
        hypothesis_fingerprint=proposal.hypothesis_fingerprint,
        evidence_ids=proposal.evidence_ids,
        rejected_at=now - timedelta(days=1),
        reason_codes=("NO_NET_INCREMENT",),
    )
    snapshot = build_governance_snapshot(
        as_of=now,
        constitution=constitution,
        champion=_manifest(now),
        failed_experiments=(failed,),
        complexity_used=3,
        complexity_limit=5,
    )

    result = GovernanceGate().validate(proposal, _plan(now), snapshot)

    assert not result.accepted
    assert set(result.reason_codes) >= {
        "RISK_POLICY_MUST_BE_MANUAL_ONLY",
        "COMPLEXITY_BUDGET_EXCEEDED",
        "FAILED_HYPOTHESIS_WITHOUT_NEW_EVIDENCE",
    }


def test_promotion_gate_never_publishes_and_requires_all_preregistered_stages() -> None:
    now = datetime(2026, 8, 18, tzinfo=UTC)
    plan = _plan(now)
    result = EvaluationResult(
        evaluation_id="evaluation-1",
        proposal_id="change-1",
        plan_id=plan.plan_id,
        candidate_manifest_id="release-challenger-v1",
        completed_at=now,
        stage_results=tuple(
            StageResult(
                stage=stage,
                outcome=StageOutcome.PASSED,
                artifact_hash="artifact-hash-0001",
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

    decision = PromotionGate().evaluate(result, plan)

    assert decision.eligible_for_human_approval
    assert decision.reason_codes == ("HUMAN_APPROVAL_REQUIRED",)

    unsafe = result.model_copy(
        update={
            "stage_results": (
                *result.stage_results[:-1],
                result.stage_results[-1].model_copy(update={"safety_violations": 1}),
            )
        }
    )
    blocked = PromotionGate().evaluate(unsafe, plan)
    assert not blocked.eligible_for_human_approval
    assert any(item.startswith("SAFETY_VIOLATION") for item in blocked.reason_codes)


def test_governance_repository_restores_long_term_state_without_chat_history() -> None:
    now = datetime(2026, 8, 18, tzinfo=UTC)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    repository = SqlGovernanceRepository(engine)
    constitution = load_constitution("config/system-constitution.yaml")
    champion = _manifest(now)
    plan = _plan(now)
    proposal = _proposal(now)
    failed = FailedExperiment(
        experiment_id="failed-1",
        hypothesis_fingerprint="fingerprint-1",
        evidence_ids=("evidence-1",),
        rejected_at=now,
        reason_codes=("NO_INCREMENTAL_VALUE",),
    )
    snapshot = build_governance_snapshot(
        as_of=now,
        constitution=constitution,
        champion=champion,
        failed_experiments=(failed,),
        open_proposal_ids=(proposal.proposal_id,),
    )

    repository.record_constitution(constitution)
    repository.record_release(champion)
    repository.register_plan(plan)
    repository.register_proposal(proposal)
    repository.record_failed_experiment(failed)
    repository.record_snapshot(snapshot)
    repository.record_snapshot(snapshot)

    restored = SqlGovernanceRepository(engine).get_snapshot(snapshot.snapshot_id)
    assert restored == snapshot
    assert SqlGovernanceRepository(engine).get_plan(plan.plan_id) == plan
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(governance_snapshots)) == 1
        assert connection.scalar(select(func.count()).select_from(evaluation_plans)) == 1
        assert connection.scalar(select(func.count()).select_from(change_proposals)) == 1
        assert connection.scalar(select(func.count()).select_from(failed_experiment_records)) == 1
