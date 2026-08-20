from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from investment_manager.execution.models import Side
from investment_manager.forecast.models import EdgeCalibration
from investment_manager.forecast.policy import CalibrationPolicy
from investment_manager.kernel.identity import content_hash
from investment_manager.legacy.analyst import analysis_behavior_hash
from investment_manager.legacy.calibration import (
    EDGE_CALIBRATION_MISSING,
    CalibrationBuildSpec,
    EdgeCalibrationBook,
    EdgeCalibrationBuilder,
    uncalibrated_ref,
)
from investment_manager.legacy.cycle import AnalysisCycle
from investment_manager.legacy.decision import HighestNetEdgeComposer
from investment_manager.legacy.models import (
    CandidateOutcome,
    CandidateOutcomeStatus,
)


def test_published_calibration_replaces_only_the_edge_fields(
    app_config, base_app_config, replay_input
) -> None:
    raw = AnalysisCycle.create(base_app_config).run(replay_input).candidates[0]

    calibrated = EdgeCalibrationBook(app_config.calibration).apply(raw)

    assert calibrated.expected_gross_bps == Decimal("40")
    assert calibrated.calibration_ref == "test-price-trend-calibration-v1"
    assert EDGE_CALIBRATION_MISSING not in calibrated.unknowns
    assert calibrated.raw_score == raw.raw_score
    assert calibrated.candidate_id == raw.candidate_id


def test_calibration_is_point_in_time_and_fails_closed_outside_validity(
    app_config, base_app_config, replay_input
) -> None:
    raw = AnalysisCycle.create(base_app_config).run(replay_input).candidates[0]
    signal_at = app_config.calibration.artifacts[0].valid_until
    future = raw.model_copy(
        update={
            "signal_observed_at": signal_at,
            "valid_until": signal_at + timedelta(minutes=15),
        }
    )

    unresolved = EdgeCalibrationBook(app_config.calibration).apply(future)

    assert unresolved.expected_gross_bps == 0
    assert unresolved.calibration_ref.startswith("uncalibrated:")
    assert unresolved.unknowns == (EDGE_CALIBRATION_MISSING,)


def test_calibration_never_applies_an_artifact_from_a_different_source_cohort(
    app_config, base_app_config, replay_input
) -> None:
    raw = AnalysisCycle.create(base_app_config).run(replay_input).candidates[0]
    artifact = app_config.calibration.artifacts[0]
    payload = artifact.model_dump(mode="python", exclude={"artifact_hash"})
    payload["source_calibration_ref"] = "different-source-cohort"
    mismatched = EdgeCalibration(**payload, artifact_hash=content_hash(payload))
    policy = app_config.calibration.model_copy(update={"artifacts": (mismatched,)})

    unresolved = EdgeCalibrationBook(policy).apply(raw)

    assert unresolved.expected_gross_bps == 0
    assert unresolved.unknowns == (EDGE_CALIBRATION_MISSING,)


def test_calibration_isolates_analysis_behavior_cohorts(
    app_config, base_app_config, replay_input
) -> None:
    raw = AnalysisCycle.create(base_app_config).run(replay_input).candidates[0]
    behavior_ref = uncalibrated_ref(raw.producer_version, "a" * 64)
    artifact = app_config.calibration.artifacts[0]
    payload = artifact.model_dump(mode="python", exclude={"artifact_hash"})
    payload["source_calibration_ref"] = behavior_ref
    scoped = EdgeCalibration(**payload, artifact_hash=content_hash(payload))
    policy = app_config.calibration.model_copy(update={"artifacts": (scoped,)})

    calibrated = EdgeCalibrationBook(policy).apply(
        raw.model_copy(update={"calibration_ref": behavior_ref})
    )
    unresolved = EdgeCalibrationBook(policy).apply(
        raw.model_copy(
            update={
                "calibration_ref": uncalibrated_ref(raw.producer_version, "b" * 64)
            }
        )
    )

    assert calibrated.calibration_ref == scoped.calibration_id
    assert unresolved.calibration_ref.endswith("b" * 64)
    assert unresolved.unknowns == (EDGE_CALIBRATION_MISSING,)


def test_publishing_calibration_does_not_rotate_and_self_lock_source_cohort(
    app_config, base_app_config, replay_input
) -> None:
    behavior_hash = analysis_behavior_hash(base_app_config)
    source_ref = uncalibrated_ref(base_app_config.proposal.version, behavior_hash)
    program_candidate = AnalysisCycle.create(base_app_config).run(replay_input).candidates[0]
    ai_candidate = program_candidate.model_copy(
        update={
            "producer_id": base_app_config.proposal.producer_id,
            "producer_version": base_app_config.proposal.version,
            "strategy_family": base_app_config.proposal.strategy_family,
            "calibration_ref": source_ref,
        }
    )
    template = app_config.calibration.artifacts[0]
    payload = template.model_dump(mode="python", exclude={"artifact_hash"})
    payload.update(
        {
            "calibration_id": "test-codex-calibration-v1",
            "producer_id": ai_candidate.producer_id,
            "producer_version": ai_candidate.producer_version,
            "horizon_minutes": ai_candidate.horizon_minutes,
            "source_calibration_ref": source_ref,
        }
    )
    artifact = EdgeCalibration(**payload, artifact_hash=content_hash(payload))
    published = base_app_config.model_copy(
        update={
            "calibration": base_app_config.calibration.model_copy(
                update={
                    "version": "published-codex-calibration-v1",
                    "artifacts": (artifact,),
                }
            )
        }
    )

    assert analysis_behavior_hash(published) == behavior_hash
    calibrated = EdgeCalibrationBook(published.calibration).apply(ai_candidate)
    assert calibrated.calibration_ref == artifact.calibration_id
    assert calibrated.expected_gross_bps == artifact.conservative_gross_bps


def test_calibration_rejects_malformed_behavior_cohort(
    app_config, base_app_config, replay_input
) -> None:
    raw = AnalysisCycle.create(base_app_config).run(replay_input).candidates[0]
    malformed = raw.model_copy(
        update={"calibration_ref": f"uncalibrated:{raw.producer_version}@not-a-hash"}
    )

    with pytest.raises(ValueError, match="不得自行填充校准收益"):
        EdgeCalibrationBook(app_config.calibration).apply(malformed)


def test_candidate_producer_cannot_self_assign_expected_edge(
    app_config, base_app_config, replay_input
) -> None:
    raw = AnalysisCycle.create(base_app_config).run(replay_input).candidates[0]
    forged = raw.model_copy(update={"expected_gross_bps": Decimal("999")})

    with pytest.raises(ValueError, match="不得自行填充校准收益"):
        EdgeCalibrationBook(app_config.calibration).apply(forged)


def test_composer_ranks_calibrated_candidates_by_conservative_net_edge_not_raw_score(
    app_config, replay_input
) -> None:
    program = AnalysisCycle.create(app_config).prepare(replay_input).candidates[0]
    ai = program.model_copy(
        update={
            "candidate_id": "higher-confidence-lower-edge",
            "producer_id": "codex-analyst",
            "producer_version": "test-codex-v1",
            "raw_score": Decimal("0.99"),
            "expected_gross_bps": Decimal("35"),
            "calibration_ref": "test-codex-calibration-v1",
        }
    )

    result = HighestNetEdgeComposer(app_config.composition, app_config.pipeline.version).compose(
        (ai, program), as_of=replay_input.market.as_of
    )

    assert result.intent is not None
    assert result.intent.candidate_ids == (program.candidate_id,)


def test_calibration_artifact_rejects_optimistic_bound(app_config) -> None:
    artifact = app_config.calibration.artifacts[0]
    payload = artifact.model_dump(mode="python")
    payload["conservative_gross_bps"] = artifact.expected_gross_bps + Decimal("1")

    with pytest.raises(ValidationError, match="保守毛优势不能高于均值估计"):
        EdgeCalibration.model_validate(payload)


def test_calibration_policy_rejects_overlapping_scope(app_config) -> None:
    artifact = app_config.calibration.artifacts[0]
    duplicate_payload = artifact.model_dump(mode="python", exclude={"artifact_hash"})
    duplicate_payload["calibration_id"] = "overlapping-calibration-v2"
    duplicate = EdgeCalibration(
        **duplicate_payload,
        artifact_hash=content_hash(duplicate_payload),
    )

    with pytest.raises(ValidationError, match="有效期不得重叠"):
        CalibrationPolicy(
            version="invalid-overlap-v1",
            minimum_non_overlapping_samples=30,
            artifacts=(artifact, duplicate),
        )


def _settled_outcome(
    index: int,
    *,
    signal_at: datetime,
    evaluation_at: datetime,
    gross_bps: str,
    settled_at: datetime | None = None,
) -> CandidateOutcome:
    return CandidateOutcome(
        outcome_id=f"outcome-{index}",
        candidate_id=f"candidate-{index}",
        cycle_id=f"cycle-{index}",
        producer_id="price-trend",
        producer_version="price-trend-v4",
        calibration_ref="uncalibrated:price-trend-v4",
        evaluation_version="candidate-evaluation-v1",
        execution_policy_version="mock-execution-v1",
        frequency_policy_version="frequency-v3",
        symbol="BTCUSDT",
        side=Side.BUY,
        status=CandidateOutcomeStatus.SETTLED,
        signal_observed_at=signal_at,
        evaluation_at=evaluation_at,
        settled_at=settled_at or evaluation_at,
        reference_price=Decimal("100"),
        exit_price=Decimal("101"),
        exit_event_time=evaluation_at,
        gross_return_bps=Decimal(gross_bps),
        estimated_cost_bps=Decimal("20"),
        net_return_bps=Decimal(gross_bps) - Decimal("20"),
        reason_code="HORIZON_RETURN_AVAILABLE",
    )


def _build_spec() -> CalibrationBuildSpec:
    return CalibrationBuildSpec(
        producer_id="price-trend",
        producer_version="price-trend-v4",
        symbol="BTCUSDT",
        side=Side.BUY,
        horizon_minutes=60,
        evaluation_version="candidate-evaluation-v1",
        source_calibration_ref="uncalibrated:price-trend-v4",
        source_execution_policy_version="mock-execution-v1",
        source_frequency_policy_version="frequency-v3",
        training_start=datetime(2026, 1, 1, tzinfo=UTC),
        training_end=datetime(2026, 1, 5, tzinfo=UTC),
        published_at=datetime(2026, 1, 6, tzinfo=UTC),
        valid_from=datetime(2026, 1, 6, tzinfo=UTC),
        valid_until=datetime(2026, 2, 1, tzinfo=UTC),
    )


def test_builder_uses_only_point_in_time_non_overlapping_exact_scope_samples() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    outcomes = (
        _settled_outcome(
            1,
            signal_at=start,
            evaluation_at=start + timedelta(hours=1),
            gross_bps="10",
        ),
        _settled_outcome(
            2,
            signal_at=start + timedelta(minutes=30),
            evaluation_at=start + timedelta(minutes=90),
            gross_bps="999",
        ),
        _settled_outcome(
            3,
            signal_at=start + timedelta(days=1),
            evaluation_at=start + timedelta(days=1, hours=1),
            gross_bps="20",
        ),
        _settled_outcome(
            4,
            signal_at=start + timedelta(days=2),
            evaluation_at=start + timedelta(days=2, hours=1),
            gross_bps="30",
        ),
    )
    policy = CalibrationPolicy(
        version="test-builder-v1",
        minimum_non_overlapping_samples=3,
        method_version="mean-lower-bound-v1",
        lower_confidence_z=Decimal("1.96"),
    )

    artifact = EdgeCalibrationBuilder(policy).build(outcomes, _build_spec())

    assert artifact.sample_size == 4
    assert artifact.non_overlapping_sample_size == 3
    assert artifact.expected_gross_bps == Decimal("20")
    assert artifact.conservative_gross_bps < artifact.expected_gross_bps
    assert artifact.source_calibration_ref == "uncalibrated:price-trend-v4"
    assert len(artifact.dataset_hash) == 64
    assert len(artifact.artifact_hash) == 64


def test_builder_excludes_labels_not_visible_at_publication() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    late = datetime(2026, 1, 7, tzinfo=UTC)
    outcomes = tuple(
        _settled_outcome(
            index,
            signal_at=start + timedelta(days=index - 1),
            evaluation_at=start + timedelta(days=index - 1, hours=1),
            settled_at=late,
            gross_bps="20",
        )
        for index in range(1, 4)
    )
    policy = CalibrationPolicy(
        version="test-builder-v1",
        minimum_non_overlapping_samples=3,
    )

    with pytest.raises(ValueError, match="非重叠成熟样本不足"):
        EdgeCalibrationBuilder(policy).build(outcomes, _build_spec())


def test_calibration_identity_changes_with_artifact_validity() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    outcomes = tuple(
        _settled_outcome(
            index,
            signal_at=start + timedelta(days=index - 1),
            evaluation_at=start + timedelta(days=index - 1, hours=1),
            gross_bps=str(index * 10),
        )
        for index in range(1, 4)
    )
    policy = CalibrationPolicy(
        version="test-builder-v1",
        minimum_non_overlapping_samples=3,
        lower_confidence_z=Decimal("1.96"),
    )
    base_spec = _build_spec()
    extended_spec = replace(base_spec, valid_until=base_spec.valid_until + timedelta(days=1))

    base = EdgeCalibrationBuilder(policy).build(outcomes, base_spec)
    extended = EdgeCalibrationBuilder(policy).build(outcomes, extended_spec)

    assert base.dataset_hash == extended.dataset_hash
    assert base.calibration_id != extended.calibration_id
    assert base.artifact_hash != extended.artifact_hash


def test_artifact_rejects_tampered_content_hash(app_config) -> None:
    payload = app_config.calibration.artifacts[0].model_dump(mode="python")
    payload["expected_gross_bps"] += Decimal("1")

    with pytest.raises(ValidationError, match="制品哈希与内容不一致"):
        EdgeCalibration.model_validate(payload)
