from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from investment_manager.forecast.context.evaluation import (
    ForecastEvidenceStatus,
    ForecastPairPanelCase,
    ForecastScoringCase,
    evaluate_forecast_evidence,
    evaluate_forecast_pair_evidence,
)
from investment_manager.forecast.contracts import ForecastSlotStratum


def _case(
    name: str,
    start: datetime,
    realized: str,
    probabilities,
    *,
    market_state_key: str | None = None,
    outcome_available_at: datetime | None = None,
    source_stratum: ForecastSlotStratum = ForecastSlotStratum.CADENCE_ONLY,
) -> ForecastScoringCase:
    return ForecastScoringCase(
        forecast_id=name,
        information_cutoff_at=start,
        evaluation_at=start + timedelta(hours=4),
        probabilities=tuple((key, Decimal(value)) for key, value in probabilities),
        benchmark_probabilities=(("DOWN", Decimal("0.5")), ("UP", Decimal("0.5"))),
        realized_bucket_id=realized,
        expected_gross_bps=Decimal("10"),
        realized_gross_bps=Decimal("20") if realized == "UP" else Decimal("-20"),
        market_state_key=market_state_key,
        outcome_available_at=outcome_available_at,
        source_stratum=source_stratum,
    )


def _pair_case(
    name: str,
    start: datetime,
    *,
    candidate: str,
    comparator: str,
    targets: int = 2,
) -> ForecastPairPanelCase:
    return ForecastPairPanelCase(
        panel_id=name,
        information_cutoff_at=start,
        evaluation_at=start + timedelta(hours=4),
        source_stratum=ForecastSlotStratum.CADENCE_ONLY,
        paired_target_count=targets,
        candidate_brier_score=Decimal(candidate),
        comparator_brier_score=Decimal(comparator),
        mean_max_bucket_probability_delta=Decimal("0.07"),
        mean_expected_gross_bps_delta=Decimal("3.5"),
    )


def test_forecast_pair_evidence_is_empty_without_shared_settled_panels() -> None:
    evidence = evaluate_forecast_pair_evidence(())

    assert evidence.settled_panel_count == 0
    assert evidence.paired_target_count == 0
    assert evidence.non_overlapping_panel_count == 0
    assert evidence.mean_brier_improvement is None
    assert evidence.brier_improvement_lower_bound is None


def test_forecast_pair_evidence_scores_only_shared_non_overlapping_panels() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    evidence = evaluate_forecast_pair_evidence(
        (
            _pair_case("first", start, candidate="0.2", comparator="0.4"),
            _pair_case(
                "overlap",
                start + timedelta(hours=1),
                candidate="0.8",
                comparator="0.1",
                targets=1,
            ),
            _pair_case(
                "second",
                start + timedelta(hours=4),
                candidate="0.3",
                comparator="0.5",
            ),
        )
    )

    assert evidence.settled_panel_count == 3
    assert evidence.paired_target_count == 5
    assert evidence.non_overlapping_panel_count == 2
    assert evidence.mean_candidate_brier_score == Decimal("0.25")
    assert evidence.mean_comparator_brier_score == Decimal("0.45")
    assert evidence.mean_brier_improvement == Decimal("0.20")
    assert evidence.brier_improvement_lower_bound == Decimal("0.20")
    assert evidence.brier_improvement_upper_bound == Decimal("0.20")
    assert evidence.candidate_better_panel_count == 2
    assert evidence.candidate_worse_panel_count == 0
    assert evidence.mean_max_bucket_probability_delta == Decimal("0.07")


def test_forecast_evidence_rejects_duplicate_interval_within_source_stratum() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    cases = (
        _case("first", start, "UP", (("DOWN", "0.2"), ("UP", "0.8"))),
        _case("duplicate", start, "UP", (("DOWN", "0.3"), ("UP", "0.7"))),
    )

    with pytest.raises(ValueError, match="同一 Forecast 来源层"):
        evaluate_forecast_evidence(
            cases,
            due_slot_count=2,
            forecast_count=2,
            no_estimate_count=0,
            required_non_overlapping_samples=2,
        )


def test_forecast_evidence_tie_break_never_depends_on_forecast_id() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    cadence = _case(
        "z-cadence",
        start,
        "UP",
        (("DOWN", "0.2"), ("UP", "0.8")),
    )
    material = _case(
        "a-material",
        start,
        "DOWN",
        (("DOWN", "0.7"), ("UP", "0.3")),
        source_stratum=ForecastSlotStratum.MATERIAL_STATE_ONLY,
    )

    evidence = evaluate_forecast_evidence(
        (material, cadence),
        due_slot_count=2,
        forecast_count=2,
        no_estimate_count=0,
        required_non_overlapping_samples=2,
        permission_evidence_eligible=False,
    )

    assert evidence.status == ForecastEvidenceStatus.DIAGNOSTIC_ONLY
    assert evidence.mean_brier_score == Decimal("0.08")


def test_forecast_evidence_scores_only_non_overlapping_cases() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    cases = (
        _case("first", start, "UP", (("DOWN", "0.2"), ("UP", "0.8"))),
        _case(
            "overlap",
            start + timedelta(hours=1),
            "DOWN",
            (("DOWN", "0.8"), ("UP", "0.2")),
        ),
        _case(
            "second",
            start + timedelta(hours=4),
            "UP",
            (("DOWN", "0.3"), ("UP", "0.7")),
        ),
    )

    evidence = evaluate_forecast_evidence(
        cases,
        due_slot_count=4,
        forecast_count=3,
        no_estimate_count=1,
        required_non_overlapping_samples=2,
    )

    assert evidence.status == ForecastEvidenceStatus.INSUFFICIENT_EVIDENCE
    assert evidence.settled_forecast_count == 3
    assert evidence.non_overlapping_sample_count == 2
    assert evidence.mean_brier_score == Decimal("0.13")
    assert evidence.benchmark_mean_brier_score == Decimal("0.5")
    assert evidence.brier_skill == Decimal("0.37")
    assert evidence.rolling_benchmark_mean_brier_score is None
    assert evidence.market_benchmark_mean_brier_score is None
    assert evidence.result_coverage == Decimal("1")


def test_forecast_evidence_never_claims_skill_before_sample_threshold() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    evidence = evaluate_forecast_evidence(
        (_case("first", start, "UP", (("DOWN", "0.1"), ("UP", "0.9"))),),
        due_slot_count=1,
        forecast_count=1,
        no_estimate_count=0,
        required_non_overlapping_samples=30,
    )

    assert evidence.status == ForecastEvidenceStatus.INSUFFICIENT_EVIDENCE
    assert evidence.brier_skill is not None and evidence.brier_skill > 0


def test_forecast_evidence_uses_only_prior_settled_history_for_dynamic_baseline() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    cases = (
        *(
            _case(
                f"history-{index}",
                start + timedelta(hours=4 * index),
                "UP",
                (("DOWN", "0.5"), ("UP", "0.5")),
                market_state_key="TRENDING_UP",
            )
            for index in range(5)
        ),
        _case(
            "current",
            start + timedelta(hours=20),
            "DOWN",
            (("DOWN", "0.5"), ("UP", "0.5")),
            market_state_key="TRENDING_UP",
        ),
    )

    evidence = evaluate_forecast_evidence(
        cases,
        due_slot_count=6,
        forecast_count=6,
        no_estimate_count=0,
        required_non_overlapping_samples=6,
    )

    assert evidence.rolling_baseline_ready_count == 1
    assert evidence.market_baseline_ready_count == 1
    assert evidence.rolling_benchmark_mean_brier_score is not None
    assert evidence.market_benchmark_mean_brier_score is not None
    assert evidence.rolling_benchmark_mean_brier_score > evidence.benchmark_mean_brier_score
    assert evidence.market_benchmark_mean_brier_score > evidence.benchmark_mean_brier_score
    assert evidence.rolling_brier_skill is not None
    assert evidence.rolling_brier_skill > 0


def test_forecast_evidence_does_not_claim_a_tied_mean_is_skill() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    realized = ("UP", "DOWN", "UP", "DOWN", "UP")
    cases = (
        *(
            _case(
                f"history-{index}",
                start + timedelta(hours=4 * index),
                outcome,
                (("DOWN", "0.5"), ("UP", "0.5")),
                market_state_key="RANGING",
            )
            for index, outcome in enumerate(realized)
        ),
        _case(
            "ready-1",
            start + timedelta(hours=20),
            "DOWN",
            (("DOWN", "0.4375"), ("UP", "0.5625")),
            market_state_key="RANGING",
        ),
        _case(
            "ready-2",
            start + timedelta(hours=24),
            "UP",
            (("DOWN", "0.5"), ("UP", "0.5")),
            market_state_key="RANGING",
        ),
    )

    evidence = evaluate_forecast_evidence(
        cases,
        due_slot_count=7,
        forecast_count=7,
        no_estimate_count=0,
        required_non_overlapping_samples=2,
    )

    assert evidence.status == ForecastEvidenceStatus.INCONCLUSIVE
    assert evidence.rolling_baseline_ready_count == 2
    assert evidence.market_baseline_ready_count == 2
    assert evidence.rolling_brier_skill == Decimal("0.0")
    assert evidence.rolling_brier_skill_lower_bound == Decimal("0.0")


def test_forecast_evidence_claims_skill_only_with_enough_dynamic_pairs() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    realized = ("UP", "DOWN", "UP", "DOWN", "UP")
    cases = (
        *(
            _case(
                f"history-{index}",
                start + timedelta(hours=4 * index),
                outcome,
                (("DOWN", "0.5"), ("UP", "0.5")),
                market_state_key="RANGING",
            )
            for index, outcome in enumerate(realized)
        ),
        _case(
            "ready-1",
            start + timedelta(hours=20),
            "DOWN",
            (("DOWN", "0.9"), ("UP", "0.1")),
            market_state_key="RANGING",
        ),
        _case(
            "ready-2",
            start + timedelta(hours=24),
            "UP",
            (("DOWN", "0.1"), ("UP", "0.9")),
            market_state_key="RANGING",
        ),
    )

    evidence = evaluate_forecast_evidence(
        cases,
        due_slot_count=7,
        forecast_count=7,
        no_estimate_count=0,
        required_non_overlapping_samples=2,
    )

    assert evidence.status == ForecastEvidenceStatus.ABOVE_BENCHMARK
    assert evidence.rolling_baseline_ready_count == 2
    assert evidence.market_baseline_ready_count == 2
    assert evidence.rolling_brier_skill_lower_bound is not None
    assert evidence.rolling_brier_skill_lower_bound > 0
    assert evidence.market_brier_skill_lower_bound is not None
    assert evidence.market_brier_skill_lower_bound > 0


def test_dynamic_baseline_excludes_outcomes_not_yet_settled_at_cutoff() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    current_at = start + timedelta(hours=20)
    cases = (
        *(
            _case(
                f"late-{index}",
                start + timedelta(hours=4 * index),
                "UP",
                (("DOWN", "0.5"), ("UP", "0.5")),
                market_state_key="TRENDING_UP",
                outcome_available_at=current_at + timedelta(minutes=1),
            )
            for index in range(5)
        ),
        _case(
            "current",
            current_at,
            "DOWN",
            (("DOWN", "0.5"), ("UP", "0.5")),
            market_state_key="TRENDING_UP",
        ),
    )

    evidence = evaluate_forecast_evidence(
        cases,
        due_slot_count=6,
        forecast_count=6,
        no_estimate_count=0,
        required_non_overlapping_samples=6,
    )

    assert evidence.rolling_baseline_ready_count == 0
    assert evidence.market_baseline_ready_count == 0
    assert evidence.rolling_benchmark_mean_brier_score is None
    assert evidence.market_benchmark_mean_brier_score is None


def test_market_baseline_is_distinct_from_rolling_unconditional_history() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    history = tuple(
        _case(
            f"history-{index}",
            start + timedelta(hours=4 * index),
            "UP" if index % 2 == 0 else "DOWN",
            (("DOWN", "0.5"), ("UP", "0.5")),
            market_state_key="TRENDING_UP" if index % 2 == 0 else "TRENDING_DOWN",
        )
        for index in range(10)
    )
    cases = (
        *history,
        _case(
            "current",
            start + timedelta(hours=40),
            "DOWN",
            (("DOWN", "0.5"), ("UP", "0.5")),
            market_state_key="TRENDING_UP",
        ),
    )

    evidence = evaluate_forecast_evidence(
        cases,
        due_slot_count=11,
        forecast_count=11,
        no_estimate_count=0,
        required_non_overlapping_samples=11,
    )

    assert evidence.rolling_baseline_ready_count == 6
    assert evidence.market_baseline_ready_count == 1
    assert evidence.rolling_benchmark_mean_brier_score != evidence.market_benchmark_mean_brier_score
