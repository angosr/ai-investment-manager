from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, func, select

from quant_core.analyst import AnalystResult
from quant_core.governance import (
    ChangeProposal,
    ChangeType,
    EvaluationPlan,
    EvaluationStage,
    FailedExperiment,
    NoChange,
)
from quant_core.governance_agent import (
    CodexGovernor,
    GovernorBundleBuilder,
    SqlGovernorDecisionStore,
)
from quant_core.governance_context import GovernanceSnapshotAssembler
from quant_core.persistence import (
    SqlGovernanceRepository,
    change_proposals,
    create_schema,
    governance_decisions,
)
from quant_core.trigger import (
    TriggerNow,
    build_initial_trigger_plan,
    build_trigger_plan_patch,
)
from quant_core.trigger_sql import SqlTriggerRepository


class StaticRouter:
    def __init__(self, decision, trigger_plan_patch=None) -> None:
        self.decision = decision
        self.trigger_plan_patch = trigger_plan_patch
        self.calls = 0

    def run(self, bundle):
        self.calls += 1
        return AnalystResult(
            True,
            {
                "decision": self.decision.model_dump(mode="json"),
                "trigger_plan_patch": (
                    self.trigger_plan_patch.model_dump(mode="json")
                    if self.trigger_plan_patch is not None
                    else None
                ),
            },
            "CODEX_ANALYSIS_SUCCEEDED",
            "codex_b",
            1,
        )


def _plan(now: datetime) -> EvaluationPlan:
    return EvaluationPlan(
        plan_id="governance-plan-1",
        registered_at=now - timedelta(hours=1),
        base_manifest_id="release-bootstrap-v1",
        primary_metric="net_pnl_after_all_costs",
        minimum_sample_size=100,
        hard_guardrails=("rule_violation_eq_0", "max_drawdown_not_worse"),
        required_stages=(
            EvaluationStage.STATIC,
            EvaluationStage.FIXED_REGRESSION,
            EvaluationStage.WALK_FORWARD,
            EvaluationStage.SHADOW,
        ),
        fixed_regression_suite_version="phase-a-regression-v1",
    )


def _snapshot(engine, app_config, now, *, with_plan=True):
    repository = SqlGovernanceRepository(engine)
    failed = FailedExperiment(
        experiment_id="failed-evidence-1",
        hypothesis_fingerprint="old-hypothesis",
        evidence_ids=("old-window",),
        rejected_at=now - timedelta(days=1),
        reason_codes=("NO_INCREMENTAL_VALUE",),
    )
    repository.record_failed_experiment(failed)
    if with_plan:
        repository.register_plan(_plan(now))
    return GovernanceSnapshotAssembler(
        engine,
        app_config,
        project_root=Path("."),
    ).build(as_of=now)


def _proposal(now: datetime, *, evidence_id="failed-evidence-1") -> ChangeProposal:
    return ChangeProposal(
        proposal_id="model-chosen-id",
        created_at=now,
        change_type=ChangeType.PANEL_POLICY,
        base_manifest_id="release-bootstrap-v1",
        hypothesis="减少低价值重复证据可能降低无效交易并改善成本后净收益",
        evidence_ids=(evidence_id,),
        affected_layers=("panel_policy",),
        expected_effects=("duplicate_evidence_down", "turnover_not_up"),
        economic_case="减少重复信息导致的无效动作，预期直接降低手续费和模型成本",
        simplest_alternative="保持现状并仅延长观察窗口，但不能消除已知重复输入成本",
        guardrails=("rule_violation_eq_0", "max_drawdown_not_worse"),
        evaluation_plan_id="governance-plan-1",
        rollback_to_manifest_id="release-bootstrap-v1",
        complexity_delta=0,
        sunset_condition="两个前推窗口无净收益改善或换手恶化时删除该变更",
    )


def test_snapshot_assembler_exposes_only_preregistered_current_champion_plans(
    app_config,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    now = datetime(2026, 8, 18, 8, tzinfo=UTC)

    snapshot = _snapshot(engine, app_config, now)
    replayed = GovernanceSnapshotAssembler(
        engine,
        app_config,
        project_root=Path("."),
    ).build(as_of=now)

    assert snapshot == replayed
    assert snapshot.champion.manifest_id == "release-bootstrap-v1"
    assert [item.plan_id for item in snapshot.available_evaluation_plans] == ["governance-plan-1"]
    assert snapshot.failed_experiments[0].experiment_id == "failed-evidence-1"


def test_codex_governor_normalizes_validates_and_atomically_records_one_decision(
    app_config, tmp_path
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    now = datetime(2026, 8, 18, 8, tzinfo=UTC)
    snapshot = _snapshot(engine, app_config, now)
    router = StaticRouter(_proposal(now))
    governor = CodexGovernor(
        bundle_root=tmp_path,
        bundle_builder=GovernorBundleBuilder(
            app_config.codex_runtime,
            prompt_path=Path("config/governor_prompt.md"),
        ),
        router=router,  # type: ignore[arg-type]
        decisions=SqlGovernorDecisionStore(engine),
    )

    first = governor.govern(snapshot)
    replayed = governor.govern(snapshot)

    assert first.success
    assert replayed == first
    assert isinstance(first.decision, ChangeProposal)
    assert first.decision.proposal_id.startswith("change_")
    assert first.decision.proposal_id != "model-chosen-id"
    assert first.decision.created_at == snapshot.as_of
    assert router.calls == 2
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(governance_decisions)) == 1
        assert connection.scalar(select(func.count()).select_from(change_proposals)) == 1


def test_governor_without_preregistered_plan_records_no_change_without_codex(
    app_config, tmp_path
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    now = datetime(2026, 8, 18, 8, tzinfo=UTC)
    snapshot = _snapshot(engine, app_config, now, with_plan=False)
    router = StaticRouter(_proposal(now))
    governor = CodexGovernor(
        bundle_root=tmp_path,
        bundle_builder=GovernorBundleBuilder(
            app_config.codex_runtime,
            prompt_path=Path("config/governor_prompt.md"),
        ),
        router=router,  # type: ignore[arg-type]
        decisions=SqlGovernorDecisionStore(engine),
    )

    result = governor.govern(snapshot)

    assert result.success
    assert isinstance(result.decision, NoChange)
    assert result.decision.reason_codes == ("NO_PREREGISTERED_EVALUATION_PLAN",)
    assert router.calls == 0


def test_governor_can_apply_trigger_now_without_proposing_production_change(
    app_config, tmp_path
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    now = datetime(2026, 8, 18, 8, tzinfo=UTC)
    trigger_plans = SqlTriggerRepository(engine, app_config.trigger)
    plan = trigger_plans.create_plan(
        build_initial_trigger_plan(
            symbol="BTCUSDT",
            pipeline_id=app_config.pipeline.version,
            manifest_id="release-bootstrap-v1",
            updated_at=now - timedelta(minutes=1),
            heartbeat_seconds=900,
        )
    )
    snapshot = _snapshot(engine, app_config, now, with_plan=False)
    patch = build_trigger_plan_patch(
        plan=plan,
        submitted_at=now,
        operations=(TriggerNow(request_id="governor-now-1", reason="立即验证新信息"),),
    )
    decision = NoChange(
        decision_id="model-id",
        observed_at=now,
        reason_codes=("TRIGGER_ONLY",),
        revisit_conditions=("AFTER_IMMEDIATE_ANALYSIS",),
    )
    router = StaticRouter(decision, patch)
    governor = CodexGovernor(
        bundle_root=tmp_path,
        bundle_builder=GovernorBundleBuilder(
            app_config.codex_runtime,
            prompt_path=Path("config/governor_prompt.md"),
        ),
        router=router,  # type: ignore[arg-type]
        decisions=SqlGovernorDecisionStore(engine),
        trigger_plans=trigger_plans,
    )

    result = governor.govern(snapshot)

    assert result.success
    assert isinstance(result.decision, NoChange)
    assert result.applied_trigger_plan is not None
    assert result.applied_trigger_plan.revision == 2
    assert result.trigger_plan_patch == patch
    assert router.calls == 1
