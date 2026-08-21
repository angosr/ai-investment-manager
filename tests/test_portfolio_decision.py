from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from investment_manager.forecast.models import (
    CalibratedForecast,
    DirectionalView,
    ExposureDirection,
    ForecastLeg,
    ForecastReferencePrice,
    ForecastRole,
    ForecastTarget,
)
from investment_manager.market.models import (
    ExecutableQuote,
    InstrumentId,
    InstrumentProduct,
)
from investment_manager.portfolio.decision import (
    PortfolioDecisionEngine,
    PortfolioDecisionPolicy,
    PortfolioSleeveInput,
)
from investment_manager.portfolio.models import (
    InstrumentPosition,
    PortfolioAccountSnapshot,
    SleevePosition,
    SleeveTarget,
)

NOW = datetime(2026, 8, 20, 11, tzinfo=UTC)


def _instruments(symbol: str = "BTCUSDT") -> tuple[InstrumentId, InstrumentId]:
    base = symbol.removesuffix("USDT")
    return (
        InstrumentId.binance_spot(
            symbol=symbol,
            base_asset=base,
            quote_asset="USDT",
        ),
        InstrumentId(
            product=InstrumentProduct.USD_M_PERPETUAL,
            symbol=symbol,
            base_asset=base,
            quote_asset="USDT",
            settlement_asset="USDT",
        ),
    )


def _target(symbol: str = "BTCUSDT") -> ForecastTarget:
    spot, perpetual = _instruments(symbol)
    return ForecastTarget.create(
        (
            ForecastLeg(
                instrument=spot,
                direction=ExposureDirection.LONG,
                gross_weight=Decimal("0.5"),
            ),
            ForecastLeg(
                instrument=perpetual,
                direction=ExposureDirection.SHORT,
                gross_weight=Decimal("0.5"),
            ),
        )
    )


def _forecast(
    symbol: str = "BTCUSDT",
    *,
    forecast_id: str = "forecast-1",
    gross_bps: str = "20",
    direction: DirectionalView = DirectionalView.UP,
    half_life_seconds: int = 3_600,
    available_at: datetime = NOW,
    valid_until: datetime = NOW + timedelta(hours=1),
) -> CalibratedForecast:
    target = _target(symbol)
    return CalibratedForecast(
        forecast_id=forecast_id,
        role=ForecastRole.PROGRAM_BASE,
        producer_id="carry-calibration",
        producer_version="v1",
        forecast_family="delta-neutral-funding-carry",
        target=target,
        horizon_minutes=240,
        direction=direction,
        reference_prices=tuple(
            ForecastReferencePrice(
                instrument_id=leg.instrument.key,
                price=Decimal("100"),
            )
            for leg in target.legs
        ),
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


def _quotes(symbol: str = "BTCUSDT", *, spot_bid: str = "100"):
    quotes = []
    for instrument in _instruments(symbol):
        bid = Decimal(spot_bid) if instrument.product == InstrumentProduct.SPOT else Decimal("100")
        quotes.append(
            ExecutableQuote(
                source_quote_id=f"quote-{instrument.product.value}",
                instrument=instrument,
                as_of=NOW,
                observed_at=NOW,
                bid=bid,
                bid_quantity=Decimal("100"),
                ask=bid + Decimal("0.01"),
                ask_quantity=Decimal("100"),
                source="test",
            )
        )
    return tuple(quotes)


def _input(
    *,
    forecast: CalibratedForecast | None = None,
    cost_bps: str = "5",
) -> PortfolioSleeveInput:
    forecast = forecast or _forecast()
    sleeve_id = SleeveTarget.identity_for(
        portfolio_id="primary",
        forecast_family=forecast.forecast_family,
        forecast_target_id=forecast.target.target_id,
    )
    return PortfolioSleeveInput(
        sleeve_id=sleeve_id,
        estimated_variable_cost_bps=Decimal(cost_bps),
        forecast=forecast,
    )


def _account(
    *,
    forecast: CalibratedForecast | None = None,
    gross: str = "0",
) -> PortfolioAccountSnapshot:
    forecast = forecast or _forecast()
    gross_value = Decimal(gross)
    positions: tuple[InstrumentPosition, ...] = ()
    sleeves: tuple[SleevePosition, ...] = ()
    if gross_value > 0:
        legs = tuple(
            InstrumentPosition(
                instrument=leg.instrument,
                quantity=(
                    Decimal("1")
                    if leg.direction == ExposureDirection.LONG
                    else Decimal("-1")
                )
                * gross_value
                * leg.gross_weight
                / Decimal("100"),
                average_price=Decimal("100"),
            )
            for leg in forecast.target.legs
        )
        positions = legs
        sleeves = (
            SleevePosition(
                sleeve_id=SleeveTarget.identity_for(
                    portfolio_id="primary",
                    forecast_family=forecast.forecast_family,
                    forecast_target_id=forecast.target.target_id,
                ),
                forecast_family=forecast.forecast_family,
                target=forecast.target,
                legs=legs,
            ),
        )
    return PortfolioAccountSnapshot(
        snapshot_id="account-1",
        cycle_id="cycle-1",
        portfolio_id="primary",
        as_of=NOW,
        observed_at=NOW,
        settlement_asset="USDT",
        cash_balance=Decimal("10000") - gross_value,
        equity=Decimal("10000"),
        equity_high_water=Decimal("10000"),
        positions=positions,
        sleeves=sleeves,
    )


def _policy(**updates) -> PortfolioDecisionPolicy:
    return PortfolioDecisionPolicy(
        version="portfolio-shadow-v2",
        portfolio_id="primary",
    ).model_copy(update=updates)


def test_engine_is_off_by_default() -> None:
    result = PortfolioDecisionEngine(_policy()).decide(
        cycle_id="cycle-1",
        as_of=NOW,
        account=_account(),
        sleeves=(_input(),),
        quotes=_quotes(),
    )

    assert result is None


def test_engine_allocates_one_multi_leg_sleeve_in_gross_notional() -> None:
    result = PortfolioDecisionEngine(_policy(enabled=True)).decide(
        cycle_id="cycle-1",
        as_of=NOW,
        account=_account(),
        sleeves=(_input(),),
        quotes=_quotes(),
    )

    assert result is not None
    assert len(result.sleeves) == 1
    assert result.sleeves[0].desired_gross_notional == Decimal("3000")
    assert tuple(
        (leg.instrument.product, leg.direction, leg.gross_weight)
        for leg in result.sleeves[0].forecast_target.legs
    ) == (
        (InstrumentProduct.SPOT, ExposureDirection.LONG, Decimal("0.5")),
        (
            InstrumentProduct.USD_M_PERPETUAL,
            ExposureDirection.SHORT,
            Decimal("0.5"),
        ),
    )


def test_engine_selects_highest_fee_adjusted_sleeve() -> None:
    btc = _input(forecast=_forecast(forecast_id="btc", gross_bps="20"))
    eth_forecast = _forecast("ETHUSDT", forecast_id="eth", gross_bps="30")
    eth = _input(forecast=eth_forecast)
    quotes = tuple(sorted((*_quotes(), *_quotes("ETHUSDT")), key=lambda item: item.instrument.key))
    sleeves = tuple(sorted((btc, eth), key=lambda item: item.sleeve_id))

    result = PortfolioDecisionEngine(_policy(enabled=True)).decide(
        cycle_id="cycle-1",
        as_of=NOW,
        account=_account(),
        sleeves=sleeves,
        quotes=quotes,
    )

    assert result is not None
    assert result.sleeves[0].forecast_ids == ("eth",)


def test_engine_emits_explicit_zero_target_to_exit_open_sleeve() -> None:
    result = PortfolioDecisionEngine(_policy(enabled=True)).decide(
        cycle_id="cycle-1",
        as_of=NOW,
        account=_account(forecast=_forecast(direction=DirectionalView.DOWN), gross="2500"),
        sleeves=(
            _input(
                forecast=_forecast(direction=DirectionalView.DOWN),
            ),
        ),
        quotes=_quotes(),
    )

    assert result is not None
    assert result.sleeves[0].desired_gross_notional == 0
    assert "CASH_SELECTED" in result.sleeves[0].reason_codes


def test_engine_hysteresis_suppresses_uneconomic_rebalance() -> None:
    result = PortfolioDecisionEngine(
        _policy(enabled=True, minimum_rebalance_notional=Decimal("100"))
    ).decide(
        cycle_id="cycle-1",
        as_of=NOW,
        account=_account(gross="2950"),
        sleeves=(_input(),),
        quotes=_quotes(),
    )

    assert result is not None
    assert "REBALANCE_BELOW_MINIMUM" in result.reason_codes


def test_engine_does_not_chase_sleeve_edge_already_consumed() -> None:
    result = PortfolioDecisionEngine(_policy(enabled=True)).decide(
        cycle_id="cycle-1",
        as_of=NOW,
        account=_account(),
        sleeves=(_input(),),
        quotes=_quotes(spot_bid="100.50"),
    )

    assert result is not None
    assert result.sleeves == ()
    assert "CASH_SELECTED_NO_ELIGIBLE_FORECAST" in result.reason_codes


def test_engine_requires_complete_product_quotes() -> None:
    with pytest.raises(ValueError, match="精确覆盖"):
        PortfolioDecisionEngine(_policy(enabled=True)).decide(
            cycle_id="cycle-1",
            as_of=NOW,
            account=_account(),
            sleeves=(_input(),),
            quotes=(_quotes()[0],),
        )


@pytest.mark.parametrize(
    "forecast",
    [
        _forecast(forecast_id="future", available_at=NOW + timedelta(seconds=1)),
        _forecast(
            forecast_id="expired",
            available_at=NOW - timedelta(seconds=1),
            valid_until=NOW,
        ),
    ],
)
def test_engine_never_opens_from_unavailable_forecast(
    forecast: CalibratedForecast,
) -> None:
    result = PortfolioDecisionEngine(_policy(enabled=True)).decide(
        cycle_id="cycle-1",
        as_of=NOW,
        account=_account(forecast=forecast),
        sleeves=(_input(forecast=forecast),),
        quotes=_quotes(),
    )

    assert result is not None
    assert result.sleeves == ()
    assert "CASH_SELECTED_NO_ELIGIBLE_FORECAST" in result.reason_codes
