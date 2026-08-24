from datetime import UTC, datetime, timedelta
from decimal import Decimal

from investment_manager.forecast.context.evaluation import (
    ForecastEvidenceStatus,
    ForecastScoringCase,
    evaluate_forecast_evidence,
)


def _case(name: str, start: datetime, realized: str, probabilities) -> ForecastScoringCase:
    return ForecastScoringCase(
        forecast_id=name,
        information_cutoff_at=start,
        evaluation_at=start + timedelta(hours=4),
        probabilities=tuple((key, Decimal(value)) for key, value in probabilities),
        benchmark_probabilities=(("DOWN", Decimal("0.5")), ("UP", Decimal("0.5"))),
        realized_bucket_id=realized,
        expected_gross_bps=Decimal("10"),
        realized_gross_bps=Decimal("20") if realized == "UP" else Decimal("-20"),
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
