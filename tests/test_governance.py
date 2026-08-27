from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select

from investment_manager.governance.models import (
    BlindEvaluationClaim,
    EvaluationPlan,
    EvaluationStage,
    FailedExperiment,
    ReleaseArtifact,
    ReleaseManifest,
    build_evaluation_plan_invalidation,
    committed_file_revision,
    evaluation_plan_invalidation_id,
    load_constitution,
    load_regression_suite,
    load_release_manifest,
    validate_manifest_against_config,
    validate_manifest_artifacts,
    validate_manifest_code_version,
    validate_manifest_component_versions,
)
from investment_manager.governance.repository import SqlGovernanceRepository
from investment_manager.governance.tables import (
    blind_evaluation_claims,
    evaluation_plans,
    failed_experiment_records,
    release_manifests,
)
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.schema import create_schema
from investment_manager.settings import load_config


def _manifest(now: datetime) -> ReleaseManifest:
    return ReleaseManifest(
        manifest_id="release-champion-v1",
        created_at=now - timedelta(days=10),
        status="CHAMPION",
        code_version="commit-1",
        component_versions=(("risk", "risk-v1"), ("pipeline", "off-v1")),
        constitution_version="constitution-v1",
    )


def test_release_artifact_binds_the_actual_frontend_directory(tmp_path) -> None:
    dist = tmp_path / "web" / "dist"
    dist.mkdir(parents=True)
    manifest = _manifest(datetime(2026, 8, 24, tzinfo=UTC)).model_copy(
        update={
            "artifacts": (
                ReleaseArtifact(
                    artifact_id="web-dist",
                    relative_path="web/dist",
                    sha256=hashlib.sha256(b"").hexdigest(),
                ),
            )
        }
    )

    validate_manifest_artifacts(
        manifest,
        repository_root=tmp_path,
        required_ids=("web-dist",),
    )
    (dist / "index.html").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="内容不一致"):
        validate_manifest_artifacts(
            manifest,
            repository_root=tmp_path,
            required_ids=("web-dist",),
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


def test_constitution_and_fixed_regression_suite_are_typed_and_frozen() -> None:
    constitution = load_constitution("config/system-constitution.yaml")
    suite = load_regression_suite("config/regression-suite.yaml")

    assert constitution.version == "constitution-v2"
    assert suite.immutable
    assert {item.id for item in suite.cases} >= {
        "prompt_injection_is_data",
        "codex_schema_failure",
        "concurrent_portfolio_decision",
    }


def test_runtime_release_requires_exact_clean_code_version(monkeypatch, tmp_path) -> None:
    now = datetime(2026, 8, 18, tzinfo=UTC)
    manifest = _manifest(now)
    observations = iter((manifest.code_version, ""))
    monkeypatch.setattr(
        "investment_manager.governance.models._git_output",
        lambda _root, *_arguments: next(observations),
    )

    assert (
        validate_manifest_code_version(
            manifest,
            repository_root=tmp_path,
        )
        == tmp_path.resolve()
    )

    monkeypatch.setattr(
        "investment_manager.governance.models._git_output",
        lambda _root, *_arguments: "different-commit",
    )
    with pytest.raises(ValueError, match="实际运行源码不一致"):
        validate_manifest_code_version(manifest, repository_root=tmp_path)

    observations = iter((manifest.code_version, " M src/investment_manager/risk.py"))
    monkeypatch.setattr(
        "investment_manager.governance.models._git_output",
        lambda _root, *_arguments: next(observations),
    )
    with pytest.raises(ValueError, match="未提交变更"):
        validate_manifest_code_version(manifest, repository_root=tmp_path)


def test_committed_file_revision_binds_current_blob_and_commit_time(
    monkeypatch,
    tmp_path,
) -> None:
    target = tmp_path / "config" / "plan.yaml"
    target.parent.mkdir()
    target.write_text("version: v1\n", encoding="utf-8")
    observations = iter(
        (
            "",
            "a" * 40,
            "b" * 40,
            "b" * 40,
            "2026-08-24T12:30:00+00:00",
        )
    )
    monkeypatch.setattr(
        "investment_manager.governance.models._git_output",
        lambda _root, *_arguments: next(observations),
    )

    commit, committed_at = committed_file_revision(
        target,
        repository_root=tmp_path,
    )

    assert commit == "a" * 40
    assert committed_at == datetime(2026, 8, 24, 12, 30, tzinfo=UTC)

    monkeypatch.setattr(
        "investment_manager.governance.models._git_output",
        lambda _root, *_arguments: " M config/plan.yaml",
    )
    with pytest.raises(ValueError, match="尚未提交"):
        committed_file_revision(target, repository_root=tmp_path)


def test_historical_runtime_release_rejects_changed_configuration() -> None:
    config = load_config("config/investment-manager.testnet.yaml")
    manifest = load_release_manifest("config/release-manifest.yaml")

    with pytest.raises(ValueError, match=r"配置版本不一致|完整配置内容不一致"):
        validate_manifest_against_config(
            manifest,
            config,
            require_configuration_hash=True,
        )


def test_runtime_release_binds_complete_configuration_content() -> None:
    config = load_config("config/investment-manager.testnet.yaml")
    historical = load_release_manifest("config/release-manifest.yaml")
    current_names = tuple(
        name
        for name, _version in load_release_manifest(
            "config/release-manifest.yaml"
        ).component_versions
    )
    component_versions = tuple(
        (name, getattr(config, name).version) for name in current_names
    )
    manifest = historical.model_copy(
        update={
            "manifest_id": "release-testnet-current-contract-test",
            "component_versions": component_versions,
            "configuration_hash": content_hash(config),
        }
    )

    validate_manifest_against_config(
        manifest,
        config,
        require_configuration_hash=True,
    )
    changed = config.model_copy(
        update={
            "decision_state": config.decision_state.model_copy(
                update={
                    "packet_policy": config.decision_state.packet_policy.model_copy(
                        update={
                            "maximum_market_age_seconds": (
                                config.decision_state.packet_policy.maximum_market_age_seconds
                                + 1
                            )
                        }
                    )
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


def test_read_only_component_validation_does_not_rehash_historical_config() -> None:
    loaded = load_config("config/investment-manager.shadow.yaml")
    config = loaded.model_copy(
        update={"capital": loaded.capital.model_copy(update={"enabled": False})}
    )
    historical = load_release_manifest("config/release-manifest.yaml")
    manifest = historical.model_copy(
        update={
            "component_versions": tuple(
                (name, getattr(config, name).version)
                for name, _version in historical.component_versions
            ),
            "configuration_hash": "0" * 64,
        }
    )

    validate_manifest_component_versions(manifest, config)
    with pytest.raises(ValueError, match="完整配置内容不一致"):
        validate_manifest_against_config(manifest, config)


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


def test_governance_repository_keeps_only_live_release_and_evaluation_facts() -> None:
    now = datetime(2026, 8, 18, tzinfo=UTC)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    repository = SqlGovernanceRepository(engine)
    champion = _manifest(now)
    plan = _plan(now)
    failed = FailedExperiment(
        experiment_id="failed-1",
        hypothesis_fingerprint="fingerprint-1",
        evidence_ids=("evidence-1",),
        rejected_at=now,
        reason_codes=("NO_INCREMENTAL_VALUE",),
    )
    repository.record_release(champion)
    repository.register_plan(plan)
    repository.record_failed_experiment(failed)
    repository.record_failed_experiment(
        failed.model_copy(update={"rejected_at": now + timedelta(minutes=1)})
    )
    assert SqlGovernanceRepository(engine).get_plan(plan.plan_id) == plan
    assert SqlGovernanceRepository(engine).plans_for_manifest(plan.base_manifest_id) == (plan,)
    assert SqlGovernanceRepository(engine).plans_for_manifest("other-release") == ()
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(release_manifests)) == 1
        assert connection.scalar(select(func.count()).select_from(evaluation_plans)) == 1
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
        EvaluationPlan.model_validate({**plan.model_dump(mode="json"), "blind_query_budget": 2})
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
