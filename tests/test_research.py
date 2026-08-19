from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from quant_core.ids import stable_id
from quant_core.market_data import ClosedMarketBar
from quant_core.research.dataset import (
    HistoricalDataset,
    HistoricalDatasetCatalog,
    HistoricalDatasetManifest,
    InstrumentSpec,
    _bars_hash,
    fetch_binance_history,
)


def _instrument() -> InstrumentSpec:
    return InstrumentSpec(
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        price_increment=Decimal("0.01"),
        quantity_increment=Decimal("0.000001"),
        minimum_quantity=Decimal("0.000001"),
        maximum_quantity=Decimal("9000"),
        minimum_notional=Decimal("10"),
        minimum_price=Decimal("0.01"),
        maximum_price=Decimal("1000000"),
    )


def _dataset(
    *,
    count: int = 500,
    price_step: Decimal = Decimal("1.0002"),
    price_steps: tuple[Decimal, ...] | None = None,
    interval: str = "5m",
    bar_delta: timedelta = timedelta(minutes=5),
    initial_price: Decimal = Decimal("10000"),
    instrument: InstrumentSpec | None = None,
) -> HistoricalDataset:
    if price_steps is not None and len(price_steps) != count:
        raise ValueError("price_steps 必须与 count 一致")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + bar_delta * count
    bars: list[ClosedMarketBar] = []
    spec = instrument or _instrument()
    price = initial_price
    for index in range(count):
        open_price = price
        close_price = open_price * (
            price_steps[index] if price_steps is not None else price_step
        )
        price = close_price
        open_time = start + bar_delta * index
        close_time = open_time + bar_delta - timedelta(milliseconds=1)
        bars.append(
            ClosedMarketBar(
                symbol=spec.symbol,
                interval=interval,
                open_time=open_time,
                close_time=close_time,
                observed_at=close_time,
                open=open_price,
                high=max(open_price, close_price) * Decimal("1.0001"),
                low=min(open_price, close_price) * Decimal("0.9999"),
                close=close_price,
                volume=Decimal("10"),
                source="test-history",
            )
        )
    bars_hash = _bars_hash(bars)
    dataset_id = stable_id(
        "historical_dataset",
        "historical-bars-v1",
        "test-history",
        spec.symbol,
        interval,
        start,
        end,
        bars_hash,
        spec,
    )
    manifest = HistoricalDatasetManifest(
        dataset_id=dataset_id,
        symbol=spec.symbol,
        interval=interval,
        source="test-history",
        collected_at=end,
        requested_start=start,
        requested_end=end,
        first_open_time=bars[0].open_time,
        last_close_time=bars[-1].close_time,
        bar_count=len(bars),
        bars_hash=bars_hash,
        instrument=spec,
    )
    return HistoricalDataset(manifest=manifest, bars=tuple(bars))


def test_historical_catalog_round_trip_and_rejects_tampering(tmp_path) -> None:
    dataset = _dataset(count=10)
    catalog = HistoricalDatasetCatalog(tmp_path)
    target = catalog.store(dataset)
    assert catalog.load(dataset.manifest.dataset_id) == dataset

    rows = json.loads((target / "bars.json").read_text())
    rows[0][4] = "9999"
    (target / "bars.json").write_text(json.dumps(rows))
    with pytest.raises(ValueError, match="内容哈希"):
        catalog.load(dataset.manifest.dataset_id)


def test_historical_dataset_rejects_bar_gap() -> None:
    dataset = _dataset(count=10)
    bars = list(dataset.bars)
    bars.pop(4)
    bars_hash = _bars_hash(bars)
    manifest = HistoricalDatasetManifest(
        dataset_id=stable_id(
            "historical_dataset",
            dataset.manifest.schema_version,
            dataset.manifest.source,
            dataset.manifest.symbol,
            dataset.manifest.interval,
            dataset.manifest.requested_start,
            dataset.manifest.requested_end,
            bars_hash,
            dataset.manifest.instrument,
        ),
        **dataset.manifest.model_dump(
            exclude={"dataset_id", "bar_count", "bars_hash"}
        ),
        bar_count=len(bars),
        bars_hash=bars_hash,
    )
    with pytest.raises(ValueError, match="存在缺口"):
        HistoricalDataset(manifest=manifest, bars=tuple(bars))


def test_fetch_binance_history_paginates_and_freezes_instrument() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(minutes=10)
    first_ms = int(start.timestamp() * 1000)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path == "/api/v3/exchangeInfo":
            return httpx.Response(
                200,
                json={
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "baseAsset": "BTC",
                            "quoteAsset": "USDT",
                            "filters": [
                                {
                                    "filterType": "PRICE_FILTER",
                                    "tickSize": "0.01",
                                    "minPrice": "0.01",
                                    "maxPrice": "1000000",
                                },
                                {
                                    "filterType": "LOT_SIZE",
                                    "stepSize": "0.000001",
                                    "minQty": "0.000001",
                                    "maxQty": "9000",
                                },
                                {"filterType": "MIN_NOTIONAL", "minNotional": "10"},
                            ],
                        }
                    ]
                },
            )
        calls += 1
        if calls > 1:
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json=[
                [first_ms, "100", "102", "99", "101", "5", first_ms + 299_999],
                [
                    first_ms + 300_000,
                    "101",
                    "103",
                    "100",
                    "102",
                    "6",
                    first_ms + 599_999,
                ],
            ],
        )

    dataset = asyncio.run(
        fetch_binance_history(
            base_url="https://api.binance.com",
            symbol="BTCUSDT",
            interval="5m",
            start=start,
            end=end,
            timeout_seconds=1,
            clock=lambda: end + timedelta(days=1),
            transport=httpx.MockTransport(handler),
        )
    )
    assert dataset.manifest.bar_count == 2
    assert dataset.manifest.instrument.quantity_increment == Decimal("0.000001")
    assert dataset.bars[0].observed_at == dataset.bars[0].close_time
    assert calls == 1


def test_nautilus_backtest_enters_only_after_signal_and_deducts_frozen_costs(
    app_config,
) -> None:
    pytest.importorskip("nautilus_trader")
    from quant_core.research.backtest import run_bar_backtest

    dataset = _dataset(count=120)
    run = run_bar_backtest(
        dataset=dataset,
        config=app_config,
        signal_start=dataset.bars[63].close_time,
        signal_end=dataset.bars[-1].open_time - timedelta(minutes=65),
    )
    assert run.completed
    assert run.trades
    assert all(item.opened_at > item.signal_at for item in run.trades)
    assert run.trades[0].opened_at - run.trades[0].signal_at == timedelta(milliseconds=1)
    assert all(item.modeled_cost > 0 for item in run.trades)
    assert all(item.net_pnl < item.gross_pnl for item in run.trades)
    assert run.metrics.gross_pnl == sum(
        (item.gross_pnl for item in run.trades), Decimal("0")
    )
    assert run.metrics.modeled_cost == sum(
        (item.modeled_cost for item in run.trades), Decimal("0")
    )
    assert run.metrics.gross_pnl - run.metrics.modeled_cost == run.metrics.net_pnl
    assert run.metrics.average_gross_return_bps == sum(
        (item.gross_return_bps for item in run.trades), Decimal("0")
    ) / len(run.trades)
    assert (
        run.metrics.average_modeled_cost_bps
        == run.metrics.average_gross_return_bps - run.metrics.average_net_return_bps
    )
    # 每 5 分钟上涨 2 bps，60 分钟毛收益仍不足以覆盖当前完整往返成本。
    assert run.metrics.net_pnl < 0


def test_backtest_metrics_keep_legacy_artifacts_readable_and_validate_costs() -> None:
    from quant_core.research.backtest import BacktestMetrics

    legacy = BacktestMetrics(
        starting_equity=Decimal("10000"),
        ending_equity=Decimal("9990"),
        net_pnl=Decimal("-10"),
        return_fraction=Decimal("-0.001"),
        trade_count=1,
        maximum_drawdown_fraction=Decimal("0.001"),
        benchmark_buy_hold_bps=Decimal("0"),
    )
    assert legacy.gross_pnl is None
    assert legacy.modeled_cost is None

    with pytest.raises(ValueError, match="净收益必须等于"):
        BacktestMetrics.model_validate(
            {
                **legacy.model_dump(),
                "gross_pnl": Decimal("5"),
                "modeled_cost": Decimal("5"),
            }
        )


def test_nautilus_backtest_protects_final_quantity_after_partial_entry_fill(
    app_config,
) -> None:
    pytest.importorskip("nautilus_trader")
    from quant_core.research.backtest import run_bar_backtest

    instrument = InstrumentSpec(
        symbol="ETHUSDT",
        base_asset="ETH",
        quote_asset="USDT",
        price_increment=Decimal("0.01"),
        quantity_increment=Decimal("0.0001"),
        minimum_quantity=Decimal("0.0001"),
        maximum_quantity=Decimal("9000"),
        minimum_notional=Decimal("5"),
        minimum_price=Decimal("0.01"),
        maximum_price=Decimal("1000000"),
    )
    steps = (Decimal("1.0002"),) * 64 + (Decimal("0.99"),) + (
        Decimal("1"),
    ) * 25
    dataset = _dataset(
        count=90,
        price_steps=steps,
        initial_price=Decimal("1973"),
        instrument=instrument,
    )
    run = run_bar_backtest(
        dataset=dataset,
        config=app_config,
        signal_start=dataset.bars[63].close_time,
        signal_end=dataset.bars[64].close_time,
        replay_end=dataset.bars[-1].close_time + timedelta(microseconds=1),
    )

    assert run.completed
    assert len(run.trades) == 1
    # 合成 QuoteTick 深度为 1 ETH，最终成交量略大于 1，强制覆盖部分成交路径。
    assert run.trades[0].quantity > Decimal("1")
    assert run.trades[0].exit_reason == "STOP_LOSS"
    assert not run.order_failure_reasons
    assert not run.terminal_candidate_ids


def test_forward_decision_tape_replays_baseline_and_ai_gate_with_one_matcher(
    app_config,
) -> None:
    pytest.importorskip("nautilus_trader")
    from quant_core.domain import (
        Action,
        AnalysisProposal,
        DirectionalForecast,
        DirectionalView,
    )
    from quant_core.ids import content_hash
    from quant_core.research.decision_tape import (
        ForecastDecisionTape,
        ForecastGatePolicy,
        ForecastTapeEntry,
        run_paired_decision_tape_backtest,
    )
    from quant_core.strategy import PriceTrendStrategy

    dataset = _dataset(count=140)
    signal_start = dataset.bars[63].close_time
    signal_end = dataset.bars[-1].open_time - timedelta(minutes=65)
    forecast_times = tuple(
        dataset.bars[index].close_time
        for index in range(63, 125, 10)
        if dataset.bars[index].close_time < signal_end
    )
    midpoint = forecast_times[len(forecast_times) // 2]
    entries = []
    for index, available_at in enumerate(forecast_times):
        view = (
            DirectionalView.UP
            if available_at < midpoint
            else DirectionalView.DOWN
        )
        forecast = DirectionalForecast(
            horizon_minutes=60,
            directional_view=view,
            confidence=Decimal("0.70"),
        )
        proposal = AnalysisProposal(
            proposal_id=f"forward-proposal-{index}",
            suggested_action=Action.NO_ACTION,
            symbol="BTCUSDT",
            thesis="结果发生前冻结的研究预测",
            confidence=Decimal("0.70"),
            forecasts=(forecast,),
        )
        entries.append(
            ForecastTapeEntry.freeze(
                proposal=proposal,
                forecast=forecast,
                cycle_id=f"forward-cycle-{index}",
                pipeline_version="forward-pipeline-v1",
                source_run_id=f"forward-run-{index}",
                available_at=available_at,
            )
        )
    tape_payload = {
        "version": "forecast-decision-tape-v1",
        "pipeline_version": "forward-pipeline-v1",
        "symbol": "BTCUSDT",
        "window_start": signal_start,
        "window_end": signal_end,
        "entries": tuple(entries),
        "exclusions": (),
    }
    tape_hash = content_hash(tape_payload)
    tape = ForecastDecisionTape(
        tape_id=stable_id("forecast_decision_tape", tape_hash),
        content_hash=tape_hash,
        **tape_payload,
    )
    policy = ForecastGatePolicy(
        plan_id="pre-registered-forward-gate-v1",
        registered_at=signal_start,
        horizon_minutes=60,
        maximum_age_minutes=60,
        minimum_confidence=Decimal("0.60"),
    )

    result = run_paired_decision_tape_backtest(
        dataset=dataset,
        config=app_config,
        tape=tape,
        policy=policy,
        strategy=PriceTrendStrategy(app_config.strategy),
        signal_start=signal_start,
        signal_end=signal_end,
    )

    assert result.baseline.engine == result.gated.engine == "nautilus-trader"
    assert result.baseline.dataset_id == result.gated.dataset_id
    assert result.gated.metrics.trade_count < result.baseline.metrics.trade_count
    assert result.trade_count_change == (
        result.gated.metrics.trade_count - result.baseline.metrics.trade_count
    )
    assert result.incremental_net_pnl == (
        result.gated.metrics.net_pnl - result.baseline.metrics.net_pnl
    )
    assert "NO_AI_OUTPUT_REGENERATION" in result.limitations

    late_policy = policy.model_copy(
        update={"registered_at": signal_start + timedelta(minutes=1)}
    )
    with pytest.raises(ValueError, match="预登记时间"):
        run_paired_decision_tape_backtest(
            dataset=dataset,
            config=app_config,
            tape=tape,
            policy=late_policy,
            strategy=PriceTrendStrategy(app_config.strategy),
            signal_start=signal_start,
            signal_end=signal_end,
        )


def test_walk_forward_uses_non_overlapping_test_windows_with_automatic_separation(
    app_config,
) -> None:
    pytest.importorskip("nautilus_trader")
    from quant_core.research.walk_forward import WalkForwardPlan, run_walk_forward

    result = run_walk_forward(
        dataset=_dataset(),
        config=app_config,
        plan=WalkForwardPlan(
            plan_id="walk-forward-test-v1",
            training_bars=128,
            test_bars=100,
            blind_bars=50,
        ),
    )
    assert result.completed
    assert not result.passed
    assert "NET_PNL_NOT_POSITIVE" in result.reason_codes
    assert "NET_RETURN_LOWER_CONFIDENCE_BOUND_NOT_POSITIVE" in result.reason_codes
    assert len(result.folds) == 2
    assert result.embargo_bars == 13
    assert result.purge_bars == 14
    assert result.blind_bar_count == 50
    assert result.blind_start == _dataset().bars[-50].open_time
    assert result.blind_end == _dataset().bars[-1].close_time
    assert result.strategy_spec_snapshot is not None
    assert result.strategy_spec_snapshot["version"] == app_config.strategy.version
    assert result.metrics.gross_pnl == sum(
        (trade.gross_pnl for fold in result.folds for trade in fold.run.trades),
        Decimal("0"),
    )
    assert result.metrics.modeled_cost == sum(
        (trade.modeled_cost for fold in result.folds for trade in fold.run.trades),
        Decimal("0"),
    )
    assert (
        result.metrics.gross_pnl - result.metrics.modeled_cost
        == result.metrics.net_pnl
    )
    assert result.metrics.average_gross_return_bps is not None
    assert result.metrics.average_modeled_cost_bps is not None
    assert abs(
        result.metrics.average_gross_return_bps
        - result.metrics.average_modeled_cost_bps
        - result.metrics.average_net_return_bps
    ) < Decimal("1e-24")
    assert result.folds[-1].test_end < result.blind_start
    assert result.folds[0].test_end < result.folds[1].test_start
    assert all(item.run.completed for item in result.folds)


def test_custom_research_strategy_identity_changes_artifact(app_config) -> None:
    from quant_core.research.backtest import artifact_hash
    from quant_core.research.candidates import LongOnlyTimeSeriesMomentumSpec

    first = LongOnlyTimeSeriesMomentumSpec(version="long-only-tsmom-test-v1")
    second = first.model_copy(update={"version": "long-only-tsmom-test-v2"})

    assert artifact_hash(app_config, strategy_spec=first) != artifact_hash(
        app_config, strategy_spec=second
    )


def test_long_only_time_series_momentum_is_point_in_time_and_long_cash(
    app_config, replay_input
) -> None:
    from quant_core.features import FeatureEngine
    from quant_core.research.candidates import (
        LongOnlyTimeSeriesMomentumSpec,
        LongOnlyTimeSeriesMomentumStrategy,
    )

    strategy = LongOnlyTimeSeriesMomentumStrategy(
        LongOnlyTimeSeriesMomentumSpec(
            version="long-only-tsmom-test-v1",
            lookback_bars=5,
            atr_bars=3,
            horizon_minutes=1440,
        )
    )
    features = FeatureEngine(app_config.feature).compute(replay_input.market)
    candidates = strategy.evaluate(
        market=replay_input.market,
        account=replay_input.account,
        features=features,
    )

    assert len(candidates) == 1
    assert candidates[0].producer_version == "long-only-tsmom-test-v1"
    assert candidates[0].signal_observed_at == replay_input.market.as_of
    assert candidates[0].side.value == "BUY"
    assert candidates[0].stop_price < replay_input.market.ask


def test_long_only_moving_average_is_point_in_time_and_long_cash(
    app_config, replay_input
) -> None:
    from quant_core.features import FeatureEngine
    from quant_core.research.candidates import (
        LongOnlyMovingAverageSpec,
        LongOnlyMovingAverageStrategy,
        resolve_research_candidate,
    )

    strategy = LongOnlyMovingAverageStrategy(
        LongOnlyMovingAverageSpec(
            version="long-only-sma-test-v1",
            moving_average_bars=5,
            atr_bars=3,
            horizon_minutes=1440,
        )
    )
    features = FeatureEngine(app_config.feature).compute(replay_input.market)
    candidates = strategy.evaluate(
        market=replay_input.market,
        account=replay_input.account,
        features=features,
    )
    effective, resolved = resolve_research_candidate(
        "long-only-sma100-2w-v1", app_config
    )
    monthly_effective, monthly = resolve_research_candidate(
        "long-only-sma200-1m-v1", app_config
    )

    assert len(candidates) == 1
    assert candidates[0].producer_version == "long-only-sma-test-v1"
    assert candidates[0].signal_observed_at == replay_input.market.as_of
    assert candidates[0].side.value == "BUY"
    assert effective.market_data.interval == "1d"
    assert effective.market_data.bar_window == 100
    assert resolved.research_spec.version == "long-only-sma100-2w-v1"
    assert monthly_effective.market_data.bar_window == 200
    assert monthly.research_spec.version == "long-only-sma200-1m-v1"
    assert monthly.research_spec.horizon_minutes == 43_200


def test_program_exit_uses_same_rule_in_nautilus_replay(app_config) -> None:
    pytest.importorskip("nautilus_trader")
    from quant_core.research.backtest import run_bar_backtest
    from quant_core.research.candidates import (
        LongOnlyMovingAverageSpec,
        LongOnlyMovingAverageStrategy,
    )

    dataset = _dataset(
        count=40,
        price_steps=(Decimal("1.002"),) * 20 + (Decimal("0.998"),) * 20,
        interval="1d",
        bar_delta=timedelta(days=1),
    )
    spec = LongOnlyMovingAverageSpec(
        version="long-only-sma5-riskoff-test-v1",
        moving_average_bars=5,
        atr_bars=3,
        stop_atr_multiple=Decimal("10"),
        horizon_minutes=90 * 1_440,
        cooldown_minutes=7 * 1_440,
        program_exit_moving_average_bars=5,
    )
    effective = app_config.model_copy(
        update={
            "market_data": app_config.market_data.model_copy(
                update={"interval": "1d", "bar_window": spec.required_bar_window}
            ),
            "feature": app_config.feature.model_copy(
                update={"volatility_window": spec.atr_bars}
            ),
            "frequency": app_config.frequency.model_copy(
                update={"cooldown_minutes": spec.cooldown_minutes}
            ),
        }
    )
    run = run_bar_backtest(
        dataset=dataset,
        config=effective,
        strategy=LongOnlyMovingAverageStrategy(spec),
        signal_start=dataset.bars[5].close_time,
        signal_end=dataset.bars[-5].close_time,
        replay_start=dataset.bars[0].open_time,
        replay_end=dataset.bars[-1].close_time + timedelta(microseconds=1),
    )

    assert run.completed
    assert run.trades
    assert run.trades[0].opened_at > run.trades[0].signal_at
    assert run.trades[0].exit_reason == "PROGRAM_SIGNAL"
    assert "PROGRAM_EXIT_EVALUATED_FROM_MATCHED_CLOSED_BARS" in run.assumptions
    assert "DRAWDOWN_MARKED_TO_EACH_BAR_CLOSE" in run.assumptions


def test_daily_candidate_uses_native_daily_bar_type(app_config) -> None:
    pytest.importorskip("nautilus_trader")
    from quant_core.research.backtest import run_bar_backtest
    from quant_core.research.candidates import resolve_research_candidate

    dataset = _dataset(count=450, interval="1d", bar_delta=timedelta(days=1))
    effective, strategy = resolve_research_candidate(
        "long-only-tsmom-12m-v1", app_config
    )
    run = run_bar_backtest(
        dataset=dataset,
        config=effective,
        strategy=strategy,
        signal_start=dataset.bars[365].close_time,
        signal_end=dataset.bars[-34].close_time,
        replay_start=dataset.bars[0].open_time,
        replay_end=dataset.bars[-1].close_time + timedelta(microseconds=1),
    )

    assert run.completed
    assert run.trades
    assert run.interval == "1d"


def test_hourly_candidate_uses_native_hour_bar_type(app_config) -> None:
    pytest.importorskip("nautilus_trader")
    from quant_core.research.backtest import run_bar_backtest
    from quant_core.research.candidates import (
        LongOnlyMovingAverageSpec,
        LongOnlyMovingAverageStrategy,
    )

    dataset = _dataset(
        count=200,
        interval="4h",
        bar_delta=timedelta(hours=4),
    )
    spec = LongOnlyMovingAverageSpec(
        version="long-only-sma20-4h-test-v1",
        interval="4h",
        moving_average_bars=20,
        atr_bars=10,
        horizon_minutes=7 * 1_440,
        cooldown_minutes=7 * 1_440,
        signal_validity_minutes=240,
    )
    effective = app_config.model_copy(
        update={
            "market_data": app_config.market_data.model_copy(
                update={"interval": "4h", "bar_window": spec.required_bar_window}
            ),
            "feature": app_config.feature.model_copy(
                update={"volatility_window": spec.atr_bars}
            ),
            "frequency": app_config.frequency.model_copy(
                update={"cooldown_minutes": spec.cooldown_minutes}
            ),
        }
    )
    run = run_bar_backtest(
        dataset=dataset,
        config=effective,
        strategy=LongOnlyMovingAverageStrategy(spec),
        signal_start=dataset.bars[20].close_time,
        signal_end=dataset.bars[-45].close_time,
        replay_start=dataset.bars[0].open_time,
        replay_end=dataset.bars[-1].close_time + timedelta(microseconds=1),
    )

    assert run.completed
    assert run.trades
    assert run.interval == "4h"


def test_evaluation_catalog_round_trip_and_rejects_tampering(
    app_config, tmp_path
) -> None:
    pytest.importorskip("nautilus_trader")
    from quant_core.research.evaluation_catalog import HistoricalEvaluationCatalog
    from quant_core.research.walk_forward import WalkForwardPlan, run_walk_forward

    result = run_walk_forward(
        dataset=_dataset(),
        config=app_config,
        plan=WalkForwardPlan(
            plan_id="catalog-test-v1",
            training_bars=128,
            test_bars=100,
            blind_bars=50,
        ),
    )
    catalog = HistoricalEvaluationCatalog(tmp_path)
    target = catalog.store(result)
    assert catalog.load(result.evaluation_id) == result

    envelope = json.loads(target.read_text(encoding="utf-8"))
    envelope["result"]["metrics"]["net_pnl"] = "999"
    target.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(ValueError, match="内容哈希"):
        catalog.load(result.evaluation_id)


def test_evaluation_catalog_derives_canonical_semantics_and_rejects_ambiguity(
    app_config, tmp_path
) -> None:
    pytest.importorskip("nautilus_trader")
    from quant_core.research.evaluation_catalog import HistoricalEvaluationCatalog
    from quant_core.research.walk_forward import WalkForwardPlan, run_walk_forward

    result = run_walk_forward(
        dataset=_dataset(),
        config=app_config,
        plan=WalkForwardPlan(
            plan_id="catalog-lineage-test-v1",
            training_bars=128,
            test_bars=100,
            blind_bars=50,
        ),
    )
    catalog = HistoricalEvaluationCatalog(tmp_path)
    catalog.store(result)

    def revised(version: str, suffix: str):
        folds = tuple(
            fold.model_copy(
                update={
                    "run": fold.run.model_copy(
                        update={
                            "run_id": stable_id("catalog-run", suffix, fold.fold_id),
                            "backtest_model_version": version,
                        }
                    )
                }
            )
            for fold in result.folds
        )
        return result.model_copy(
            update={
                "evaluation_id": stable_id("catalog-evaluation", suffix),
                "folds": folds,
            }
        )

    newer = revised("quant-core-bar-backtest-v99", "newer")
    catalog.store(newer)
    summary = catalog.summaries()[0]
    assert summary.attempt_count == 2
    assert summary.canonical_evaluation_id == newer.evaluation_id
    assert summary.superseded_evaluation_ids == (result.evaluation_id,)
    assert not summary.ambiguity_reasons

    duplicate = revised("quant-core-bar-backtest-v99", "duplicate")
    catalog.store(duplicate)
    ambiguous = catalog.summaries()[0]
    assert ambiguous.attempt_count == 3
    assert ambiguous.canonical_evaluation_id is None
    assert ambiguous.ambiguity_reasons == ("DUPLICATE_TOP_SEMANTICS",)
