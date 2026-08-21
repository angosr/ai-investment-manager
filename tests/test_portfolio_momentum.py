from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from investment_manager.governance.models import EvaluationStage
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.market.models import ClosedMarketBar
from investment_manager.research.dataset import (
    HistoricalDataset,
    HistoricalDatasetManifest,
    InstrumentSpec,
    _bars_hash,
)
from investment_manager.research.portfolio_momentum import (
    PortfolioMomentumEvaluationSpec,
    PortfolioMomentumPolicy,
    PortfolioMomentumWalkForwardPlan,
    build_portfolio_momentum_evaluation_plan,
    current_portfolio_momentum_environment,
    dataset_identities,
    run_portfolio_momentum_backtest,
    run_portfolio_momentum_walk_forward,
    validate_portfolio_momentum_evaluation_plan,
)


def _dataset(symbol: str, *, count: int = 1_500, future_volume: Decimal | None = None):
    start = datetime(2020, 1, 1, tzinfo=UTC)
    base = "BTC" if symbol == "BTCUSDT" else "ETH"
    price = Decimal("100") if base == "BTC" else Decimal("50")
    bars = []
    for index in range(count):
        open_price = price
        phase = index % 160
        if phase < 75:
            change = Decimal("1.012")
        elif phase < 115:
            change = Decimal("0.982")
        else:
            change = Decimal("1.002")
        close_price = open_price * change
        open_time = start + timedelta(days=index)
        close_time = open_time + timedelta(days=1) - timedelta(milliseconds=1)
        volume = Decimal("1000") + Decimal(index % 17)
        if future_volume is not None and index >= 1_300:
            volume = future_volume
        bars.append(
            ClosedMarketBar(
                symbol=symbol,
                interval="1d",
                open_time=open_time,
                close_time=close_time,
                observed_at=close_time,
                open=open_price,
                high=max(open_price, close_price),
                low=min(open_price, close_price),
                close=close_price,
                volume=volume,
                source="portfolio-momentum-test",
            )
        )
        price = close_price
    frozen_bars = tuple(bars)
    instrument = InstrumentSpec(
        symbol=symbol,
        base_asset=base,
        quote_asset="USDT",
        price_increment=Decimal("0.01"),
        quantity_increment=Decimal("0.000001"),
        minimum_quantity=Decimal("0.000001"),
        maximum_quantity=Decimal("100000"),
        minimum_notional=Decimal("10"),
        minimum_price=Decimal("0.01"),
        maximum_price=Decimal("10000000"),
    )
    bars_hash = _bars_hash(frozen_bars)
    payload = {
        "schema_version": "historical-bars-v1",
        "source": "portfolio-momentum-test",
        "symbol": symbol,
        "interval": "1d",
        "requested_start": start,
        "requested_end": start + timedelta(days=count),
        "bars_hash": bars_hash,
        "instrument": instrument,
    }
    manifest = HistoricalDatasetManifest(
        dataset_id=stable_id("historical_dataset", *payload.values()),
        symbol=symbol,
        interval="1d",
        source="portfolio-momentum-test",
        collected_at=start + timedelta(days=count),
        requested_start=start,
        requested_end=start + timedelta(days=count),
        first_open_time=frozen_bars[0].open_time,
        last_close_time=frozen_bars[-1].close_time,
        bar_count=count,
        bars_hash=bars_hash,
        instrument=instrument,
    )
    return HistoricalDataset(manifest=manifest, bars=frozen_bars)


def test_portfolio_momentum_backtest_is_costed_reconciled_and_point_in_time() -> None:
    datasets = (_dataset("BTCUSDT"), _dataset("ETHUSDT"))
    policy = PortfolioMomentumPolicy()
    start = datasets[0].bars[900].open_time
    end = datasets[0].bars[1_250].open_time

    result = run_portfolio_momentum_backtest(
        datasets=datasets,
        policy=policy,
        starting_equity=Decimal("10000"),
        evaluation_start=start,
        evaluation_end=end,
    )

    assert result.completed
    assert result.metrics.trade_count > 0
    assert result.metrics.modeled_cost > 0
    assert result.metrics.gross_pnl - result.metrics.modeled_cost == result.metrics.net_pnl
    assert result.metrics.ending_equity - Decimal("10000") == result.metrics.net_pnl
    assert all(trade.opened_at > trade.signal_at for trade in result.trades)
    assert all((trade.closed_at - trade.opened_at).days == 5 for trade in result.trades)

    future_changed = (
        _dataset("BTCUSDT", future_volume=Decimal("999999999")),
        _dataset("ETHUSDT", future_volume=Decimal("0.0001")),
    )
    replay = run_portfolio_momentum_backtest(
        datasets=future_changed,
        policy=policy,
        starting_equity=Decimal("10000"),
        evaluation_start=start,
        evaluation_end=end,
    )
    assert replay.metrics == result.metrics
    assert [item.net_pnl for item in replay.trades] == [
        item.net_pnl for item in result.trades
    ]


def test_portfolio_momentum_rejects_unaligned_market_data() -> None:
    btc = _dataset("BTCUSDT")
    eth = _dataset("ETHUSDT")
    shifted_bars = tuple(
        bar.model_copy(
            update={
                "open_time": bar.open_time + timedelta(days=1),
                "close_time": bar.close_time + timedelta(days=1),
                "observed_at": bar.observed_at + timedelta(days=1),
            }
        )
        for bar in eth.bars
    )
    shifted_hash = _bars_hash(shifted_bars)
    shifted_manifest = eth.manifest.model_copy(
        update={
            "dataset_id": stable_id(
                "historical_dataset",
                "historical-bars-v1",
                eth.manifest.source,
                eth.manifest.symbol,
                eth.manifest.interval,
                eth.manifest.requested_start + timedelta(days=1),
                eth.manifest.requested_end + timedelta(days=1),
                shifted_hash,
                eth.manifest.instrument,
            ),
            "requested_start": eth.manifest.requested_start + timedelta(days=1),
            "requested_end": eth.manifest.requested_end + timedelta(days=1),
            "first_open_time": shifted_bars[0].open_time,
            "last_close_time": shifted_bars[-1].close_time,
            "bars_hash": shifted_hash,
        }
    )
    shifted = HistoricalDataset(manifest=shifted_manifest, bars=shifted_bars)

    with pytest.raises(ValueError, match="逐根严格对齐"):
        run_portfolio_momentum_backtest(
            datasets=(btc, shifted),
            policy=PortfolioMomentumPolicy(),
            starting_equity=Decimal("10000"),
            evaluation_start=btc.bars[900].open_time,
            evaluation_end=btc.bars[1_250].open_time,
        )


def test_portfolio_momentum_walk_forward_requires_exact_preregistration() -> None:
    datasets = (_dataset("BTCUSDT"), _dataset("ETHUSDT"))
    policy = PortfolioMomentumPolicy()
    plan = PortfolioMomentumWalkForwardPlan(
        plan_id="portfolio-momentum-test-v1",
        training_days=730,
        test_days=365,
        minimum_trades=1,
    )
    environment = current_portfolio_momentum_environment()
    spec = PortfolioMomentumEvaluationSpec(
        dataset_ids=dataset_identities(datasets),
        evaluator_code_version="1" * 40,
        evaluator_environment=environment,
        policy=policy,
        plan=plan,
    )
    registered_at = datetime(2026, 1, 1, tzinfo=UTC)
    registered = build_portfolio_momentum_evaluation_plan(
        spec=spec,
        base_manifest_id="champion-v1",
        registered_at=registered_at,
    )

    assert registered.required_stages == (
        EvaluationStage.STATIC,
        EvaluationStage.FIXED_REGRESSION,
        EvaluationStage.WALK_FORWARD,
    )
    assert registered.blind_query_budget == 0
    validate_portfolio_momentum_evaluation_plan(
        spec=spec,
        plan=registered,
        champion_manifest_id="champion-v1",
        evaluated_at=registered_at + timedelta(seconds=1),
        evaluator_code_version="1" * 40,
        evaluator_environment=environment,
    )
    result = run_portfolio_momentum_walk_forward(
        datasets=datasets,
        policy=policy,
        plan=plan,
        evaluation_spec_hash=content_hash(spec),
    )
    assert result.evaluation_spec_hash == content_hash(spec)
    assert result.folds

    with pytest.raises(ValueError, match="精确评价代码版本"):
        validate_portfolio_momentum_evaluation_plan(
            spec=spec,
            plan=registered,
            champion_manifest_id="champion-v1",
            evaluated_at=registered_at + timedelta(seconds=1),
            evaluator_code_version="2" * 40,
            evaluator_environment=environment,
        )
