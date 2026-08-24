from datetime import UTC, datetime, timedelta
from decimal import Decimal

from investment_manager.forecast.context.evaluation import (
    ForecastEvidenceStatus,
    ForecastScoringCase,
    evaluate_forecast_evidence,
)


def _case(
    name: str,
    start: datetime,
    realized: str,
    probabilities,
    *,
    market_state_key: str | None = None,
    outcome_available_at: datetime | None = None,
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
    )


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

    assert evidence.status == ForecastEvidenceStatus.ABOVE_BENCHMARK
    assert evidence.settled_forecast_count == 3
    assert evidence.non_overlapping_sample_count == 2
    assert evidence.mean_brier_score == Decimal("0.13")
    assert evidence.benchmark_mean_brier_score == Decimal("0.5")
    assert evidence.brier_skill == Decimal("0.37")
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
    cases = tuple(
        _case(
            f"case-{index}",
            start + timedelta(hours=4 * index),
            "UP",
            (("DOWN", "0.5"), ("UP", "0.5")),
        )
        for index in range(2)
    )

    evidence = evaluate_forecast_evidence(
        cases,
        due_slot_count=2,
        forecast_count=2,
        no_estimate_count=0,
        required_non_overlapping_samples=2,
    )

    assert evidence.status == ForecastEvidenceStatus.INCONCLUSIVE
    assert evidence.rolling_brier_skill == Decimal("0.0")
    assert evidence.rolling_brier_skill_lower_bound == Decimal("0.0")


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
    assert evidence.rolling_benchmark_mean_brier_score == Decimal("0.50")


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
    assert (
        evidence.rolling_benchmark_mean_brier_score
        != evidence.market_benchmark_mean_brier_score
    )
