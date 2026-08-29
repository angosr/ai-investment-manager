import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from investment_manager.decision_cycle.service import _assemble_forecast_runtime
from investment_manager.forecast.context.analyst import configured_assess_behavior_hash
from investment_manager.forecast.context.posterior_contract import (
    POSTERIOR_PRODUCER_ID,
    posterior_behavior_hash,
)
from investment_manager.forecast.program.admission import (
    resolve_active_forecast_admissions,
)
from investment_manager.forecast.program.baseline import load_forecast_baseline
from investment_manager.forecast.program.prior import build_prior_targets
from investment_manager.governance.models import ReleaseArtifact, ReleaseManifest
from investment_manager.portfolio.models import CandidateCapitalAuthorization
from investment_manager.schema import create_schema
from investment_manager.settings import load_config


def _cohort():
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config/investment-manager.shadow.yaml")
    artifact = load_forecast_baseline(
        root / "evidence/forecast-baselines/forecast_baseline_7edf2cf090b47cdad2e5.json"
    )
    targets = tuple(
        sorted(build_prior_targets(artifact), key=lambda item: item.contract.contract_id)
    )
    behavior_id = posterior_behavior_hash(
        config.codex_runtime,
        contracts=tuple(item.contract for item in targets),
        prior_bindings=tuple(item.binding for item in targets),
        world_model_behavior_id=configured_assess_behavior_hash(config),
    )
    families = tuple(sorted(item.contract.outcome_family_id for item in targets))
    return config, artifact, behavior_id, families


def test_empty_admission_preserves_the_frozen_active_posterior() -> None:
    config, artifact, behavior_id, _families = _cohort()

    admissions = resolve_active_forecast_admissions(
        artifact=artifact,
        runtime=config.codex_runtime,
        world_model_behavior_id=configured_assess_behavior_hash(config),
        authorization_identities=(),
    )

    assert behavior_id == "07d94712727cce8ba04491b0939e2170038ce42baa91dd79e29e8967a739ba2f"
    assert admissions.prior_outcome_families == ()
    assert admissions.posterior_outcome_families == ()


def test_only_one_complete_active_behavior_can_receive_capital_admission() -> None:
    config, artifact, behavior_id, families = _cohort()
    identities = tuple((POSTERIOR_PRODUCER_ID, behavior_id, family) for family in families)

    admitted = resolve_active_forecast_admissions(
        artifact=artifact,
        runtime=config.codex_runtime,
        world_model_behavior_id=configured_assess_behavior_hash(config),
        authorization_identities=identities,
    )

    assert admitted.posterior_outcome_families == families
    with pytest.raises(ValueError, match="完整授权"):
        resolve_active_forecast_admissions(
            artifact=artifact,
            runtime=config.codex_runtime,
            world_model_behavior_id=configured_assess_behavior_hash(config),
            authorization_identities=identities[:1],
        )
    with pytest.raises(ValueError, match="不属于现役"):
        resolve_active_forecast_admissions(
            artifact=artifact,
            runtime=config.codex_runtime,
            world_model_behavior_id=configured_assess_behavior_hash(config),
            authorization_identities=((POSTERIOR_PRODUCER_ID, "unknown", families[0]),),
        )


def test_production_assembly_wires_future_authorization_without_changing_behavior() -> None:
    root = Path(__file__).resolve().parents[1]
    config, _artifact, behavior_id, families = _cohort()
    artifact_path = root / "evidence/forecast-baselines/forecast_baseline_7edf2cf090b47cdad2e5.json"
    manifest = ReleaseManifest(
        manifest_id="test-forecast-admission-release",
        created_at=datetime(2026, 8, 29, tzinfo=UTC),
        status="CHALLENGER",
        code_version="test",
        component_versions=(),
        artifacts=(
            ReleaseArtifact(
                artifact_id="forecast_baseline_7edf2cf090b47cdad2e5",
                relative_path=str(artifact_path.relative_to(root)),
                sha256=hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
            ),
        ),
        constitution_version="constitution-v2",
    )
    authorizations = tuple(
        CandidateCapitalAuthorization(
            version="test-posterior-capital-admission-v1",
            producer_id=POSTERIOR_PRODUCER_ID,
            producer_behavior_id=behavior_id,
            outcome_family_id=family,
            hypothesis_fingerprint="a" * 64,
        )
        for family in families
    )
    authorized = config.model_copy(
        update={
            "capital": config.capital.model_copy(
                update={"candidate_capital_authorizations": authorizations}
            )
        }
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)

    runtime = _assemble_forecast_runtime(
        config=authorized,
        manifest=manifest,
        engine=engine,
    )

    assert runtime.posterior is not None
    assert runtime.posterior.producer_behavior_id == behavior_id
    assert tuple(item.contract.outcome_family_id for item in runtime.capital_sources) == families
    assert all(item.capital_authorization is not None for item in runtime.capital_sources)
    assert all(
        item.binding.permission.value == "CAPITAL_CANDIDATE" for item in runtime.capital_sources
    )
