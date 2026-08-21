from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from investment_manager.decision_cycle.portfolio import (
    PortfolioDecisionPipeline,
    PortfolioPipelineOutcome,
)
from investment_manager.execution.models import Position
from investment_manager.execution.planner import (
    MarketExecutionSpec,
    TradePlanner,
    TradePlannerPolicy,
)
from investment_manager.forecast.models import (
    CalibratedForecast,
    DirectionalView,
    ForecastReferencePrice,
    ForecastRole,
    ForecastTarget,
)
from investment_manager.market.models import InstrumentId
from investment_manager.portfolio.decision import (
    PortfolioAssetInput,
    PortfolioDecisionEngine,
    PortfolioDecisionPolicy,
)
from investment_manager.risk.portfolio import (
    PortfolioRiskEngine,
    PortfolioRiskPolicy,
    ProtectiveStop,
)

NOW = datetime(2026, 8, 20, 11, tzinfo=UTC)


def _pipeline(*, enabled: bool) -> PortfolioDecisionPipeline:
    return PortfolioDecisionPipeline(
        PortfolioDecisionEngine(
            PortfolioDecisionPolicy(
                version="portfolio-v1",
                portfolio_id="primary",
                enabled=enabled,
            )
        ),
        PortfolioRiskEngine(
            PortfolioRiskPolicy(
                version="risk-v1",
                symbol_allowlist=("BTCUSDT",),
                maximum_market_age_seconds=180,
                maximum_account_age_seconds=60,
                maximum_daily_loss=Decimal("200"),
                maximum_drawdown_fraction=Decimal("0.05"),
                maximum_risk_fraction=Decimal("0.005"),
                maximum_total_exposure_fraction=Decimal("0.5"),
                maximum_position_notional=Decimal("2000"),
                maximum_spread_bps=Decimal("20"),
            )
        ),
        TradePlanner(
            TradePlannerPolicy(
                version="planner-v1",
                managed_symbols=("BTCUSDT",),
            )
        ),
    )


def _asset() -> PortfolioAssetInput:
    target = ForecastTarget.single_long(
        InstrumentId.binance_spot(
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
        )
    )
    forecast = CalibratedForecast(
        forecast_id="forecast-1",
        role=ForecastRole.PROGRAM_BASE,
        producer_id="program",
        producer_version="v1",
        forecast_family="trend",
        target=target,
        horizon_minutes=240,
        direction=DirectionalView.UP,
        reference_prices=(
            ForecastReferencePrice(
                instrument_id=target.legs[0].instrument.key,
                price=Decimal("100"),
            ),
        ),
        expected_edge_half_life_seconds=3600,
        available_at=NOW,
        valid_until=NOW + timedelta(hours=1),
        base_forecast_id="base-1",
        expected_gross_bps=Decimal("25"),
        conservative_gross_bps=Decimal("20"),
        dispersion_bps=Decimal("30"),
        calibration_ref="calibration-v1",
        calibration_sample_size=40,
        non_overlapping_sample_size=30,
        input_refs=("feature-1",),
    )
    return PortfolioAssetInput(
        symbol="BTCUSDT",
        current_quote_notional=Decimal("0"),
        current_price=Decimal("100"),
        estimated_variable_cost_bps=Decimal("5"),
        forecast=forecast,
    )


def _inputs(replay_input):
    market = replay_input.market.model_copy(
        update={
            "cycle_id": "cycle-1",
            "symbol": "BTCUSDT",
            "as_of": NOW,
            "observed_at": NOW,
            "bid": Decimal("100"),
            "ask": Decimal("100"),
            "last": Decimal("100"),
        }
    )
    account = replay_input.account.model_copy(
        update={
            "cycle_id": "cycle-1",
            "as_of": NOW,
            "observed_at": NOW,
            "quote_balance": Decimal("10000"),
            "equity": Decimal("10000"),
            "positions": (),
            "reconciled": True,
        }
    )
    return {
        "cycle_id": "cycle-1",
        "as_of": NOW,
        "reference_equity": Decimal("10000"),
        "assets": (_asset(),),
        "account": account,
        "markets": (market,),
        "protective_stops": (
            ProtectiveStop(symbol="BTCUSDT", stop_price=Decimal("95")),
        ),
        "execution_specs": (
            MarketExecutionSpec(
                symbol="BTCUSDT",
                quantity_step=Decimal("0.01"),
                minimum_order_notional=Decimal("10"),
            ),
        ),
    }


def test_off_pipeline_stops_before_target_and_risk(replay_input) -> None:
    result = _pipeline(enabled=False).run(**_inputs(replay_input))

    assert result.outcome == PortfolioPipelineOutcome.NO_CHANGE
    assert result.target is None
    assert result.risk_decision is None
    assert result.trade_plan is None


def test_single_pipeline_clamps_then_plans_without_second_economic_vote(
    replay_input,
) -> None:
    result = _pipeline(enabled=True).run(**_inputs(replay_input))

    assert result.outcome == PortfolioPipelineOutcome.PLANNED
    assert result.target is not None
    assert result.target.targets[0].desired_quote_notional == Decimal("3000")
    assert result.risk_decision is not None
    assert result.risk_decision.approved_target is not None
    assert (
        result.risk_decision.approved_target.targets[0].approved_quote_notional
        == Decimal("1000")
    )
    assert result.trade_plan is not None
    assert result.trade_plan.trades[0].quote_notional == Decimal("1000")


def test_pipeline_rejects_caller_supplied_position_notional_drift(
    replay_input,
) -> None:
    inputs = _inputs(replay_input)
    inputs["assets"] = (
        inputs["assets"][0].model_copy(
            update={"current_quote_notional": Decimal("1000")}
        ),
    )

    with pytest.raises(ValueError, match="current_quote_notional"):
        _pipeline(enabled=True).run(**inputs)


def test_pipeline_rejects_account_position_missing_from_asset_inputs(
    replay_input,
) -> None:
    inputs = _inputs(replay_input)
    inputs["account"] = inputs["account"].model_copy(
        update={
            "positions": (
                Position(
                    symbol="ETHUSDT",
                    quantity=Decimal("1"),
                    average_price=Decimal("100"),
                ),
            )
        }
    )

    with pytest.raises(ValueError, match="当前持仓缺少资产输入: ETHUSDT"):
        _pipeline(enabled=True).run(**inputs)
