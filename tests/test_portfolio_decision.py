from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from investment_manager.execution.planning.planner import InstrumentExecutionSpec
from investment_manager.forecast.contracts import ForecastPriceAnchor
from investment_manager.forecast.models import (
    ExposureDirection,
    ForecastLeg,
    ForecastQuantityMode,
    ForecastTarget,
)
from investment_manager.forecast.results import BaseForecast, ForecastBucketProbability
from investment_manager.kernel.identity import stable_id
from investment_manager.market.models import (
    ExecutableQuote,
    InstrumentId,
    InstrumentProduct,
)
from investment_manager.portfolio.decision import (
    PortfolioDecisionEngine,
    PortfolioDecisionPolicy,
    PortfolioSleeveInput,
    remaining_forecast_gross_bps,
)
from investment_manager.portfolio.models import (
    InstrumentPosition,
    MockCandidateAuthorization,
    PortfolioAccountSnapshot,
    SleevePosition,
    SleeveTarget,
)

NOW = datetime(2026, 8, 23, 12, tzinfo=UTC)
SPOT = InstrumentId.binance_spot(
    symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT"
)
PERPETUAL = InstrumentId(
    product=InstrumentProduct.USD_M_PERPETUAL,
    symbol="BTCUSDT",
    base_asset="BTC",
    quote_asset="USDT",
    settlement_asset="USDT",
)
TARGET = ForecastTarget.create(
    (
        ForecastLeg(
            instrument=SPOT,
            direction=ExposureDirection.LONG,
            gross_weight=Decimal("0.5"),
        ),
        ForecastLeg(
            instrument=PERPETUAL,
            direction=ExposureDirection.SHORT,
            gross_weight=Decimal("0.5"),
        ),
    ),
    quantity_mode=ForecastQuantityMode.SAME_BASE_QUANTITY,
)


def _forecast(
    *,
    expected_bps: str = "40",
    available_at: datetime = NOW - timedelta(minutes=1),
    valid_until: datetime = NOW + timedelta(minutes=30),
    behavior: str = "carry-v1",
) -> BaseForecast:
    cutoff_at = available_at - timedelta(minutes=1)
    anchors = tuple(
        ForecastPriceAnchor(
            instrument_id=leg.instrument.key,
            price=Decimal("100"),
            observed_at=cutoff_at,
            available_at=cutoff_at,
            quote_ref=f"cutoff-{leg.instrument.key}",
        )
        for leg in TARGET.legs
    )
    slot_id = stable_id("slot", cutoff_at.isoformat())
    return BaseForecast(
        forecast_id=stable_id("base_forecast", slot_id, behavior),
        contract_id="carry-contract-v1",
        decision_slot_id=slot_id,
        producer_id="cash-carry",
        producer_behavior_id=behavior,
        outcome_family_id="btc-carry",
        target=TARGET,
        horizon_minutes=1_440,
        cutoff_prices=anchors,
        entry_prices=tuple(
            item.model_copy(update={"available_at": available_at}) for item in anchors
        ),
        information_cutoff_at=cutoff_at,
        input_observed_at=cutoff_at,
        available_at=available_at,
        valid_until=valid_until,
        outcome_probabilities=(
            ForecastBucketProbability(bucket_id="LOSS", probability=Decimal("0.2")),
            ForecastBucketProbability(bucket_id="FLAT", probability=Decimal("0.3")),
            ForecastBucketProbability(bucket_id="GAIN", probability=Decimal("0.5")),
        ),
        expected_gross_bps=Decimal(expected_bps),
        input_refs=("market-input",),
    )


def _authorization(forecast: BaseForecast) -> MockCandidateAuthorization:
    return MockCandidateAuthorization(
        version="mock-v1",
        producer_id=forecast.producer_id,
        producer_behavior_id=forecast.producer_behavior_id,
        outcome_family_id=forecast.outcome_family_id,
        hypothesis_fingerprint="a" * 64,
        maximum_allocation_fraction=Decimal("0.10"),
        minimum_entry_net_bps=Decimal("5"),
        minimum_hold_net_bps=Decimal("-5"),
    )


def _input(forecast: BaseForecast | None = None) -> PortfolioSleeveInput:
    forecast = forecast or _forecast()
    return PortfolioSleeveInput(
        sleeve_id=SleeveTarget.identity_for(
            portfolio_id="primary",
            forecast_family=forecast.outcome_family_id,
            forecast_target_id=forecast.target.target_id,
        ),
        forecast=forecast,
        mock_authorization=_authorization(forecast),
    )


def _quotes(
    *,
    spot_bid: str = "100",
    spot_ask: str = "100",
    perpetual_bid: str = "100",
    perpetual_ask: str = "100",
    quantity: str = "100",
) -> tuple[ExecutableQuote, ...]:
    values = (
        ExecutableQuote(
            source_quote_id="spot-quote",
            instrument=SPOT,
            as_of=NOW,
            observed_at=NOW,
            bid=Decimal(spot_bid),
            bid_quantity=Decimal(quantity),
            ask=Decimal(spot_ask),
            ask_quantity=Decimal(quantity),
            source="test",
        ),
        ExecutableQuote(
            source_quote_id="perpetual-quote",
            instrument=PERPETUAL,
            as_of=NOW,
            observed_at=NOW,
            bid=Decimal(perpetual_bid),
            bid_quantity=Decimal(quantity),
            ask=Decimal(perpetual_ask),
            ask_quantity=Decimal(quantity),
            source="test",
        ),
    )
    return tuple(sorted(values, key=lambda item: item.instrument.key))


def _specs() -> tuple[InstrumentExecutionSpec, ...]:
    values = (
        InstrumentExecutionSpec(
            instrument=SPOT,
            quantity_step=Decimal("0.00001"),
            minimum_order_notional=Decimal("5"),
            fee_bps=Decimal("12.5"),
        ),
        InstrumentExecutionSpec(
            instrument=PERPETUAL,
            quantity_step=Decimal("0.001"),
            minimum_order_notional=Decimal("100"),
            fee_bps=Decimal("7.5"),
        ),
    )
    return tuple(sorted(values, key=lambda item: item.instrument.key))


def _account(*, holding: bool = False) -> PortfolioAccountSnapshot:
    sleeve = _input()
    positions = ()
    product_positions = ()
    if holding:
        product_positions = (
            InstrumentPosition(
                instrument=SPOT,
                quantity=Decimal("5"),
                average_price=Decimal("100"),
            ),
            InstrumentPosition(
                instrument=PERPETUAL,
                quantity=Decimal("-5"),
                average_price=Decimal("100"),
            ),
        )
        positions = (
            SleevePosition(
                sleeve_id=sleeve.sleeve_id,
                forecast_family=sleeve.forecast.outcome_family_id,
                target=TARGET,
                legs=(
                    InstrumentPosition(
                        instrument=SPOT,
                        quantity=Decimal("5"),
                        average_price=Decimal("100"),
                    ),
                    InstrumentPosition(
                        instrument=PERPETUAL,
                        quantity=Decimal("-5"),
                        average_price=Decimal("100"),
                    ),
                ),
            ),
        )
    return PortfolioAccountSnapshot(
        snapshot_id="account-1",
        cycle_id="cycle-1",
        portfolio_id="primary",
        as_of=NOW,
        observed_at=NOW,
        settlement_asset="USDT",
        cash_balance=Decimal("10000"),
            equity=Decimal("10000"),
            equity_high_water=Decimal("10000"),
            positions=product_positions,
            sleeves=positions,
    )


def _engine(*, enabled: bool = True) -> PortfolioDecisionEngine:
    return PortfolioDecisionEngine(
        PortfolioDecisionPolicy(
            version="portfolio-v1",
            portfolio_id="primary",
            enabled=enabled,
        )
    )


def test_engine_is_off_without_creating_a_target() -> None:
    assert (
        _engine(enabled=False).decide(
            cycle_id="cycle-1",
            as_of=NOW,
            account=_account(),
            sleeves=(_input(),),
            quotes=_quotes(),
            execution_specs=_specs(),
        )
        is None
    )


def test_portfolio_uses_actual_fee_tier_and_selects_only_positive_net_edge() -> None:
    target = _engine().decide(
        cycle_id="cycle-1",
        as_of=NOW,
        account=_account(),
        sleeves=(_input(),),
        quotes=_quotes(),
        execution_specs=_specs(),
    )
    assert target is not None and len(target.sleeves) == 1
    sleeve = target.sleeves[0]
    assert sleeve.desired_gross_notional == Decimal("1000")
    assert sleeve.cost.fee_bps == Decimal("20.00")
    assert sleeve.cost.total_bps == Decimal("20.00")
    assert sleeve.decision_net_bps == Decimal("20.00")


def test_negative_fee_after_edge_is_preserved_as_a_cash_decision() -> None:
    target = _engine().decide(
        cycle_id="cycle-1",
        as_of=NOW,
        account=_account(),
        sleeves=(_input(_forecast(expected_bps="20")),),
        quotes=_quotes(),
        execution_specs=_specs(),
    )
    assert target is not None
    assert target.sleeves == ()
    assert target.reason_codes == ("CASH_SELECTED_NO_POSITIVE_NET_EDGE",)


def test_repricing_keeps_favorable_and_adverse_moves_in_payoff_algebra() -> None:
    forecast = _forecast()
    favorable = remaining_forecast_gross_bps(
        forecast,
        quote_by_instrument={item.instrument.key: item for item in _quotes(
            spot_bid="99", spot_ask="99", perpetual_bid="101", perpetual_ask="101"
        )},
        as_of=NOW,
    )
    adverse = remaining_forecast_gross_bps(
        forecast,
        quote_by_instrument={item.instrument.key: item for item in _quotes(
            spot_bid="101", spot_ask="101", perpetual_bid="99", perpetual_ask="99"
        )},
        as_of=NOW,
    )
    assert favorable > forecast.expected_gross_bps
    assert adverse < forecast.expected_gross_bps


def test_base_forecast_requires_exact_behavior_authorization() -> None:
    forecast = _forecast()
    permission = _authorization(forecast).model_copy(
        update={"producer_behavior_id": "different"}
    )
    with pytest.raises(ValueError, match="精确绑定"):
        PortfolioSleeveInput(
            sleeve_id=_input(forecast).sleeve_id,
            forecast=forecast,
            mock_authorization=permission,
        )


def test_expired_forecast_forces_an_existing_sleeve_to_cash() -> None:
    expired = _forecast(
        available_at=NOW - timedelta(hours=2),
        valid_until=NOW - timedelta(hours=1),
    )
    target = _engine().decide(
        cycle_id="cycle-1",
        as_of=NOW,
        account=_account(holding=True),
        sleeves=(_input(expired),),
        quotes=_quotes(),
        execution_specs=_specs(),
    )
    assert target is not None
    assert target.sleeves[0].desired_gross_notional == 0
    assert "EXPIRED_FORECAST_EXIT" in target.reason_codes


def test_cost_increases_when_desired_size_exceeds_visible_depth() -> None:
    target = _engine().decide(
        cycle_id="cycle-1",
        as_of=NOW,
        account=_account(),
        sleeves=(_input(_forecast(expected_bps="500")),),
        quotes=_quotes(
            spot_bid="99.9",
            spot_ask="100.1",
            perpetual_bid="99.9",
            perpetual_ask="100.1",
            quantity="1",
        ),
        execution_specs=_specs(),
    )
    assert target is not None
    assert target.sleeves[0].cost.depth_slippage_bps > 0


def test_decision_rejects_incomplete_product_quotes() -> None:
    with pytest.raises(ValueError, match="精确覆盖"):
        _engine().decide(
            cycle_id="cycle-1",
            as_of=NOW,
            account=_account(),
            sleeves=(_input(),),
            quotes=(_quotes()[0],),
            execution_specs=_specs(),
        )
