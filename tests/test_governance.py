from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select

from investment_manager.config import load_config
from investment_manager.governance import (
    BlindEvaluationClaim,
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
    build_evaluation_plan_invalidation,
    build_governance_snapshot,
    evaluation_plan_invalidation_id,
    load_constitution,
    load_regression_suite,
    load_release_manifest,
    validate_manifest_against_config,
    validate_manifest_code_version,
)
from investment_manager.ids import stable_id
from investment_manager.persistence import (
    SqlGovernanceRepository,
    blind_evaluation_claims,
    change_proposals,
    evaluation_plans,
    failed_experiment_records,
    governance_snapshots,
    release_manifests,
)
from investment_manager.schema import create_schema


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


def test_runtime_release_requires_exact_clean_code_version(monkeypatch, tmp_path) -> None:
    now = datetime(2026, 8, 18, tzinfo=UTC)
    manifest = _manifest(now)
    observations = iter((manifest.code_version, ""))
    monkeypatch.setattr(
        "investment_manager.governance._git_output",
        lambda _root, *_arguments: next(observations),
    )

    assert validate_manifest_code_version(
        manifest,
        repository_root=tmp_path,
    ) == tmp_path.resolve()

    monkeypatch.setattr(
        "investment_manager.governance._git_output",
        lambda _root, *_arguments: "different-commit",
    )
    with pytest.raises(ValueError, match="实际运行源码不一致"):
        validate_manifest_code_version(manifest, repository_root=tmp_path)

    observations = iter((manifest.code_version, " M src/investment_manager/risk.py"))
    monkeypatch.setattr(
        "investment_manager.governance._git_output",
        lambda _root, *_arguments: next(observations),
    )
    with pytest.raises(ValueError, match="未提交变更"):
        validate_manifest_code_version(manifest, repository_root=tmp_path)


def test_runtime_release_binds_complete_configuration_content() -> None:
    config = load_config("config/investment-manager.testnet.yaml")
    manifest = load_release_manifest("config/release-manifest.testnet.yaml")

    validate_manifest_against_config(
        manifest,
        config,
        require_configuration_hash=True,
    )
    changed = config.model_copy(
        update={
            "frequency": config.frequency.model_copy(
                update={
                    "minimum_net_edge_bps": config.frequency.minimum_net_edge_bps
                    + 1
                }
            )
        }
    )
    with pytest.raises(ValueError, match="完整配置内容不一致"):
        validate_manifest_against_config(
            manifest,
            changed,
            require_configuration_hash=True,
        )

    unbound = manifest.model_copy(update={"configuration_hash": None})
    with pytest.raises(ValueError, match="缺少完整配置哈希"):
        validate_manifest_against_config(
            unbound,
            config,
            require_configuration_hash=True,
        )


def test_legacy_release_manifest_keeps_immutable_payload_shape() -> None:
    now = datetime(2026, 8, 18, tzinfo=UTC)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    repository = SqlGovernanceRepository(engine)
    manifest = _manifest(now)

    assert "configuration_hash" not in manifest.model_dump(mode="json")
    repository.record_release(manifest)
    repository.record_release(manifest)

    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(release_manifests)) == 1


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
    repository.record_failed_experiment(
        failed.model_copy(update={"rejected_at": now + timedelta(minutes=1)})
    )
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


def test_evaluation_plan_invalidation_is_durable_and_idempotent() -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    repository = SqlGovernanceRepository(engine)
    plan = _plan(now)
    repository.register_plan(plan)
    invalidation = build_evaluation_plan_invalidation(
        plan_id=plan.plan_id,
        invalidated_at=now,
        reason_codes=("CODEX_RUNTIME_ARTIFACT_DRIFT",),
        evidence_ids=("cycle:contaminated", "binary:sha256:changed"),
    )

    repository.record_failed_experiment(invalidation)
    repository.record_failed_experiment(
        invalidation.model_copy(update={"rejected_at": now + timedelta(minutes=1)})
    )

    assert invalidation.experiment_id == evaluation_plan_invalidation_id(plan.plan_id)
    assert repository.get_failed_experiment(invalidation.experiment_id) == invalidation
    with pytest.raises(ValueError, match="原因码和证据"):
        build_evaluation_plan_invalidation(
            plan_id=plan.plan_id,
            invalidated_at=now,
            reason_codes=(),
            evidence_ids=("cycle:contaminated",),
        )


def test_blind_evaluation_budget_is_claimed_once_and_exact_retry_is_idempotent() -> None:
    now = datetime(2026, 8, 18, tzinfo=UTC)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    repository = SqlGovernanceRepository(engine)
    plan = _plan(now).model_copy(
        update={
            "required_stages": (*_plan(now).required_stages, EvaluationStage.BLIND),
            "blind_query_budget": 1,
        }
    )
    with pytest.raises(ValueError, match="恰好一次"):
        EvaluationPlan.model_validate(
            {**plan.model_dump(mode="json"), "blind_query_budget": 2}
        )
    repository.register_plan(plan)
    blind_start = now - timedelta(days=365)
    blind_end = now - timedelta(days=1)
    initial = BlindEvaluationClaim(
        query_id="blind-query-1",
        blind_scope_id=stable_id(
            "blind_evaluation_scope",
            "BTCUSDT",
            blind_start,
            blind_end,
        ),
        blind_symbol="BTCUSDT",
        blind_start=blind_start,
        blind_end=blind_end,
        plan_id=plan.plan_id,
        source_evaluation_id="walk-forward-1",
        claimed_at=now,
    )

    assert repository.claim_blind_evaluation(initial) == initial
    retry = initial.model_copy(update={"claimed_at": now + timedelta(minutes=1)})
    assert repository.claim_blind_evaluation(retry) == initial
    with pytest.raises(ValueError, match="已经被消费"):
        repository.claim_blind_evaluation(initial.model_copy(update={"query_id": "blind-query-2"}))

    second_plan = plan.model_copy(update={"plan_id": "eval-plan-2"})
    repository.register_plan(second_plan)
    with pytest.raises(ValueError, match="重叠"):
        repository.claim_blind_evaluation(
            initial.model_copy(
                update={
                    "query_id": "blind-query-other-plan",
                    "plan_id": second_plan.plan_id,
                    "source_evaluation_id": "walk-forward-2",
                }
            )
        )

    third_plan = plan.model_copy(update={"plan_id": "eval-plan-3"})
    repository.register_plan(third_plan)
    future_start = blind_end + timedelta(microseconds=1)
    future_end = blind_end + timedelta(days=30)
    future_claim = initial.model_copy(
        update={
            "query_id": "blind-query-future-window",
            "blind_scope_id": stable_id(
                "blind_evaluation_scope",
                "BTCUSDT",
                future_start,
                future_end,
            ),
            "blind_start": future_start,
            "blind_end": future_end,
            "plan_id": third_plan.plan_id,
            "source_evaluation_id": "walk-forward-3",
        }
    )
    assert repository.claim_blind_evaluation(future_claim) == future_claim

    completed = initial.model_copy(
        update={
            "completed_at": now + timedelta(minutes=2),
            "result_id": "blind-result-1",
            "result_hash": "a" * 64,
        }
    )
    assert repository.complete_blind_evaluation(completed) == completed
    assert repository.complete_blind_evaluation(completed) == completed
    with pytest.raises(ValueError, match="不同结果"):
        repository.complete_blind_evaluation(completed.model_copy(update={"result_hash": "b" * 64}))
    assert repository.get_blind_evaluation_claim(plan.plan_id) == completed
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(blind_evaluation_claims)) == 2
