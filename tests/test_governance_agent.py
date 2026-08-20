from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, insert, select

from investment_manager.analyst import AnalystResult, canonical_json
from investment_manager.candidate_evaluation import SqlCandidateOutcomeStore
from investment_manager.cycle import AnalysisCycle
from investment_manager.execution.legacy_exchange import MockExchange
from investment_manager.governance.agent import (
    CodexGovernor,
    GovernorBundleBuilder,
    SqlGovernorDecisionStore,
)
from investment_manager.governance.context import GovernanceSnapshotAssembler
from investment_manager.governance.models import (
    ChangeProposal,
    ChangeType,
    EvaluationPlan,
    EvaluationStage,
    FailedExperiment,
    NoChange,
    ReleaseManifest,
)
from investment_manager.governance.repository import SqlGovernanceRepository
from investment_manager.governance.tables import change_proposals, governance_decisions
from investment_manager.information.models import IntelligenceEvent
from investment_manager.information.tables import normalized_events
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.legacy.models import (
    CandidateOutcome,
    CandidateOutcomeStatus,
)
from investment_manager.persistence import (
    SqlFactLedger,
    analysis_cycles,
    codex_runs,
    signal_candidates,
)
from investment_manager.risk.budget import SqlRiskBudgetStore
from investment_manager.scheduling.models import (
    AnalysisTriggerType,
    TriggerNow,
    build_initial_trigger_plan,
    build_trigger_batch,
    build_trigger_event,
    build_trigger_plan_patch,
)
from investment_manager.scheduling.repository import SqlTriggerRepository
from investment_manager.scheduling.tables import analysis_trigger_batches
from investment_manager.schema import create_schema


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
        primary_metric="net_pnl_after_trade_costs",
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


def _seed_historical_champion(repository: SqlGovernanceRepository) -> None:
    """Model an upgraded installation whose original Champion already exists."""

    repository.record_release(
        ReleaseManifest(
            manifest_id="release-bootstrap-v1",
            created_at=datetime(2026, 8, 17, tzinfo=UTC),
            status="CHAMPION",
            code_version="historical-bootstrap-v1",
            component_versions=(("pipeline", "off-pipeline-v1"),),
            constitution_version="constitution-v1",
        )
    )


def _snapshot(engine, app_config, now, *, with_plan=True):
    repository = SqlGovernanceRepository(engine)
    _seed_historical_champion(repository)
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


def test_governor_bundle_embeds_snapshot_without_file_tools(app_config, tmp_path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    snapshot = _snapshot(engine, app_config, datetime(2026, 8, 18, 8, tzinfo=UTC))

    bundle = GovernorBundleBuilder(
        app_config.codex_runtime,
        prompt_path=Path("config/governor_prompt.md"),
    ).build(snapshot, tmp_path / "bundle")

    snapshot_json = canonical_json(snapshot)
    assert f"<governance_snapshot_json>\n{snapshot_json}\n" in bundle.prompt
    assert "禁止调用任何工具" in bundle.prompt
    assert "读取 governance_snapshot.json" not in bundle.prompt
    assert (bundle.path / "governance_snapshot.json").read_text(encoding="utf-8") == (
        snapshot_json + "\n"
    )
    assert not (bundle.path / "governance_snapshot.md").exists()


def test_governor_bundle_rejects_prompt_above_explicit_limit(app_config, tmp_path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    snapshot = _snapshot(engine, app_config, datetime(2026, 8, 18, 8, tzinfo=UTC))
    oversized = snapshot.model_copy(
        update={
            "metric_summaries": tuple((f"oversized:{index}", "x" * 200) for index in range(100))
        }
    )
    runtime = app_config.codex_runtime.model_copy(update={"maximum_prompt_characters": 8_000})

    with pytest.raises(ValueError, match="Governor 内嵌治理快照超过"):
        GovernorBundleBuilder(
            runtime,
            prompt_path=Path("config/governor_prompt.md"),
        ).build(oversized, tmp_path / "oversized")


def test_governance_snapshot_exposes_valid_trigger_latency_and_rejects_old_timestamps(
    app_config,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    now = datetime(2026, 8, 18, 8, tzinfo=UTC)
    plan = build_initial_trigger_plan(
        symbol="BTCUSDT",
        pipeline_id=app_config.pipeline.version,
        manifest_id="release-bootstrap-v1",
        updated_at=now - timedelta(minutes=1),
        heartbeat_seconds=900,
    )
    records = (
        ("valid", 5, 4, 2, 1, 0),
        ("old-invalid", 10, 9, 7, 6, 7),
        ("future-pending", 15, 14, 12, 11, -1),
    )
    with engine.begin() as connection:
        for name, occurred_ago, observed_ago, batched_ago, submitted_ago, decided_ago in records:
            event = build_trigger_event(
                trigger_type=AnalysisTriggerType.INTELLIGENCE_INSERTED,
                symbol="BTCUSDT",
                pipeline_id=app_config.pipeline.version,
                occurred_at=now - timedelta(seconds=occurred_ago),
                observed_at=now - timedelta(seconds=observed_ago),
                priority=80,
                dedup_key=f"latency-{name}",
                expires_at=now + timedelta(minutes=1),
            )
            batch = build_trigger_batch(
                plan=plan,
                triggers=(event,),
                created_at=now - timedelta(seconds=batched_ago),
                deadline=now + timedelta(minutes=1),
            )
            submitted_at = now - timedelta(seconds=submitted_ago)
            connection.execute(
                insert(analysis_cycles).values(
                    cycle_id=stable_id("triggered_cycle", batch.batch_id),
                    as_of=batch.created_at,
                    pipeline_version=app_config.pipeline.version,
                    outcome="NO_ACTION",
                    reason_code="NO_VALID_CANDIDATE",
                    created_at=now - timedelta(seconds=decided_ago),
                )
            )
            connection.execute(
                insert(analysis_trigger_batches).values(
                    batch_id=batch.batch_id,
                    symbol=batch.symbol,
                    pipeline_id=batch.pipeline_id,
                    plan_revision=batch.plan_revision,
                    first_occurred_at=event.occurred_at,
                    first_observed_at=event.observed_at,
                    batched_at=batch.created_at,
                    analysis_submitted_at=submitted_at,
                    payload=batch.model_dump(mode="json"),
                )
            )

    metrics = dict(_snapshot(engine, app_config, now).metric_summaries)
    prefix = ":".join(
        (
            "trigger_latency",
            app_config.pipeline.version,
            "BTCUSDT",
            AnalysisTriggerType.INTELLIGENCE_INSERTED.value,
        )
    )

    assert metrics[f"{prefix}:sample_count"] == "3"
    assert metrics[f"{prefix}:valid_decision_sample_count"] == "1"
    assert metrics[f"{prefix}:invalid_decision_sample_count"] == "1"
    assert metrics[f"{prefix}:pending_decision_sample_count"] == "1"
    assert metrics[f"{prefix}:source_to_observed:p99_seconds"] == "1.000000"
    assert metrics[f"{prefix}:observed_to_batch:p95_seconds"] == "2.000000"
    assert metrics[f"{prefix}:batch_to_submit:p50_seconds"] == "1.000000"
    assert metrics[f"{prefix}:submit_to_decision:p99_seconds"] == "1.000000"
    assert metrics[f"{prefix}:source_to_decision:p50_seconds"] == "5.000000"


def test_governance_snapshot_uses_attempt_latency_and_hides_future_codex_runs(
    app_config,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    now = datetime(2026, 8, 18, 8, tzinfo=UTC)
    records = (
        ("success", "SUCCEEDED", None, -10, -9, 1000),
        ("timeout", "FAILED", "TIMEOUT", -8, -5, 3000),
        ("future", "SUCCEEDED", None, 1, 2, 1000),
    )
    with engine.begin() as connection:
        for name, status, failure, observed_offset, completed_offset, duration_ms in records:
            cycle_id = f"cycle-{name}"
            connection.execute(
                insert(analysis_cycles).values(
                    cycle_id=cycle_id,
                    as_of=now - timedelta(minutes=1),
                    pipeline_version=app_config.pipeline.version,
                    outcome="NO_ACTION",
                    reason_code="TEST",
                    created_at=now - timedelta(seconds=1),
                )
            )
            connection.execute(
                insert(codex_runs).values(
                    run_id=f"run-{name}",
                    cycle_id=cycle_id,
                    account_id="codex_b",
                    attempt=1,
                    status=status,
                    error_class=failure,
                    payload={
                        "observed_at": (now + timedelta(seconds=observed_offset)).isoformat(),
                        "completed_at": (now + timedelta(seconds=completed_offset)).isoformat(),
                        "duration_ms": duration_ms,
                        "runtime_policy_version": "runtime-v4",
                        "bundle_hash": f"bundle-{name}",
                        "usage": {},
                    },
                )
            )

    metrics = dict(_snapshot(engine, app_config, now).metric_summaries)
    prefix = f"codex_runtime:{app_config.pipeline.version}:runtime-v4"

    assert metrics["codex_run:SUCCEEDED"] == "1"
    assert metrics["codex_run:FAILED"] == "1"
    assert metrics[f"{prefix}:total"] == "2"
    assert metrics[f"{prefix}:failure:TIMEOUT"] == "1"
    assert metrics[f"{prefix}:duration_sample_count"] == "2"
    assert metrics[f"{prefix}:duration:p50_seconds"] == "2.000000"
    assert metrics[f"{prefix}:duration:p95_seconds"] == "2.900000"


def test_governance_snapshot_separates_intelligence_normalizer_versions(app_config) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    now = datetime(2026, 8, 18, 8, tzinfo=UTC)
    events = (
        IntelligenceEvent(
            evidence_id="legacy-event",
            event_time=now - timedelta(seconds=2),
            observed_at=now - timedelta(seconds=2),
            source="wire",
            title="legacy",
            body="",
            symbols=("BTCUSDT",),
            relevance=Decimal("1"),
            impact=Decimal("0.9"),
            source_reliability=Decimal("0.6"),
            novelty=Decimal("1"),
        ),
        IntelligenceEvent(
            evidence_id="current-event",
            normalizer_version="normalizer-v4",
            acquisition_route="newsnow-fast-v1",
            event_time=now - timedelta(seconds=4),
            observed_at=now - timedelta(seconds=1),
            source="wire",
            title="current",
            body="",
            symbols=("BTCUSDT",),
            relevance=Decimal("0.5"),
            impact=Decimal("0.495"),
            source_reliability=Decimal("0.6"),
            novelty=Decimal("1"),
        ),
        IntelligenceEvent(
            evidence_id="future-event",
            normalizer_version="normalizer-v4",
            event_time=now + timedelta(seconds=1),
            observed_at=now + timedelta(seconds=1),
            source="wire",
            title="future",
            body="",
            symbols=("BTCUSDT",),
            relevance=Decimal("1"),
            impact=Decimal("0.99"),
            source_reliability=Decimal("0.6"),
            novelty=Decimal("1"),
        ),
    )
    with engine.begin() as connection:
        connection.execute(
            insert(normalized_events),
            [
                {
                    "evidence_id": event.evidence_id,
                    "event_time": event.event_time,
                    "observed_at": event.observed_at,
                    "source": event.source,
                    "content_hash": content_hash({"title": event.title, "body": event.body}),
                    "payload": event.model_dump(mode="json"),
                }
                for event in events
            ],
        )

    metrics = dict(_snapshot(engine, app_config, now).metric_summaries)
    legacy = "intelligence_event:legacy-unknown:wire"
    current = "intelligence_event:normalizer-v4:wire"

    assert metrics[f"{legacy}:count"] == "1"
    assert metrics[f"{legacy}:high_impact_count"] == "1"
    assert metrics[f"{legacy}:high_impact_rate"] == "1.000000"
    assert metrics[f"{current}:count"] == "1"
    assert metrics[f"{current}:high_impact_count"] == "0"
    assert metrics[f"{current}:average_impact"] == "0.495000"
    discovery = "intelligence_discovery:normalizer-v4:newsnow-fast-v1:wire"
    assert metrics[f"{discovery}:p50_seconds"] == "3.000000"
    assert metrics[f"{discovery}:p95_seconds"] == "3.000000"
    assert metrics[f"{discovery}:p99_seconds"] == "3.000000"


def test_governance_snapshot_exposes_unsettled_candidate_sample_progress(
    app_config, replay_input
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    ledger = SqlFactLedger(engine)
    result = AnalysisCycle.with_adapters(
        app_config,
        ledger=ledger,
        exchange=MockExchange(app_config.execution),
        risk_budget=SqlRiskBudgetStore(engine),
    ).run(replay_input)
    facts = ledger.get(result.cycle_id)
    assert facts is not None and len(facts.candidates) == 1
    candidate = facts.candidates[0]
    evaluation_at = candidate.signal_observed_at + timedelta(minutes=candidate.horizon_minutes)
    assert SqlCandidateOutcomeStore(engine).record(
        CandidateOutcome(
            outcome_id="future-visible-outcome",
            candidate_id=candidate.candidate_id,
            cycle_id=candidate.cycle_id,
            producer_id=candidate.producer_id,
            producer_version=candidate.producer_version,
            calibration_ref=candidate.calibration_ref,
            evaluation_version="test-evaluation-v1",
            execution_policy_version=app_config.execution.version,
            frequency_policy_version=app_config.frequency.version,
            symbol=candidate.symbol,
            side=candidate.side,
            status=CandidateOutcomeStatus.SETTLED,
            signal_observed_at=candidate.signal_observed_at,
            evaluation_at=evaluation_at,
            settled_at=evaluation_at + timedelta(minutes=1),
            reference_price=candidate.reference_price,
            exit_price=candidate.reference_price,
            exit_event_time=evaluation_at,
            gross_return_bps=Decimal("0"),
            estimated_cost_bps=Decimal("1"),
            net_return_bps=Decimal("-1"),
            reason_code="TEST_SETTLED_AFTER_SNAPSHOT",
        )
    )

    snapshot = _snapshot(
        engine,
        app_config,
        replay_input.market.as_of + timedelta(minutes=1),
    )
    metrics = dict(snapshot.metric_summaries)
    prefix = (
        f"candidate_sample:{app_config.strategy.strategy_id}:{app_config.strategy.version}:BTCUSDT"
    )

    assert metrics[f"{prefix}:total"] == "1"
    assert metrics[f"{prefix}:settled"] == "0"
    assert metrics[f"{prefix}:unscorable"] == "0"
    assert metrics[f"{prefix}:pending"] == "1"


def test_governance_snapshot_never_merges_distinct_producers_with_same_version(
    app_config, replay_input
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    ledger = SqlFactLedger(engine)
    result = AnalysisCycle.with_adapters(
        app_config,
        ledger=ledger,
        exchange=MockExchange(app_config.execution),
        risk_budget=SqlRiskBudgetStore(engine),
    ).run(replay_input)
    facts = ledger.get(result.cycle_id)
    assert facts is not None and len(facts.candidates) == 1
    original = facts.candidates[0]
    second = original.model_copy(
        update={
            "candidate_id": "same-version-distinct-producer",
            "producer_id": "independent-producer",
        }
    )
    with engine.begin() as connection:
        connection.execute(
            insert(signal_candidates),
            {
                "candidate_id": second.candidate_id,
                "cycle_id": second.cycle_id,
                "sequence": 1,
                "producer_id": second.producer_id,
                "producer_version": second.producer_version,
                "symbol": second.symbol,
                "valid_until": second.valid_until,
                "payload": second.model_dump(mode="json"),
            },
        )

    metrics = dict(
        _snapshot(
            engine, app_config, replay_input.market.as_of + timedelta(minutes=1)
        ).metric_summaries
    )
    original_prefix = (
        f"candidate_sample:{original.producer_id}:{original.producer_version}:{original.symbol}"
    )
    second_prefix = (
        f"candidate_sample:{second.producer_id}:{second.producer_version}:{second.symbol}"
    )

    assert metrics[f"{original_prefix}:total"] == "1"
    assert metrics[f"{second_prefix}:total"] == "1"


def test_governance_snapshot_counts_only_non_overlapping_settled_candidates(
    app_config, replay_input
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    ledger = SqlFactLedger(engine)
    result = AnalysisCycle.with_adapters(
        app_config,
        ledger=ledger,
        exchange=MockExchange(app_config.execution),
        risk_budget=SqlRiskBudgetStore(engine),
    ).run(replay_input)
    facts = ledger.get(result.cycle_id)
    assert facts is not None and len(facts.candidates) == 1
    original = facts.candidates[0]
    offsets = (0, 15, 60, 75)
    candidates = tuple(
        original.model_copy(
            update={
                "candidate_id": f"candidate-overlap-{offset}",
                "signal_observed_at": original.signal_observed_at + timedelta(minutes=offset),
                "valid_until": original.valid_until + timedelta(minutes=offset),
            }
        )
        for offset in offsets
    )
    with engine.begin() as connection:
        connection.execute(
            signal_candidates.delete().where(signal_candidates.c.cycle_id == original.cycle_id)
        )
        connection.execute(
            insert(signal_candidates),
            [
                {
                    "candidate_id": candidate.candidate_id,
                    "cycle_id": candidate.cycle_id,
                    "sequence": sequence,
                    "producer_id": candidate.producer_id,
                    "producer_version": candidate.producer_version,
                    "symbol": candidate.symbol,
                    "valid_until": candidate.valid_until,
                    "payload": candidate.model_dump(mode="json"),
                }
                for sequence, candidate in enumerate(candidates)
            ],
        )
    outcome_store = SqlCandidateOutcomeStore(engine)
    for candidate, offset in zip(candidates, offsets, strict=True):
        evaluation_at = candidate.signal_observed_at + timedelta(minutes=candidate.horizon_minutes)
        assert outcome_store.record(
            CandidateOutcome(
                outcome_id=f"outcome-{candidate.candidate_id}",
                candidate_id=candidate.candidate_id,
                cycle_id=candidate.cycle_id,
                producer_id=candidate.producer_id,
                producer_version=candidate.producer_version,
                calibration_ref=candidate.calibration_ref,
                evaluation_version=("test-evaluation-v1" if offset < 60 else "test-evaluation-v2"),
                execution_policy_version=app_config.execution.version,
                frequency_policy_version=app_config.frequency.version,
                symbol=candidate.symbol,
                side=candidate.side,
                status=CandidateOutcomeStatus.SETTLED,
                signal_observed_at=candidate.signal_observed_at,
                evaluation_at=evaluation_at,
                settled_at=evaluation_at,
                reference_price=candidate.reference_price,
                exit_price=candidate.reference_price,
                exit_event_time=evaluation_at,
                gross_return_bps=Decimal("0"),
                estimated_cost_bps=Decimal("1"),
                net_return_bps=Decimal("-1"),
                reason_code="TEST_SETTLED",
            )
        )

    snapshot = _snapshot(
        engine,
        app_config,
        original.signal_observed_at + timedelta(hours=3),
    )
    metrics = dict(snapshot.metric_summaries)
    prefix = (
        f"candidate_sample:{app_config.strategy.strategy_id}:{app_config.strategy.version}:BTCUSDT"
    )

    assert metrics[f"{prefix}:total"] == "4"
    assert metrics[f"{prefix}:settled"] == "4"
    assert metrics[f"{prefix}:unscorable"] == "0"
    assert metrics[f"{prefix}:pending"] == "0"
    for evaluation_version in ("test-evaluation-v1", "test-evaluation-v2"):
        evidence_prefix = ":".join(
            (
                "candidate_evidence",
                original.producer_id,
                original.producer_version,
                original.symbol,
                original.side.value,
                str(original.horizon_minutes),
                original.calibration_ref,
                evaluation_version,
                app_config.execution.version,
                app_config.frequency.version,
            )
        )
        assert metrics[f"{evidence_prefix}:SETTLED:count"] == "2"
        assert metrics[f"{evidence_prefix}:non_overlapping_settled"] == "1"
        assert metrics[f"{evidence_prefix}:average_net_bps"] == "-1"


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
