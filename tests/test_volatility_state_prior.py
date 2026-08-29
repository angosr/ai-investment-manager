from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from investment_manager.forecast.program.baseline import ForecastBaselineTargetResult
from investment_manager.market.models import ClosedMarketBar
from investment_manager.research.volatility_state_prior import (
    _evaluate_target,
    _trailing_mean_absolute_log_return,
)


def _dataset(*, count: int = 2_200) -> SimpleNamespace:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    price = Decimal("100")
    bars: list[ClosedMarketBar] = []
    for index in range(count):
        open_time = start + timedelta(days=index)
        open_price = price
        regime_scale = (Decimal("0.002"), Decimal("0.008"), Decimal("0.025"))[(index // 90) % 3]
        direction = Decimal("1") if index % 5 in (0, 1, 2) else Decimal("-1")
        price = open_price * (Decimal("1") + direction * regime_scale)
        bars.append(
            ClosedMarketBar(
                symbol="BTCUSDT",
                interval="1d",
                open_time=open_time,
                close_time=open_time + timedelta(days=1) - timedelta(milliseconds=1),
                observed_at=open_time + timedelta(days=1) - timedelta(milliseconds=1),
                open=open_price,
                high=max(open_price, price),
                low=min(open_price, price),
                close=price,
                volume=Decimal("10"),
                source="test-history",
            )
        )
    return SimpleNamespace(
        manifest=SimpleNamespace(
            symbol="BTCUSDT",
            dataset_id="test-volatility-dataset",
            bars_hash="a" * 64,
            interval="1d",
            last_close_time=bars[-1].close_time,
        ),
        bars=tuple(bars),
    )


def _baseline() -> ForecastBaselineTargetResult:
    return ForecastBaselineTargetResult(
        symbol="BTCUSDT",
        dataset_id="test-volatility-dataset",
        bars_hash="a" * 64,
        development_sample_count=480,
        validation_sample_count=240,
        first_validation_cutoff_at=datetime(2024, 1, 1, tzinfo=UTC),
        last_validation_outcome_at=datetime(2025, 12, 31, tzinfo=UTC),
        bucket_ids=("TAIL_LOSS", "LOSS", "MIDDLE", "GAIN", "TAIL_GAIN"),
        bucket_boundaries_bps=(
            Decimal("-300"),
            Decimal("-50"),
            Decimal("50"),
            Decimal("300"),
        ),
        representative_bps=(
            Decimal("-500"),
            Decimal("-150"),
            Decimal("0"),
            Decimal("150"),
            Decimal("500"),
        ),
        fixed_probabilities=(
            Decimal("0.2"),
            Decimal("0.2"),
            Decimal("0.2"),
            Decimal("0.2"),
            Decimal("0.2"),
        ),
        mean_rolling_probabilities=(
            Decimal("0.2"),
            Decimal("0.2"),
            Decimal("0.2"),
            Decimal("0.2"),
            Decimal("0.2"),
        ),
        terminal_probabilities=(
            Decimal("0.2"),
            Decimal("0.2"),
            Decimal("0.2"),
            Decimal("0.2"),
            Decimal("0.2"),
        ),
        terminal_bucket_counts=(100, 100, 100, 100, 100),
        realized_probabilities=(
            Decimal("0.2"),
            Decimal("0.2"),
            Decimal("0.2"),
            Decimal("0.2"),
            Decimal("0.2"),
        ),
        minimum_visible_history_count=100,
        maximum_visible_history_count=499,
        terminal_history_count=500,
        rolling_mean_brier=Decimal("0.8"),
        fixed_mean_brier=Decimal("0.8"),
        rolling_mean_ranked_probability_score=Decimal("0.2"),
        fixed_mean_ranked_probability_score=Decimal("0.2"),
        rolling_maximum_absolute_calibration_error=Decimal("0.01"),
    )


def test_feature_reads_only_completed_bars_at_cutoff() -> None:
    dataset = _dataset(count=120)
    cutoff = dataset.bars[60].close_time

    before = _trailing_mean_absolute_log_return(
        dataset,
        cutoff=cutoff,
        lookback_days=30,
    )
    changed_future = list(dataset.bars)
    changed_future[-1] = changed_future[-1].model_copy(
        update={"close": changed_future[-1].close * Decimal("10")}
    )
    after = _trailing_mean_absolute_log_return(
        SimpleNamespace(manifest=dataset.manifest, bars=tuple(changed_future)),
        cutoff=cutoff,
        lookback_days=30,
    )

    assert before == after


def test_target_evaluation_uses_one_frozen_state_model_and_reports_all_gates() -> None:
    dataset = _dataset()

    result = _evaluate_target(
        dataset,
        baseline=_baseline(),
        target={
            "symbol": "BTCUSDT",
            "dataset_id": "test-volatility-dataset",
            "bars_hash": "a" * 64,
        },
        development_end=date(2024, 1, 1),
        lookback_days=30,
        shrinkage_strength=Decimal("20"),
    )

    assert result.validation_sample_count > 100
    assert result.feature_thresholds[0] < result.feature_thresholds[1]
    assert all(count > 0 for count in result.terminal_regime_sample_counts)
    assert tuple(item.half for item in result.temporal_scores) == ("FIRST", "SECOND")
    assert isinstance(result.gate_brier_improved, bool)
    assert isinstance(result.gate_ranked_probability_improved, bool)
    assert isinstance(result.gate_temporal_stability, bool)
    assert isinstance(result.gate_calibration_not_worse, bool)
