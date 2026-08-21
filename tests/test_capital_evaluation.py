from datetime import UTC, datetime
from decimal import Decimal

import pytest

from investment_manager.governance.evaluation.capital import (
    CapitalShadowEvaluationSpec,
    build_capital_shadow_evaluation_plan,
)
from investment_manager.governance.models import (
    EvaluationStage,
    ReleaseArtifact,
    load_release_manifest,
)
from investment_manager.kernel.identity import content_hash
from investment_manager.settings import load_config


def _release():
    config = load_config("config/investment-manager.shadow.yaml")
    historical = load_release_manifest("config/release-manifest.yaml")
    evidence = config.carry_forecast.evidence
    assert evidence is not None
    manifest = historical.model_copy(
        update={
            "manifest_id": "release-capital-shadow-evaluation-test",
            "code_version": "a" * 40,
            "configuration_hash": content_hash(config),
            "component_versions": tuple(
                (name, getattr(config, name).version)
                for name, _version in historical.component_versions
            ),
            "artifacts": (
                ReleaseArtifact(
                    artifact_id=evidence.source_evaluation_id,
                    sha256=evidence.source_artifact_sha256,
                ),
            ),
        }
    )
    return config, manifest


def test_capital_shadow_plan_freezes_release_baselines_and_failure_rules() -> None:
    config, manifest = _release()
    start = datetime(2026, 9, 1, tzinfo=UTC)
    spec = CapitalShadowEvaluationSpec.freeze(
        plan_id="capital-shadow-202609-v1",
        config=config,
        manifest=manifest,
        observation_start=start,
        observation_end=datetime(2027, 9, 1, tzinfo=UTC),
    )
    plan = build_capital_shadow_evaluation_plan(
        spec=spec,
        registered_at=datetime(2026, 8, 21, tzinfo=UTC),
    )

    assert spec.source_policy_version == "spot-perp-monthly-risk-30pct-v2"
    assert spec.thresholds.calendar_months == 12
    assert spec.thresholds.minimum_forecast_available_months == 11
    assert spec.thresholds.minimum_decision_complete_months == 12
    assert spec.thresholds.maximum_duplicate_execution_groups == 0
    assert (
        spec.thresholds.maximum_source_policy_underperformance_fraction
        == Decimal("0.005")
    )
    assert "COMPENSATION_LOSS" in spec.accounting_dimensions
    assert plan.base_manifest_id == manifest.manifest_id
    assert plan.required_stages[-1] == EvaluationStage.SHADOW
    assert plan.candidate_spec_hash == content_hash(spec)


def test_capital_shadow_plan_rejects_short_or_retrospective_windows() -> None:
    config, manifest = _release()
    with pytest.raises(ValueError, match="至少需要十二个"):
        CapitalShadowEvaluationSpec.freeze(
            plan_id="capital-shadow-too-short",
            config=config,
            manifest=manifest,
            observation_start=datetime(2026, 9, 1, tzinfo=UTC),
            observation_end=datetime(2027, 8, 1, tzinfo=UTC),
        )

    spec = CapitalShadowEvaluationSpec.freeze(
        plan_id="capital-shadow-too-late",
        config=config,
        manifest=manifest,
        observation_start=datetime(2026, 9, 1, tzinfo=UTC),
        observation_end=datetime(2027, 9, 1, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="首个观察月开始前"):
        build_capital_shadow_evaluation_plan(
            spec=spec,
            registered_at=datetime(2026, 9, 1, tzinfo=UTC),
        )
