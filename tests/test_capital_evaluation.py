from datetime import UTC, datetime
from decimal import Decimal

import pytest

from investment_manager.governance.evaluation.capital import (
    CapitalShadowEvaluationSpec,
    build_capital_shadow_evaluation_plan,
    validate_capital_shadow_evaluation_plan,
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

    assert spec.source_policy_version == "spot-perp-calendar-month-risk-30pct-v3"
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


def test_capital_shadow_startup_requires_one_exact_preregistered_contract() -> None:
    config, manifest = _release()
    spec = CapitalShadowEvaluationSpec.freeze(
        plan_id="capital-shadow-startup-v1",
        config=config,
        manifest=manifest,
        observation_start=datetime(2026, 9, 1, tzinfo=UTC),
        observation_end=datetime(2027, 9, 1, tzinfo=UTC),
    )
    plan = build_capital_shadow_evaluation_plan(
        spec=spec,
        registered_at=datetime(2026, 8, 21, tzinfo=UTC),
    )

    assert validate_capital_shadow_evaluation_plan(
        config=config,
        manifest=manifest,
        plans=(plan,),
        started_at=datetime(2026, 8, 22, tzinfo=UTC),
    ) == (spec, plan)

    with pytest.raises(ValueError, match="恰好绑定一个"):
        validate_capital_shadow_evaluation_plan(
            config=config,
            manifest=manifest,
            plans=(),
            started_at=datetime(2026, 8, 22, tzinfo=UTC),
        )

    with pytest.raises(ValueError, match="完整合同不一致"):
        validate_capital_shadow_evaluation_plan(
            config=config,
            manifest=manifest,
            plans=(plan.model_copy(update={"primary_metric": "gross_return"}),),
            started_at=datetime(2026, 8, 22, tzinfo=UTC),
        )


def test_capital_shadow_startup_rejects_ambiguous_or_future_registration() -> None:
    config, manifest = _release()
    start = datetime(2026, 9, 1, tzinfo=UTC)
    first_spec = CapitalShadowEvaluationSpec.freeze(
        plan_id="capital-shadow-startup-first",
        config=config,
        manifest=manifest,
        observation_start=start,
        observation_end=datetime(2027, 9, 1, tzinfo=UTC),
    )
    second_spec = first_spec.model_copy(
        update={"plan_id": "capital-shadow-startup-second"}
    )
    first = build_capital_shadow_evaluation_plan(
        spec=first_spec,
        registered_at=datetime(2026, 8, 21, tzinfo=UTC),
    )
    second = build_capital_shadow_evaluation_plan(
        spec=second_spec,
        registered_at=datetime(2026, 8, 21, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="恰好绑定一个"):
        validate_capital_shadow_evaluation_plan(
            config=config,
            manifest=manifest,
            plans=(first, second),
            started_at=datetime(2026, 8, 22, tzinfo=UTC),
        )

    future = build_capital_shadow_evaluation_plan(
        spec=first_spec,
        registered_at=datetime(2026, 8, 30, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="晚于本次服务启动"):
        validate_capital_shadow_evaluation_plan(
            config=config,
            manifest=manifest,
            plans=(future,),
            started_at=datetime(2026, 8, 22, tzinfo=UTC),
        )
