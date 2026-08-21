from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from investment_manager.decision_cycle.portfolio import (
    PortfolioDecisionPipeline,
    PortfolioPipelineOutcome,
)
from investment_manager.execution.planning.planner import (
    InstrumentExecutionSpec,
    TradePlanner,
    TradePlannerPolicy,
)
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
from investment_manager.risk.portfolio import (
    PortfolioRiskEngine,
    PortfolioRiskPolicy,
    SleeveRiskProfile,
)

NOW = datetime(2026, 8, 20, 11, tzinfo=UTC)


class _PortfolioStore:
    def __init__(self) -> None:
        self.accounts = []
        self.targets = []

    def record_account(self, account) -> bool:
        self.accounts.append(account)
        return True

    def record_target(self, target) -> bool:
        self.targets.append(target)
        return True


class _DecisionStore:
    def __init__(self) -> None:
        self.values = []

    def record(self, value) -> bool:
        self.values.append(value)
        return True


def _instruments() -> tuple[InstrumentId, InstrumentId]:
    return (
        InstrumentId.binance_spot(
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
        ),
        InstrumentId(
            product=InstrumentProduct.USD_M_PERPETUAL,
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            settlement_asset="USDT",
        ),
    )


def _target() -> ForecastTarget:
    spot, perpetual = _instruments()
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


def _forecast() -> CalibratedForecast:
    target = _target()
    return CalibratedForecast(
        forecast_id="forecast-1",
        role=ForecastRole.PROGRAM_BASE,
        producer_id="carry-calibration",
        producer_version="v1",
        forecast_family="delta-neutral-funding-carry",
        target=target,
        horizon_minutes=240,
        direction=DirectionalView.UP,
        reference_prices=tuple(
            ForecastReferencePrice(
                instrument_id=leg.instrument.key,
                price=Decimal("100"),
            )
            for leg in target.legs
        ),
        expected_edge_half_life_seconds=3_600,
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


def _sleeve_id() -> str:
    forecast = _forecast()
    return SleeveTarget.identity_for(
        portfolio_id="primary",
        forecast_family=forecast.forecast_family,
        forecast_target_id=forecast.target.target_id,
    )


def _quotes() -> tuple[ExecutableQuote, ...]:
    return tuple(
        ExecutableQuote(
            source_quote_id=f"quote-{instrument.product.value}",
            instrument=instrument,
            as_of=NOW,
            observed_at=NOW,
            bid=Decimal("100"),
            bid_quantity=Decimal("100"),
            ask=Decimal("100"),
            ask_quantity=Decimal("100"),
            source="test",
        )
        for instrument in _instruments()
    )


def _account(*, gross: str = "0") -> PortfolioAccountSnapshot:
    gross_value = Decimal(gross)
    positions: tuple[InstrumentPosition, ...] = ()
    sleeves: tuple[SleevePosition, ...] = ()
    if gross_value > 0:
        quantity = gross_value / Decimal("200")
        spot, perpetual = _instruments()
        positions = (
            InstrumentPosition(
                instrument=spot,
                quantity=quantity,
                average_price=Decimal("100"),
            ),
            InstrumentPosition(
                instrument=perpetual,
                quantity=-quantity,
                average_price=Decimal("100"),
            ),
        )
        sleeves = (
            SleevePosition(
                sleeve_id=_sleeve_id(),
                forecast_family="delta-neutral-funding-carry",
                target=_target(),
                legs=positions,
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


def _pipeline(*, enabled: bool) -> PortfolioDecisionPipeline:
    instruments = tuple(item.key for item in _instruments())
    portfolio_store = _PortfolioStore()
    risk_store = _DecisionStore()
    plan_store = _DecisionStore()
    return PortfolioDecisionPipeline(
        PortfolioDecisionEngine(
            PortfolioDecisionPolicy(
                version="portfolio-v2",
                portfolio_id="primary",
                enabled=enabled,
            )
        ),
        PortfolioRiskEngine(
            PortfolioRiskPolicy(
                version="risk-v2",
                instrument_allowlist=instruments,
                maximum_quote_age_seconds=180,
                maximum_account_age_seconds=60,
                maximum_daily_loss=Decimal("200"),
                maximum_drawdown_fraction=Decimal("0.05"),
                maximum_gross_exposure_fraction=Decimal("0.5"),
                maximum_net_delta_fraction=Decimal("0.1"),
                maximum_instrument_fraction=Decimal("0.4"),
                maximum_margin_fraction=Decimal("0.5"),
                maximum_stress_loss_fraction=Decimal("0.002"),
                maximum_spread_bps=Decimal("20"),
                maximum_unhedged_fraction=Decimal("0.05"),
                maximum_unhedged_seconds=10,
            )
        ),
        TradePlanner(
            TradePlannerPolicy(
                version="planner-v2",
                managed_instruments=instruments,
            )
        ),
        portfolio_store,
        risk_store,
        plan_store,
    )


def _inputs(*, gross: str = "0") -> dict[str, object]:
    account = _account(gross=gross)
    return {
        "cycle_id": "cycle-1",
        "as_of": NOW,
        "sleeves": (
            PortfolioSleeveInput(
                sleeve_id=_sleeve_id(),
                estimated_variable_cost_bps=Decimal("5"),
                forecast=_forecast(),
            ),
        ),
        "account": account,
        "quotes": _quotes(),
        "risk_profiles": (
            SleeveRiskProfile(
                sleeve_id=_sleeve_id(),
                version="carry-risk-v1",
                basis_stress_bps=Decimal("50"),
                funding_stress_bps=Decimal("30"),
                execution_stress_bps=Decimal("20"),
            ),
        ),
        "execution_specs": tuple(
            InstrumentExecutionSpec(
                instrument=instrument,
                quantity_step=Decimal("0.01"),
                minimum_order_notional=Decimal("10"),
            )
            for instrument in _instruments()
        ),
    }


def test_off_pipeline_stops_before_target_and_risk() -> None:
    result = _pipeline(enabled=False).run(**_inputs())

    assert result.outcome == PortfolioPipelineOutcome.NO_CHANGE
    assert result.target is None


def test_pipeline_allocates_clamps_and_groups_one_carry_sleeve() -> None:
    result = _pipeline(enabled=True).run(**_inputs())

    assert result.outcome == PortfolioPipelineOutcome.PLANNED
    assert result.target is not None
    assert result.target.sleeves[0].desired_gross_notional == Decimal("3000")
    assert result.risk_decision is not None
    assert result.risk_decision.approved_target is not None
    assert (
        result.risk_decision.approved_target.sleeves[0].approved_gross_notional
        == Decimal("2000")
    )
    assert result.trade_plan is not None
    assert len(result.trade_plan.groups) == 1
    assert len(result.trade_plan.groups[0].legs) == 2


def test_pipeline_reads_equity_and_current_exposure_only_from_account() -> None:
    inputs = _inputs(gross="1000")
    account = inputs["account"]
    assert isinstance(account, PortfolioAccountSnapshot)
    inputs["account"] = account.model_copy(
        update={"cash_balance": Decimal("4000"), "equity": Decimal("5000")}
    )

    result = _pipeline(enabled=True).run(**inputs)

    assert result.target is not None
    assert result.target.reference_equity == Decimal("5000")
    assert "current_gross_notional" not in PortfolioSleeveInput.model_fields


def test_pipeline_rejects_account_sleeve_missing_from_inputs() -> None:
    inputs = _inputs(gross="1000")
    inputs["sleeves"] = ()

    with pytest.raises(ValueError, match="显式覆盖全部当前 Sleeve"):
        _pipeline(enabled=True).run(**inputs)
