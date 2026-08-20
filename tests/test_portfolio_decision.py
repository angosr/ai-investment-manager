from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from quant_core.asset_management import CalibratedForecast, ForecastRole
from quant_core.domain import DirectionalView
from quant_core.portfolio_decision import (
    PortfolioAssetInput,
    PortfolioDecisionEngine,
    PortfolioDecisionPolicy,
)

NOW = datetime(2026, 8, 20, 11, tzinfo=UTC)


def _forecast(
    symbol: str,
    *,
    forecast_id: str,
    gross_bps: str = "20",
    direction: DirectionalView = DirectionalView.UP,
    reference_price: str = "100",
    half_life_seconds: int = 3_600,
    available_at: datetime = NOW,
    valid_until: datetime = NOW + timedelta(hours=1),
) -> CalibratedForecast:
    return CalibratedForecast(
        forecast_id=forecast_id,
        role=ForecastRole.PROGRAM_BASE,
        producer_id="calibration",
        producer_version="v1",
        forecast_family="trend",
        symbol=symbol,
        horizon_minutes=240,
        direction=direction,
        reference_price=Decimal(reference_price),
        expected_edge_half_life_seconds=half_life_seconds,
        available_at=available_at,
        valid_until=valid_until,
        base_forecast_id=f"base-{forecast_id}",
        expected_gross_bps=Decimal(gross_bps) + Decimal("5"),
        conservative_gross_bps=Decimal(gross_bps),
        dispersion_bps=Decimal("30"),
        calibration_ref="calibration-v1",
        calibration_sample_size=40,
        non_overlapping_sample_size=30,
        input_refs=(f"input-{forecast_id}",),
    )


def _asset(
    symbol: str,
    *,
    current: str = "0",
    current_price: str = "100",
    cost_bps: str = "5",
    forecast: CalibratedForecast | None = None,
) -> PortfolioAssetInput:
    return PortfolioAssetInput(
        symbol=symbol,
        current_quote_notional=Decimal(current),
        current_price=Decimal(current_price),
        estimated_variable_cost_bps=Decimal(cost_bps),
        forecast=forecast,
    )


def _policy(**updates) -> PortfolioDecisionPolicy:
    base = PortfolioDecisionPolicy(
        version="portfolio-shadow-v1",
        portfolio_id="primary",
    )
    return base.model_copy(update=updates)


def test_engine_is_off_by_default() -> None:
    result = PortfolioDecisionEngine(_policy()).decide(
        cycle_id="cycle-1",
        as_of=NOW,
        reference_equity=Decimal("10000"),
        assets=(
            _asset(
                "BTCUSDT",
                forecast=_forecast("BTCUSDT", forecast_id="btc-1"),
            ),
        ),
    )

    assert result is None


def test_engine_selects_positive_fee_adjusted_long_forecasts_only() -> None:
    result = PortfolioDecisionEngine(_policy(enabled=True)).decide(
        cycle_id="cycle-1",
        as_of=NOW,
        reference_equity=Decimal("10000"),
        assets=(
            _asset(
                "BTCUSDT",
                forecast=_forecast("BTCUSDT", forecast_id="btc-1"),
            ),
            _asset(
                "ETHUSDT",
                forecast=_forecast(
                    "ETHUSDT",
                    forecast_id="eth-1",
                    direction=DirectionalView.DOWN,
                ),
            ),
            _asset(
                "SOLUSDT",
                cost_bps="18",
                forecast=_forecast(
                    "SOLUSDT",
                    forecast_id="sol-1",
                    gross_bps="20",
                ),
            ),
        ),
    )

    assert result is not None
    assert tuple(item.symbol for item in result.targets) == ("BTCUSDT",)
    assert result.targets[0].desired_quote_notional == Decimal("3000")
    assert result.targets[0].conservative_net_bps == Decimal("15")


def test_engine_selects_only_highest_conservative_net_edge() -> None:
    result = PortfolioDecisionEngine(_policy(enabled=True)).decide(
        cycle_id="cycle-1",
        as_of=NOW,
        reference_equity=Decimal("10000"),
        assets=(
            _asset(
                "BTCUSDT",
                forecast=_forecast(
                    "BTCUSDT",
                    forecast_id="btc-1",
                    gross_bps="20",
                ),
            ),
            _asset(
                "ETHUSDT",
                forecast=_forecast(
                    "ETHUSDT",
                    forecast_id="eth-1",
                    gross_bps="30",
                ),
            ),
        ),
    )

    assert result is not None
    assert tuple(item.symbol for item in result.targets) == ("ETHUSDT",)


def test_engine_emits_all_cash_target_to_exit_when_edge_disappears() -> None:
    result = PortfolioDecisionEngine(_policy(enabled=True)).decide(
        cycle_id="cycle-1",
        as_of=NOW,
        reference_equity=Decimal("10000"),
        assets=(_asset("BTCUSDT", current="2500"),),
    )

    assert result is not None
    assert result.targets == ()


def test_engine_hysteresis_suppresses_uneconomic_rebalance() -> None:
    result = PortfolioDecisionEngine(
        _policy(enabled=True, minimum_rebalance_notional=Decimal("100"))
    ).decide(
        cycle_id="cycle-1",
        as_of=NOW,
        reference_equity=Decimal("10000"),
        assets=(
            _asset(
                "BTCUSDT",
                current="2950",
                forecast=_forecast("BTCUSDT", forecast_id="btc-1"),
            ),
        ),
    )

    assert result is None


def test_engine_does_not_chase_edge_already_consumed_by_price() -> None:
    result = PortfolioDecisionEngine(_policy(enabled=True)).decide(
        cycle_id="cycle-1",
        as_of=NOW,
        reference_equity=Decimal("10000"),
        assets=(
            _asset(
                "BTCUSDT",
                current_price="100.20",
                forecast=_forecast(
                    "BTCUSDT",
                    forecast_id="btc-1",
                    gross_bps="20",
                ),
            ),
        ),
    )

    assert result is None


def test_engine_applies_alpha_time_decay_before_cost() -> None:
    forecast = _forecast(
        "BTCUSDT",
        forecast_id="btc-1",
        gross_bps="20",
        half_life_seconds=60,
        available_at=NOW - timedelta(seconds=60),
    )
    result = PortfolioDecisionEngine(_policy(enabled=True)).decide(
        cycle_id="cycle-1",
        as_of=NOW,
        reference_equity=Decimal("10000"),
        assets=(_asset("BTCUSDT", cost_bps="6", forecast=forecast),),
    )

    assert result is None


@pytest.mark.parametrize(
    "forecast",
    [
        _forecast(
            "BTCUSDT",
            forecast_id="future",
            available_at=NOW + timedelta(seconds=1),
        ),
        _forecast(
            "BTCUSDT",
            forecast_id="expired",
            available_at=NOW - timedelta(seconds=1),
            valid_until=NOW,
        ),
    ],
)
def test_engine_never_uses_unavailable_forecast(
    forecast: CalibratedForecast,
) -> None:
    result = PortfolioDecisionEngine(_policy(enabled=True)).decide(
        cycle_id="cycle-1",
        as_of=NOW,
        reference_equity=Decimal("10000"),
        assets=(_asset("BTCUSDT", forecast=forecast),),
    )

    assert result is None


def test_engine_requires_deterministic_asset_order() -> None:
    engine = PortfolioDecisionEngine(_policy(enabled=True))

    with pytest.raises(ValueError, match="唯一且排序"):
        engine.decide(
            cycle_id="cycle-1",
            as_of=NOW,
            reference_equity=Decimal("10000"),
            assets=(
                _asset("ETHUSDT"),
                _asset("BTCUSDT"),
            ),
        )
