from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

from investment_manager.forecast.program.baseline import (
    ForecastBaselineArtifact,
    load_forecast_baseline,
    store_forecast_baseline,
)
from investment_manager.kernel.identity import stable_id
from investment_manager.market.models import ClosedMarketBar
from investment_manager.research.dataset import (
    HistoricalDataset,
    HistoricalDatasetCatalog,
    HistoricalDatasetManifest,
    InstrumentSpec,
    _bars_hash,
)
from investment_manager.research.forecast_prior import (
    ForecastBaselineEstimation,
    ForecastBaselineEvaluation,
    ForecastBaselinePlan,
    ForecastBaselineSampleContract,
    ForecastBaselineScope,
    ForecastBaselineTarget,
    SettledReturn,
    build_non_overlapping_returns,
    evaluate_forecast_baseline,
    expanding_prior,
)


def _dataset(*, count: int = 2200) -> HistoricalDataset:
    start = datetime(2018, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=count)
    instrument = InstrumentSpec(
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        price_increment=Decimal("0.01"),
        quantity_increment=Decimal("0.00001"),
        minimum_quantity=Decimal("0.00001"),
        maximum_quantity=Decimal("9000"),
        minimum_notional=Decimal("5"),
        minimum_price=Decimal("0.01"),
        maximum_price=Decimal("1000000"),
    )
    bars: list[ClosedMarketBar] = []
    price = Decimal("10000")
    for index in range(count):
        open_time = start + timedelta(days=index)
        open_price = price
        daily_bps = Decimal(((index * 37) % 101) - 50)
        price = open_price * (Decimal("1") + daily_bps / Decimal("10000"))
        bars.append(
            ClosedMarketBar(
                symbol=instrument.symbol,
                interval="1d",
                open_time=open_time,
                close_time=open_time + timedelta(days=1) - timedelta(milliseconds=1),
                observed_at=open_time + timedelta(days=1) - timedelta(milliseconds=1),
                open=open_price,
                high=max(open_price, price) * Decimal("1.001"),
                low=min(open_price, price) * Decimal("0.999"),
                close=price,
                volume=Decimal("10"),
                source="test-history",
            )
        )
    frozen = tuple(bars)
    bars_hash = _bars_hash(frozen)
    dataset_id = stable_id(
        "historical_dataset",
        "historical-bars-v1",
        "test-history",
        instrument.symbol,
        "1d",
        start,
        end,
        bars_hash,
        instrument,
    )
    return HistoricalDataset(
        manifest=HistoricalDatasetManifest(
            dataset_id=dataset_id,
            symbol=instrument.symbol,
            interval="1d",
            source="test-history",
            collected_at=end,
            requested_start=start,
            requested_end=end,
            first_open_time=frozen[0].open_time,
            last_close_time=frozen[-1].close_time,
            bar_count=len(frozen),
            bars_hash=bars_hash,
            instrument=instrument,
        ),
        bars=frozen,
    )


def _plan(dataset: HistoricalDataset) -> ForecastBaselinePlan:
    return ForecastBaselinePlan(
        schema_version="forecast-baseline-plan-v1",
        plan_id="test-prior-v1",
        purpose="为世界认知提供点时比较坐标。",
        targets=(
            ForecastBaselineTarget(
                symbol=dataset.manifest.symbol,
                dataset_id=dataset.manifest.dataset_id,
                bars_hash=dataset.manifest.bars_hash,
            ),
        ),
        sample_contract=ForecastBaselineSampleContract(
            interval="1d",
            horizon_days=3,
            slot_phase_epoch=date(1970, 1, 1),
            slot_phase_days=3,
            information_cutoff="completed_bar_close",
            outcome="close_to_close_return_bps",
            settlement_availability="outcome_endpoint_bar_close",
            independence="non_overlapping_cadence_slots",
        ),
        estimation=ForecastBaselineEstimation(
            development_end_exclusive=date(2022, 1, 1),
            bucket_quantiles=(
                Decimal("0.20"),
                Decimal("0.40"),
                Decimal("0.60"),
                Decimal("0.80"),
            ),
            quantile_method="empirical_nearest_rank",
            representative="development_bucket_median",
            fixed_benchmark="development_empirical_distribution",
            forecast_prior="只使用当前信息截止前已经结算的结果。",
        ),
        evaluation=ForecastBaselineEvaluation(
            period="from_development_boundary_to_last_settleable_slot",
            metrics=(
                "multiclass_brier",
                "ordinal_ranked_probability_score",
                "bucket_frequency_calibration",
            ),
            validity_rule="所有点时与概率约束必须成立。",
        ),
        scope=ForecastBaselineScope(
            excluded_target="SPY 尚未形成产品映射。",
            capital_change="NONE",
            historical_claim="不能证明 AI 或资本 Alpha。",
        ),
    )


def test_expanding_prior_cannot_see_future_outcome() -> None:
    cutoff = datetime(2026, 1, 10, tzinfo=UTC)
    visible = SettledReturn(
        information_cutoff_at=cutoff - timedelta(days=6),
        outcome_available_at=cutoff - timedelta(days=3),
        return_bps=Decimal("-20"),
    )
    future = SettledReturn(
        information_cutoff_at=cutoff - timedelta(days=1),
        outcome_available_at=cutoff + timedelta(days=2),
        return_bps=Decimal("10000"),
    )
    boundaries = (Decimal("-10"), Decimal("0"), Decimal("10"), Decimal("20"))

    probabilities, count = expanding_prior(
        (visible, future),
        boundaries_bps=boundaries,
        information_cutoff_at=cutoff,
    )

    assert count == 1
    assert probabilities == (Decimal("1"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"))


def test_forecast_baseline_is_deterministic_non_overlapping_and_round_trips(tmp_path) -> None:
    dataset = _dataset()
    dataset_root = tmp_path / "datasets"
    HistoricalDatasetCatalog(dataset_root).store(dataset)
    outcomes = build_non_overlapping_returns(
        dataset,
        horizon_days=3,
        phase_epoch=date(1970, 1, 1),
    )
    assert all(
        previous.outcome_available_at == current.information_cutoff_at
        for previous, current in pairwise(outcomes)
    )

    arguments = {
        "dataset_catalog": dataset_root,
        "plan_commit": "a" * 40,
        "evaluator_code_version": "b" * 40,
        "evaluated_at": datetime(2026, 8, 29, tzinfo=UTC),
    }
    first = evaluate_forecast_baseline(_plan(dataset), **arguments)
    second = evaluate_forecast_baseline(_plan(dataset), **arguments)

    assert first == second
    assert first.status == "VALID"
    assert first.capital_change == "NONE"
    assert first.results[0].validation_sample_count > 0
    assert sum(first.results[0].mean_rolling_probabilities) == 1
    assert sum(first.results[0].terminal_probabilities) == 1
    assert (
        sum(first.results[0].terminal_bucket_counts)
        == first.results[0].terminal_history_count
    )
    assert (
        first.results[0].terminal_history_count
        >= first.results[0].maximum_visible_history_count
    )
    target = store_forecast_baseline(first, root=tmp_path / "results")
    assert load_forecast_baseline(target) == first
    assert ForecastBaselineArtifact.model_validate_json(target.read_text()) == first
