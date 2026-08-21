from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from investment_manager.forecast.models import (
    BaseForecast,
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
    MockCandidateAuthorization,
    PortfolioAccountSnapshot,
    PortfolioEdgeBasis,
    SleevePosition,
    SleeveTarget,
    sleeve_gross_notional,
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
    forecast: BaseForecast | CalibratedForecast | None = None,
    cost_bps: str = "5",
    refresh_target: bool = True,
    mock_authorization: MockCandidateAuthorization | None = None,
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
        mock_authorization=mock_authorization,
        refresh_target=refresh_target,
    )


def _account(
    *,
    forecast: BaseForecast | CalibratedForecast | None = None,
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


def _base_forecast(*, gross_bps: str) -> BaseForecast:
    target = _target()
    return BaseForecast(
        forecast_id=f"mock-forecast-{gross_bps}",
        producer_id="btc-dynamic-carry",
        producer_version="dynamic-carry-v1",
        forecast_family="delta-neutral-dynamic-carry",
        target=target,
        horizon_minutes=7 * 24 * 60,
        direction=DirectionalView.UP,
        reference_prices=tuple(
            ForecastReferencePrice(
                instrument_id=leg.instrument.key,
                price=Decimal("100"),
            )
            for leg in target.legs
        ),
        observed_at=NOW,
        available_at=NOW,
        valid_until=NOW + timedelta(minutes=30),
        raw_score=Decimal(gross_bps),
        input_refs=("derivative-state-1",),
    )


def _mock_authorization() -> MockCandidateAuthorization:
    return MockCandidateAuthorization(
        version="mock-candidate-v1",
        producer_id="btc-dynamic-carry",
        producer_version="dynamic-carry-v1",
        forecast_family="delta-neutral-dynamic-carry",
        hypothesis_fingerprint="a" * 64,
        evaluation_plan_id="mock-evaluation-v1",
        valid_from=NOW - timedelta(days=1),
        valid_until=NOW + timedelta(days=30),
        maximum_allocation_fraction=Decimal("0.10"),
        minimum_entry_net_bps=Decimal("5"),
        minimum_hold_net_bps=Decimal("-5"),
    )


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


def test_mock_authorized_base_forecast_uses_hypothesis_edge_without_fake_calibration(
) -> None:
    forecast = _base_forecast(gross_bps="25")
    result = PortfolioDecisionEngine(_policy(enabled=True)).decide(
        cycle_id="cycle-1",
        as_of=NOW,
        account=_account(forecast=forecast),
        sleeves=(
            _input(
                forecast=forecast,
                cost_bps="20",
                mock_authorization=_mock_authorization(),
            ),
        ),
        quotes=_quotes(),
    )

    assert result is not None
    assert result.sleeves[0].desired_gross_notional == Decimal("1000")
    assert result.sleeves[0].edge_basis == PortfolioEdgeBasis.MOCK_HYPOTHESIS
    assert result.sleeves[0].decision_gross_bps == Decimal("25")
    assert result.sleeves[0].decision_net_bps == Decimal("5")


def test_base_forecast_without_mock_authorization_is_rejected() -> None:
    with pytest.raises(ValueError, match="Mock candidate authorization"):
        _input(forecast=_base_forecast(gross_bps="25"), cost_bps="20")


def test_mock_candidate_uses_lower_hold_threshold_without_forcing_entry() -> None:
    forecast = _base_forecast(gross_bps="16")
    authorization = _mock_authorization()
    input_value = _input(
        forecast=forecast,
        cost_bps="20",
        mock_authorization=authorization,
    )
    engine = PortfolioDecisionEngine(_policy(enabled=True))

    cash = engine.decide(
        cycle_id="cycle-1",
        as_of=NOW,
        account=_account(forecast=forecast),
        sleeves=(input_value,),
        quotes=_quotes(),
    )
    held = engine.decide(
        cycle_id="cycle-1",
        as_of=NOW,
        account=_account(forecast=forecast, gross="1000"),
        sleeves=(input_value,),
        quotes=_quotes(),
    )

    assert cash is not None and cash.sleeves == ()
    assert cash.quotes == _quotes()
    assert held is not None
    held_account = _account(forecast=forecast, gross="1000")
    assert held.sleeves[0].desired_gross_notional == sleeve_gross_notional(
        held_account.sleeves[0],
        quote_by_instrument={item.instrument.key: item for item in _quotes()},
    )

def test_engine_ranks_sleeves_and_allocates_only_remaining_capacity() -> None:
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
    desired = {
        item.forecast_ids: item.desired_gross_notional for item in result.sleeves
    }
    assert desired == {("eth",): Decimal("3000"), ("btc",): Decimal("2000")}


def test_engine_retains_unrefreshed_sleeve_while_allocating_new_opportunity() -> None:
    btc_forecast = _forecast(forecast_id="btc")
    eth_forecast = _forecast("ETHUSDT", forecast_id="eth", gross_bps="30")
    btc = _input(forecast=btc_forecast, refresh_target=False)
    eth = _input(forecast=eth_forecast)
    quotes = tuple(
        sorted((*_quotes(), *_quotes("ETHUSDT")), key=lambda item: item.instrument.key)
    )
    sleeves = tuple(sorted((btc, eth), key=lambda item: item.sleeve_id))

    result = PortfolioDecisionEngine(_policy(enabled=True)).decide(
        cycle_id="cycle-1",
        as_of=NOW,
        account=_account(forecast=btc_forecast, gross="2500"),
        sleeves=sleeves,
        quotes=quotes,
    )

    assert result is not None
    desired = {
        item.forecast_ids: item.desired_gross_notional for item in result.sleeves
    }
    assert desired == {("btc",): Decimal("2500.125"), ("eth",): Decimal("2499.875")}
    assert "UNCHANGED_SLEEVE_WITHOUT_NEW_FORECAST" in result.reason_codes


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
    assert len(result.sleeves) == 1
    account = _account(gross="2950")
    assert result.sleeves[0].desired_gross_notional == sleeve_gross_notional(
        account.sleeves[0],
        quote_by_instrument={item.instrument.key: item for item in _quotes()},
    )


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
