from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from quant_core.calibration import EDGE_CALIBRATION_MISSING, uncalibrated_ref
from quant_core.domain import (
    AccountSnapshot,
    Action,
    FeatureSnapshot,
    FrozenModel,
    IntelligenceEvent,
    MarketSnapshot,
    OrderType,
    PriceCondition,
    ProgramExitCondition,
    Side,
    SignalCandidate,
)
from quant_core.ids import content_hash, stable_id
from quant_core.market_data import ClosedMarketBar
from quant_core.research.dataset import (
    HistoricalDataset,
    HistoricalDatasetCatalog,
    HistoricalDatasetManifest,
    HistoricalEventDatasetCatalog,
    InstrumentSpec,
    _bars_hash,
    fetch_binance_history,
    freeze_historical_events,
)


class _TestResearchSpec(FrozenModel):
    strategy_id: str = "test-long"
    version: str = "test-long-v1"
    family: str = "test-only"
    horizon_minutes: int = 1_440
    signal_validity_minutes: int = 1_440
    program_exit_bar_interval_minutes: int | None = None
    program_exit_moving_average_bars: int | None = None


class _TestLongStrategy:
    """Small test fixture for replay mechanics; it is not a research candidate."""

    def __init__(self, spec: _TestResearchSpec | None = None) -> None:
        self._spec = spec or _TestResearchSpec()

    @property
    def research_spec(self) -> _TestResearchSpec:
        return self._spec

    def evaluate(
        self,
        *,
        market: MarketSnapshot,
        account: AccountSnapshot,
        features: FeatureSnapshot,
        events: tuple[IntelligenceEvent, ...] = (),
    ) -> tuple[SignalCandidate, ...]:
        if any(
            position.symbol == market.symbol and position.quantity > 0
            for position in account.positions
        ):
            return ()
        spec = self._spec
        program_exit = None
        if (
            spec.program_exit_bar_interval_minutes is not None
            and spec.program_exit_moving_average_bars is not None
        ):
            program_exit = ProgramExitCondition(
                version=f"{spec.version}-exit-v1",
                bar_interval_minutes=spec.program_exit_bar_interval_minutes,
                moving_average_bars=spec.program_exit_moving_average_bars,
            )
        return (
            SignalCandidate(
                candidate_id=stable_id(
                    "sig",
                    market.cycle_id,
                    spec.strategy_id,
                    spec.version,
                    market.symbol,
                ),
                cycle_id=market.cycle_id,
                producer_id=spec.strategy_id,
                producer_version=spec.version,
                strategy_family=spec.family,
                symbol=market.symbol,
                action=Action.OPEN,
                side=Side.BUY,
                horizon_minutes=spec.horizon_minutes,
                feature_refs=(features.feature_set_version,),
                entry=PriceCondition(order_type=OrderType.MARKET),
                stop_price=market.ask * Decimal("0.9"),
                valid_until=market.as_of
                + timedelta(minutes=spec.signal_validity_minutes),
                signal_observed_at=market.as_of,
                reference_price=market.ask,
                expected_edge_half_life_seconds=900,
                raw_score=Decimal("1"),
                expected_gross_bps=Decimal("0"),
                calibration_ref=uncalibrated_ref(spec.version),
                program_exit=program_exit,
                unknowns=(EDGE_CALIBRATION_MISSING,),
            ),
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


def _event(*, observed_at: datetime, evidence_id: str = "event-1") -> IntelligenceEvent:
    return IntelligenceEvent(
        evidence_id=evidence_id,
        normalizer_version="test-normalizer-v1",
        acquisition_route="test-archive-v1",
        event_time=observed_at - timedelta(seconds=30),
        observed_at=observed_at,
        source="test-source",
        title=f"point-in-time {evidence_id}",
        body="historical event body",
        symbols=("BTCUSDT",),
        relevance=Decimal("1"),
        impact=Decimal("0.8"),
        source_reliability=Decimal("0.7"),
        novelty=Decimal("1"),
    )


def test_historical_event_catalog_round_trip_and_rejects_tampering(tmp_path) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    dataset = freeze_historical_events(
        events=(
            _event(observed_at=start + timedelta(hours=2), evidence_id="event-2"),
            _event(observed_at=start + timedelta(hours=1), evidence_id="event-1"),
        ),
        source="test-archive",
        requested_start=start,
        requested_end=end,
        collected_at=end,
    )
    catalog = HistoricalEventDatasetCatalog(tmp_path)
    target = catalog.store(dataset)

    assert catalog.load(dataset.manifest.dataset_id) == dataset
    assert tuple(item.evidence_id for item in dataset.events) == ("event-1", "event-2")
    repeated = freeze_historical_events(
        events=dataset.events,
        source="test-archive",
        requested_start=start,
        requested_end=end,
        collected_at=end + timedelta(hours=1),
    )
    assert catalog.store(repeated) == target

    rows = json.loads((target / "events.json").read_text())
    rows[0]["body"] = "tampered"
    (target / "events.json").write_text(json.dumps(rows))
    with pytest.raises(ValueError, match="内容哈希"):
        catalog.load(dataset.manifest.dataset_id)


def test_historical_event_freeze_accepts_empty_window_as_observed_fact() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    dataset = freeze_historical_events(
        events=(),
        source="test-archive",
        requested_start=start,
        requested_end=start + timedelta(hours=1),
        collected_at=start + timedelta(hours=1),
    )

    assert dataset.manifest.event_count == 0
    assert dataset.manifest.first_observed_at is None

    with pytest.raises(ValueError, match="终点不能晚于"):
        freeze_historical_events(
            events=(),
            source="test-archive",
            requested_start=start,
            requested_end=start + timedelta(hours=2),
            collected_at=start + timedelta(hours=1),
        )


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


def test_backtest_exposes_events_only_after_frozen_observed_at(app_config) -> None:
    pytest.importorskip("nautilus_trader")
    from quant_core.research.backtest import run_bar_backtest

    dataset = _dataset(count=100)
    early_at = dataset.bars[66].close_time
    late_at = dataset.bars[70].close_time
    event_dataset = freeze_historical_events(
        events=(
            _event(observed_at=early_at, evidence_id="early"),
            _event(observed_at=late_at, evidence_id="late"),
        ),
        source="test-archive",
        requested_start=dataset.bars[0].open_time,
        requested_end=dataset.bars[-1].close_time + timedelta(microseconds=1),
        collected_at=dataset.bars[-1].close_time + timedelta(seconds=1),
    )

    class RecordingStrategy:
        research_spec = app_config.strategy

        def __init__(self) -> None:
            self.views: list[tuple[datetime, tuple[str, ...]]] = []

        def evaluate(self, *, market, account, features, events=()):
            self.views.append(
                (market.as_of, tuple(item.evidence_id for item in events))
            )
            return ()

    strategy = RecordingStrategy()
    run = run_bar_backtest(
        dataset=dataset,
        event_dataset=event_dataset,
        config=app_config,
        strategy=strategy,
        signal_start=dataset.bars[63].close_time,
        signal_end=dataset.bars[75].close_time,
    )

    views = dict(strategy.views)
    assert views[dataset.bars[65].close_time] == ()
    assert views[early_at] == ("early",)
    assert views[dataset.bars[69].close_time] == ("early",)
    assert views[late_at] == ("early", "late")
    assert run.event_dataset_id == event_dataset.manifest.dataset_id
    assert "EVENTS_VISIBLE_ONLY_AFTER_OBSERVED_AT" in run.assumptions
    assert "EVENT_STRATEGY_EVALUATED_AT_BAR_CLOSE" in run.assumptions

    partial_events = freeze_historical_events(
        events=(),
        source="test-archive",
        requested_start=dataset.bars[10].open_time,
        requested_end=dataset.bars[-1].close_time + timedelta(microseconds=1),
        collected_at=dataset.bars[-1].close_time + timedelta(seconds=1),
    )
    with pytest.raises(ValueError, match="覆盖完整回放窗口"):
        run_bar_backtest(
            dataset=dataset,
            event_dataset=partial_events,
            config=app_config,
            strategy=RecordingStrategy(),
            signal_start=dataset.bars[63].close_time,
            signal_end=dataset.bars[75].close_time,
        )


def test_backtest_event_visibility_matches_production_latest_100_bound(app_config) -> None:
    pytest.importorskip("nautilus_trader")
    from quant_core.research.backtest import run_bar_backtest

    dataset = _dataset(count=80)
    observed_at = dataset.bars[63].close_time
    event_dataset = freeze_historical_events(
        events=tuple(
            _event(observed_at=observed_at, evidence_id=f"event-{index:03d}")
            for index in range(105)
        ),
        source="test-archive",
        requested_start=dataset.bars[0].open_time,
        requested_end=dataset.bars[-1].close_time + timedelta(microseconds=1),
        collected_at=dataset.bars[-1].close_time + timedelta(seconds=1),
    )

    class RecordingStrategy:
        research_spec = app_config.strategy

        def __init__(self) -> None:
            self.first_view: tuple[str, ...] | None = None

        def evaluate(self, *, market, account, features, events=()):
            if self.first_view is None:
                self.first_view = tuple(item.evidence_id for item in events)
            return ()

    strategy = RecordingStrategy()
    run_bar_backtest(
        dataset=dataset,
        event_dataset=event_dataset,
        config=app_config,
        strategy=strategy,
        signal_start=observed_at,
        signal_end=dataset.bars[66].close_time,
    )

    assert strategy.first_view is not None
    assert len(strategy.first_view) == 100
    assert strategy.first_view[0] == "event-005"
    assert strategy.first_view[-1] == "event-104"


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
        ForecastGateEvaluationSpec,
        ForecastGatePolicy,
        ForecastTapeEntry,
        build_forecast_gate_evaluation_plan,
        run_paired_decision_tape_backtest,
        validate_forecast_gate_evaluation_plan,
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
        evaluation_end=signal_end,
        horizon_minutes=60,
        maximum_age_minutes=60,
        minimum_confidence=Decimal("0.60"),
    )
    strategy = PriceTrendStrategy(app_config.strategy)
    source_blind_evaluation_id = "passed-blind-baseline-v1"
    source_blind_evaluation_hash = "a" * 64
    registered_spec = ForecastGateEvaluationSpec.freeze(
        strategy=strategy,
        config=app_config,
        symbol="BTCUSDT",
        pipeline_version=app_config.pipeline.version,
        starting_equity=Decimal("10000"),
        spread_bps=Decimal("1"),
        maximum_completion_lag_seconds=120,
        policy=policy,
        source_blind_evaluation_id=source_blind_evaluation_id,
        source_blind_evaluation_hash=source_blind_evaluation_hash,
    )
    with pytest.raises(ValueError, match="盲测基线"):
        ForecastGateEvaluationSpec.model_validate(
            registered_spec.model_dump(
                exclude={
                    "source_blind_evaluation_id",
                    "source_blind_evaluation_hash",
                }
            )
        )
    assert registered_spec.base_strategy_spec_hash == content_hash(
        strategy.research_spec
    )
    registered_hash = content_hash(registered_spec)
    registered_plan = build_forecast_gate_evaluation_plan(
        spec=registered_spec,
        base_manifest_id="champion-v1",
    )
    validate_forecast_gate_evaluation_plan(
        spec=registered_spec,
        plan=registered_plan,
        champion_manifest_id="champion-v1",
    )
    assert registered_plan.candidate_spec_snapshot == registered_spec.model_dump(
        mode="json"
    )
    for changed in (
        {"starting_equity": Decimal("10001")},
        {"spread_bps": Decimal("2")},
        {"maximum_completion_lag_seconds": 121},
    ):
        assert content_hash(registered_spec.model_copy(update=changed)) != registered_hash
    changed_panel_config = app_config.model_copy(
        update={
            "panel": app_config.panel.model_copy(update={"version": "different-panel-v1"})
        }
    )
    assert content_hash(
        ForecastGateEvaluationSpec.freeze(
            strategy=strategy,
            config=changed_panel_config,
            symbol="BTCUSDT",
            pipeline_version=changed_panel_config.pipeline.version,
            starting_equity=Decimal("10000"),
            spread_bps=Decimal("1"),
            maximum_completion_lag_seconds=120,
            policy=policy,
            source_blind_evaluation_id=source_blind_evaluation_id,
            source_blind_evaluation_hash=source_blind_evaluation_hash,
        )
    ) != registered_hash
    assert content_hash(
        ForecastGateEvaluationSpec.freeze(
            strategy=PriceTrendStrategy(
                app_config.strategy.model_copy(update={"version": "different-q-v1"})
            ),
            config=app_config,
            symbol="BTCUSDT",
            pipeline_version=app_config.pipeline.version,
            starting_equity=Decimal("10000"),
            spread_bps=Decimal("1"),
            maximum_completion_lag_seconds=120,
            policy=policy,
            source_blind_evaluation_id=source_blind_evaluation_id,
            source_blind_evaluation_hash=source_blind_evaluation_hash,
        )
    ) != registered_hash
    assert content_hash(
        ForecastGateEvaluationSpec.freeze(
            strategy=strategy,
            config=app_config,
            symbol="BTCUSDT",
            pipeline_version=app_config.pipeline.version,
            starting_equity=Decimal("10000"),
            spread_bps=Decimal("1"),
            maximum_completion_lag_seconds=120,
            policy=policy.model_copy(update={"minimum_confidence": Decimal("0.61")}),
            source_blind_evaluation_id=source_blind_evaluation_id,
            source_blind_evaluation_hash=source_blind_evaluation_hash,
        )
    ) != registered_hash
    with pytest.raises(ValueError, match="Pipeline"):
        ForecastGateEvaluationSpec.freeze(
            strategy=strategy,
            config=app_config,
            symbol="BTCUSDT",
            pipeline_version="wrong-pipeline",
            starting_equity=Decimal("10000"),
            spread_bps=Decimal("1"),
            maximum_completion_lag_seconds=120,
            policy=policy,
            source_blind_evaluation_id=source_blind_evaluation_id,
            source_blind_evaluation_hash=source_blind_evaluation_hash,
        )

    result = run_paired_decision_tape_backtest(
        dataset=dataset,
        config=app_config,
        tape=tape,
        policy=policy,
        strategy=strategy,
        signal_start=signal_start,
        signal_end=signal_end,
        evaluation_spec_hash=registered_hash,
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
    assert policy.forecast_role == "INDEPENDENT_CONTEXT"
    assert policy.program_evaluation_clock == "BAR_CLOSE"
    assert "PRODUCTION_TRIGGER_CLOCK_NOT_REPLAYED" in result.limitations
    assert "HOSTED_MODEL_SNAPSHOT_NOT_AUDITABLE" in result.limitations
    assert "NO_AI_OUTPUT_REGENERATION" in result.limitations
    assert result.non_overlapping_forecast_count < len(forecast_times)
    assert not result.evidence_sufficient

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
            evaluation_spec_hash=registered_hash,
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


def test_blind_evaluation_replays_only_reserved_tail_after_source_passes(
    app_config, tmp_path
) -> None:
    pytest.importorskip("nautilus_trader")
    from quant_core.research.decision_tape import validate_forecast_gate_baseline
    from quant_core.research.evaluation_catalog import BlindEvaluationCatalog
    from quant_core.research.walk_forward import (
        WalkForwardPlan,
        run_blind_evaluation,
        run_walk_forward,
    )
    from quant_core.strategy import PriceTrendStrategy

    dataset = _dataset()
    source = run_walk_forward(
        dataset=dataset,
        config=app_config,
        plan=WalkForwardPlan(
            plan_id="walk-forward-blind-test-v1",
            training_bars=128,
            test_bars=100,
            blind_bars=50,
        ),
        evaluation_spec_hash="a" * 64,
    )
    with pytest.raises(ValueError, match="通过全部预登记门禁"):
        run_blind_evaluation(
            source=source,
            query_id="blind-query-1",
            dataset=dataset,
            event_dataset=None,
            config=app_config,
            strategy=PriceTrendStrategy(app_config.strategy),
        )

    admitted_source = source.model_copy(
        update={
            "passed": True,
            "reason_codes": ("ALL_PREREGISTERED_GATES_PASSED",),
        }
    )
    result = run_blind_evaluation(
        source=admitted_source,
        query_id="blind-query-1",
        dataset=dataset,
        event_dataset=None,
        config=app_config,
        strategy=PriceTrendStrategy(app_config.strategy),
    )

    assert result.reserved_start == dataset.bars[-50].open_time
    assert result.reserved_end == dataset.bars[-1].close_time
    assert (
        result.run.signal_start
        == dataset.bars[-50 + result.embargo_bars].close_time
    )
    assert result.run.signal_end == dataset.bars[-result.purge_bars].close_time
    assert result.dataset_id == source.dataset_id
    assert result.evaluation_spec_hash == source.evaluation_spec_hash
    assert result.result_id == stable_id(
        "blind_evaluation",
        result.version,
        result.query_id,
        result.source_evaluation_id,
        result.evaluation_spec_hash,
        result.dataset_id,
        result.event_dataset_id,
        result.artifact_hash,
        result.run.run_id,
    )
    blind_catalog = BlindEvaluationCatalog(tmp_path / "blind-evaluations")
    blind_catalog.store(result)
    assert blind_catalog.load(result.result_id) == result

    passed_result = result.model_copy(update={"passed": True})
    validate_forecast_gate_baseline(
        source=passed_result,
        config=app_config,
        strategy=PriceTrendStrategy(app_config.strategy),
        symbol=dataset.manifest.symbol,
    )
    with pytest.raises(ValueError, match="品种或周期"):
        validate_forecast_gate_baseline(
            source=passed_result,
            config=app_config,
            strategy=PriceTrendStrategy(app_config.strategy),
            symbol="ETHUSDT",
        )


def test_walk_forward_requires_matching_preregistered_full_spec(app_config) -> None:
    from quant_core.research.walk_forward import (
        WalkForwardEvaluationSpec,
        WalkForwardPlan,
        build_walk_forward_evaluation_plan,
        validate_walk_forward_evaluation_plan,
    )
    from quant_core.strategy import PriceTrendStrategy

    registered_at = _dataset().manifest.first_open_time
    strategy = PriceTrendStrategy(app_config.strategy)
    spec = WalkForwardEvaluationSpec.freeze(
        candidate="configured",
        dataset=_dataset(),
        event_dataset=None,
        config=app_config,
        strategy=strategy,
        plan=WalkForwardPlan(
            plan_id="walk-forward-preregistered-v1",
            training_bars=128,
            test_bars=100,
            blind_bars=50,
        ),
    )
    plan = build_walk_forward_evaluation_plan(
        spec=spec,
        base_manifest_id="champion-v1",
        registered_at=registered_at,
    )

    validate_walk_forward_evaluation_plan(
        spec=spec,
        plan=plan,
        champion_manifest_id="champion-v1",
        evaluated_at=registered_at + timedelta(seconds=1),
    )
    assert plan.candidate_spec_hash == content_hash(spec)
    assert plan.candidate_spec_snapshot == spec.model_dump(mode="json")
    assert plan.blind_query_budget == 1

    changed = spec.model_copy(
        update={
            "plan": spec.plan.model_copy(
                update={"minimum_profit_factor": Decimal("1.10")}
            )
        }
    )
    with pytest.raises(ValueError, match="预登记规格不一致"):
        validate_walk_forward_evaluation_plan(
            spec=changed,
            plan=plan,
            champion_manifest_id="champion-v1",
            evaluated_at=registered_at + timedelta(seconds=1),
        )

    with pytest.raises(ValueError, match="当前 Champion"):
        validate_walk_forward_evaluation_plan(
            spec=spec,
            plan=plan,
            champion_manifest_id="champion-v2",
            evaluated_at=registered_at + timedelta(seconds=1),
        )


def test_custom_research_strategy_identity_changes_artifact(app_config) -> None:
    from quant_core.research.backtest import artifact_hash

    first = _TestResearchSpec(version="test-long-v1")
    second = first.model_copy(update={"version": "test-long-v2"})

    assert artifact_hash(app_config, strategy_spec=first) != artifact_hash(
        app_config, strategy_spec=second
    )


def test_candidate_registry_rejects_retired_research_code(
    app_config, replay_input
) -> None:
    from quant_core.features import FeatureEngine
    from quant_core.research.candidates import (
        LongOnlyDualTrendSpec,
        LongOnlyDualTrendStrategy,
        resolve_research_candidate,
    )

    effective, strategy = resolve_research_candidate("configured", app_config)
    assert effective is app_config
    assert strategy.research_spec == app_config.strategy
    disabled = app_config.model_copy(
        update={
            "strategy": app_config.strategy.model_copy(update={"enabled": False})
        }
    )
    with pytest.raises(ValueError, match=r"已禁用.*评价基线"):
        resolve_research_candidate("configured", disabled)
    with pytest.raises(ValueError, match="未知或已退役"):
        resolve_research_candidate("long-only-tsmom-12m-v1", app_config)

    spec = LongOnlyDualTrendSpec(
        momentum_lookback_bars=3,
        regime_moving_average_bars=5,
        atr_bars=3,
    )
    strategy = LongOnlyDualTrendStrategy(spec)
    effective, resolved = resolve_research_candidate(
        "long-only-dual-trend-28d-sma200-5d-v1",
        app_config,
    )
    assert effective.market_data.interval == "1d"
    assert effective.market_data.bar_window == 200
    assert effective.frequency.cooldown_minutes == 7_200
    assert resolved.research_spec == LongOnlyDualTrendSpec()

    market = replay_input.market
    features = FeatureEngine(app_config.feature).compute(market)
    candidates = strategy.evaluate(
        market=market,
        account=replay_input.account,
        features=features,
    )
    assert len(candidates) == 1
    assert candidates[0].producer_version == spec.version
    assert candidates[0].signal_observed_at == market.as_of


def test_program_exit_uses_same_rule_in_nautilus_replay(app_config) -> None:
    pytest.importorskip("nautilus_trader")
    from quant_core.research.backtest import run_bar_backtest

    dataset = _dataset(
        count=40,
        price_steps=(Decimal("1.002"),) * 20 + (Decimal("0.998"),) * 20,
        interval="1d",
        bar_delta=timedelta(days=1),
    )
    spec = _TestResearchSpec(
        version="test-long-program-exit-v1",
        horizon_minutes=90 * 1_440,
        program_exit_bar_interval_minutes=1_440,
        program_exit_moving_average_bars=5,
    )
    effective = app_config.model_copy(
        update={
            "market_data": app_config.market_data.model_copy(
                update={"interval": "1d", "bar_window": 6}
            ),
            "frequency": app_config.frequency.model_copy(
                update={"cooldown_minutes": 7 * 1_440}
            ),
        }
    )
    run = run_bar_backtest(
        dataset=dataset,
        config=effective,
        strategy=_TestLongStrategy(spec),
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

    dataset = _dataset(count=450, interval="1d", bar_delta=timedelta(days=1))
    effective = app_config.model_copy(
        update={
            "market_data": app_config.market_data.model_copy(
                update={"interval": "1d", "bar_window": 30}
            )
        }
    )
    run = run_bar_backtest(
        dataset=dataset,
        config=effective,
        strategy=_TestLongStrategy(),
        signal_start=dataset.bars[30].close_time,
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

    dataset = _dataset(
        count=200,
        interval="4h",
        bar_delta=timedelta(hours=4),
    )
    spec = _TestResearchSpec(
        version="test-long-4h-v1",
        horizon_minutes=7 * 1_440,
        signal_validity_minutes=240,
    )
    effective = app_config.model_copy(
        update={
            "market_data": app_config.market_data.model_copy(
                update={"interval": "4h", "bar_window": 20}
            ),
            "frequency": app_config.frequency.model_copy(
                update={"cooldown_minutes": 7 * 1_440}
            ),
        }
    )
    run = run_bar_backtest(
        dataset=dataset,
        config=effective,
        strategy=_TestLongStrategy(spec),
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
