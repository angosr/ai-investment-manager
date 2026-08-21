from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine

from investment_manager.execution.planning.planner import (
    InstrumentExecutionSpec,
    TradePlanner,
    TradePlannerPolicy,
)
from investment_manager.execution.planning.repository import SqlTradePlanStore
from investment_manager.forecast.models import (
    BaseForecast,
    CalibratedForecast,
    DirectionalView,
    ForecastReferencePrice,
    ForecastRole,
    ForecastTarget,
)
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.market.models import ExecutableQuote, InstrumentId
from investment_manager.portfolio.decision import (
    PortfolioDecisionEngine,
    PortfolioDecisionPolicy,
    PortfolioSleeveInput,
)
from investment_manager.portfolio.models import (
    PortfolioAccountSnapshot,
    SleeveTarget,
)
from investment_manager.portfolio.repository import SqlPortfolioStore
from investment_manager.risk.portfolio import (
    PortfolioRiskEngine,
    PortfolioRiskPolicy,
    SleeveRiskProfile,
)
from investment_manager.risk.repository import SqlPortfolioRiskStore
from investment_manager.schema import create_schema

NOW = datetime(2026, 8, 21, 5, tzinfo=UTC)


def _forecast_pair() -> tuple[BaseForecast, CalibratedForecast]:
    instrument = InstrumentId.binance_spot(
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
    )
    target = ForecastTarget.single_long(instrument)
    reference = (
        ForecastReferencePrice(
            instrument_id=instrument.key,
            price=Decimal("100"),
        ),
    )
    base = BaseForecast(
        forecast_id="base-1",
        producer_id="trend",
        producer_version="v1",
        forecast_family="trend",
        target=target,
        horizon_minutes=240,
        direction=DirectionalView.UP,
        reference_prices=reference,
        observed_at=NOW,
        available_at=NOW,
        valid_until=NOW + timedelta(hours=1),
        raw_score=Decimal("1"),
        input_refs=("feature-1",),
    )
    calibrated = CalibratedForecast(
        forecast_id="forecast-1",
        role=ForecastRole.PROGRAM_BASE,
        producer_id="trend",
        producer_version="v1",
        forecast_family="trend",
        target=target,
        horizon_minutes=240,
        direction=DirectionalView.UP,
        reference_prices=reference,
        expected_edge_half_life_seconds=3_600,
        available_at=NOW,
        valid_until=NOW + timedelta(hours=1),
        base_forecast_id=base.forecast_id,
        expected_gross_bps=Decimal("25"),
        conservative_gross_bps=Decimal("20"),
        dispersion_bps=Decimal("30"),
        calibration_ref="calibration-v1",
        calibration_sample_size=40,
        non_overlapping_sample_size=30,
        input_refs=("feature-1",),
    )
    return base, calibrated


def _chain():
    _, forecast = _forecast_pair()
    instrument = forecast.target.legs[0].instrument
    sleeve_id = SleeveTarget.identity_for(
        portfolio_id="primary",
        forecast_family=forecast.forecast_family,
        forecast_target_id=forecast.target.target_id,
    )
    account = PortfolioAccountSnapshot(
        snapshot_id="account-1",
        cycle_id="cycle-1",
        portfolio_id="primary",
        as_of=NOW,
        observed_at=NOW,
        settlement_asset="USDT",
        cash_balance=Decimal("10000"),
        equity=Decimal("10000"),
        equity_high_water=Decimal("10000"),
    )
    quote = ExecutableQuote(
        source_quote_id="quote-1",
        instrument=instrument,
        as_of=NOW,
        observed_at=NOW,
        bid=Decimal("100"),
        bid_quantity=Decimal("100"),
        ask=Decimal("100"),
        ask_quantity=Decimal("100"),
        source="test",
    )
    target = PortfolioDecisionEngine(
        PortfolioDecisionPolicy(
            version="portfolio-v2",
            portfolio_id="primary",
            enabled=True,
        )
    ).decide(
        cycle_id="cycle-1",
        as_of=NOW,
        account=account,
        sleeves=(
            PortfolioSleeveInput(
                sleeve_id=sleeve_id,
                estimated_variable_cost_bps=Decimal("5"),
                forecast=forecast,
            ),
        ),
        quotes=(quote,),
    )
    assert target is not None
    profile = SleeveRiskProfile(
        sleeve_id=sleeve_id,
        version="trend-risk-v1",
        basis_stress_bps=Decimal("400"),
        funding_stress_bps=Decimal("0"),
        execution_stress_bps=Decimal("100"),
    )
    decision = PortfolioRiskEngine(
        PortfolioRiskPolicy(
            version="risk-v2",
            instrument_allowlist=(instrument.key,),
            maximum_quote_age_seconds=180,
            maximum_account_age_seconds=60,
            maximum_daily_loss=Decimal("200"),
            maximum_drawdown_fraction=Decimal("0.05"),
            maximum_gross_exposure_fraction=Decimal("0.5"),
            maximum_net_delta_fraction=Decimal("0.5"),
            maximum_instrument_fraction=Decimal("0.5"),
            maximum_margin_fraction=Decimal("0.5"),
            maximum_stress_loss_fraction=Decimal("0.02"),
            maximum_spread_bps=Decimal("20"),
            maximum_unhedged_fraction=Decimal("0.05"),
            maximum_unhedged_seconds=10,
        )
    ).evaluate(
        target=target,
        account=account,
        quotes=(quote,),
        risk_profiles=(profile,),
        as_of=NOW,
    )
    assert decision.approved_target is not None
    plan = TradePlanner(
        TradePlannerPolicy(
            version="planner-v2",
            managed_instruments=(instrument.key,),
        )
    ).plan(
        approved=decision.approved_target,
        account=account,
        quotes=(quote,),
        specs=(
            InstrumentExecutionSpec(
                instrument=instrument,
                quantity_step=Decimal("0.01"),
                minimum_order_notional=Decimal("10"),
            ),
        ),
        as_of=NOW,
    )
    return account, target, decision, plan


def test_sleeve_handoffs_persist_idempotently_in_dependency_order() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    forecast_store = SqlForecastStore(engine)
    base, calibrated = _forecast_pair()
    assert forecast_store.record(base)
    assert forecast_store.record(calibrated)
    account, target, decision, plan = _chain()
    portfolio = SqlPortfolioStore(engine)
    risk = SqlPortfolioRiskStore(engine)
    plans = SqlTradePlanStore(engine)

    with pytest.raises(ValueError, match="权威账户快照"):
        portfolio.record_target(target)
    assert portfolio.record_account(account)
    assert not portfolio.record_account(account)
    assert portfolio.record_target(target)
    assert not portfolio.record_target(target)
    assert risk.record(decision)
    assert not risk.record(decision)
    assert plans.record(plan)
    assert not plans.record(plan)

    assert portfolio.account(account.snapshot_id) == account
    assert portfolio.target(target.target_id) == target
    assert risk.decision(decision.decision_id) == decision
    assert plans.plan(plan.plan_id) == plan


def test_handoff_stores_reject_missing_authoritative_parent() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    _, _, decision, plan = _chain()

    with pytest.raises(ValueError, match="PortfolioTarget"):
        SqlPortfolioRiskStore(engine).record(decision)
    with pytest.raises(ValueError, match="Risk 授权"):
        SqlTradePlanStore(engine).record(plan)
