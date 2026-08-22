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
    PortfolioAccountingTotals,
    PortfolioAccountSnapshot,
    PortfolioPerformanceAttribution,
    PortfolioPerformanceInterval,
    PortfolioPerformanceKind,
    SleeveTarget,
)
from investment_manager.portfolio.repository import (
    SqlPortfolioPerformanceStore,
    SqlPortfolioStore,
)
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
            maximum_quote_skew_seconds=15,
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
    assert portfolio.latest_account(portfolio_id="primary", as_of=NOW) == account
    assert portfolio.target(target.target_id) == target
    assert risk.decision(decision.decision_id) == decision
    assert decision.approved_target is not None
    assert risk.for_approved_targets(
        (decision.approved_target.approved_target_id,)
    ) == {decision.approved_target.approved_target_id: decision}
    assert plans.plan(plan.plan_id) == plan


def test_cash_target_persists_quotes_used_to_reject_considered_forecast() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    base, forecast = _forecast_pair()
    account, invested_target, _decision, _plan = _chain()
    sleeve_id = SleeveTarget.identity_for(
        portfolio_id="primary",
        forecast_family=forecast.forecast_family,
        forecast_target_id=forecast.target.target_id,
    )
    cash = PortfolioDecisionEngine(
        PortfolioDecisionPolicy(
            version="portfolio-v2",
            portfolio_id="primary",
            enabled=True,
        )
    ).decide(
        cycle_id=account.cycle_id,
        as_of=NOW,
        account=account,
        sleeves=(
            PortfolioSleeveInput(
                sleeve_id=sleeve_id,
                estimated_variable_cost_bps=Decimal("50"),
                forecast=forecast,
            ),
        ),
        quotes=invested_target.quotes,
    )
    assert cash is not None and cash.sleeves == ()

    forecasts = SqlForecastStore(engine)
    portfolio = SqlPortfolioStore(engine)
    assert forecasts.record(base)
    assert forecasts.record(forecast)
    assert portfolio.record_account(account)
    assert portfolio.record_target(cash)


def test_handoff_stores_reject_missing_authoritative_parent() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    _, _, decision, plan = _chain()

    with pytest.raises(ValueError, match="PortfolioTarget"):
        SqlPortfolioRiskStore(engine).record(decision)
    with pytest.raises(ValueError, match="Risk 授权"):
        SqlTradePlanStore(engine).record(plan)


def test_portfolio_performance_records_same_time_execution_once() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    accounts = SqlPortfolioStore(engine)
    performance = SqlPortfolioPerformanceStore(engine)
    start = PortfolioAccountSnapshot(
        snapshot_id="account-before-execution",
        cycle_id="cycle-before-execution",
        portfolio_id="primary",
        revision=0,
        as_of=NOW,
        observed_at=NOW,
        settlement_asset="USDT",
        cash_balance=Decimal("10000"),
        equity=Decimal("10000"),
        equity_high_water=Decimal("10000"),
        accounting=PortfolioAccountingTotals(starting_equity=Decimal("10000")),
    )
    end = PortfolioAccountSnapshot(
        snapshot_id="account-after-execution",
        cycle_id="cycle-after-execution",
        portfolio_id="primary",
        revision=1,
        as_of=NOW,
        observed_at=NOW,
        settlement_asset="USDT",
        cash_balance=Decimal("9996.5"),
        equity=Decimal("9996.5"),
        equity_high_water=Decimal("10000"),
        daily_pnl=Decimal("-3.5"),
        drawdown_fraction=Decimal("0.00035"),
        accounting=PortfolioAccountingTotals(
            starting_equity=Decimal("10000"),
            price_pnl=Decimal("-2.5"),
            fee_cost=Decimal("1"),
            execution_slippage_cost=Decimal("0.25"),
            compensation_loss=Decimal("0.5"),
            net_pnl=Decimal("-3.5"),
        ),
    )

    assert accounts.record_account(start)
    assert performance.record(start) is None
    assert accounts.record_account(end)
    interval = performance.record(end)

    assert interval is not None
    assert interval.kind == PortfolioPerformanceKind.EXECUTION
    assert interval.net_pnl == Decimal("-3.5")
    assert interval.return_fraction == Decimal("-0.00035")
    assert interval.attribution is not None
    assert interval.attribution.price_pnl == Decimal("-2.5")
    assert interval.attribution.fee_cost == Decimal("1")
    assert interval.attribution.compensation_loss == Decimal("0.5")
    assert performance.record(end) == interval
    assert performance.count(portfolio_id="primary") == 1
    assert performance.latest(portfolio_id="primary") == interval

    later = end.model_copy(
        update={
            "snapshot_id": "account-next-mark",
            "cycle_id": "cycle-next-mark",
            "revision": 2,
            "as_of": NOW + timedelta(minutes=30),
            "observed_at": NOW + timedelta(minutes=30),
            "cash_balance": Decimal("9997"),
            "equity": Decimal("9997"),
            "daily_pnl": Decimal("-3"),
            "drawdown_fraction": Decimal("0.0003"),
            "accounting": PortfolioAccountingTotals(
                starting_equity=Decimal("10000"),
                price_pnl=Decimal("-2"),
                fee_cost=Decimal("1"),
                execution_slippage_cost=Decimal("0.20"),
                compensation_loss=Decimal("0.5"),
                net_pnl=Decimal("-3"),
            ),
        }
    )
    assert accounts.record_account(later)
    mark_interval = performance.record(later)
    assert mark_interval is not None
    assert mark_interval.kind == PortfolioPerformanceKind.MARK_TO_MARKET
    assert mark_interval.net_pnl == Decimal("0.5")
    assert mark_interval.attribution is not None
    assert mark_interval.attribution.price_pnl == Decimal("0.5")
    assert mark_interval.attribution.execution_slippage_cost == Decimal("-0.05")
    assert performance.count(portfolio_id="primary") == 2
    assert performance.latest(portfolio_id="primary") == mark_interval

    with pytest.raises(ValueError, match="权威账户事实"):
        performance.record(
            later.model_copy(
                update={"equity": Decimal("1"), "cash_balance": Decimal("1")}
            )
        )


def test_portfolio_performance_starts_attribution_after_legacy_snapshot() -> None:
    """Deploying attribution must not make the first post-upgrade interval unrecordable."""

    legacy = PortfolioAccountSnapshot(
        snapshot_id="account-before-attribution",
        cycle_id="cycle-before-attribution",
        portfolio_id="primary",
        revision=4,
        as_of=NOW,
        observed_at=NOW,
        settlement_asset="USDT",
        cash_balance=Decimal("10000"),
        equity=Decimal("10000"),
        equity_high_water=Decimal("10000"),
    )
    attributed = PortfolioAccountSnapshot(
        snapshot_id="account-with-attribution",
        cycle_id="cycle-with-attribution",
        portfolio_id="primary",
        revision=5,
        as_of=NOW + timedelta(minutes=1),
        observed_at=NOW + timedelta(minutes=1),
        settlement_asset="USDT",
        cash_balance=Decimal("9999"),
        equity=Decimal("9999"),
        equity_high_water=Decimal("10000"),
        daily_pnl=Decimal("-1"),
        drawdown_fraction=Decimal("0.0001"),
        accounting=PortfolioAccountingTotals(
            starting_equity=Decimal("10000"),
            price_pnl=Decimal("0"),
            fee_cost=Decimal("1"),
            net_pnl=Decimal("-1"),
        ),
    )

    interval = PortfolioPerformanceInterval.between(legacy, attributed)

    assert interval.net_pnl == Decimal("-1")
    assert interval.attribution is None


def test_compensation_attribution_allows_later_fill_price_improvement() -> None:
    """A better later fill may reduce cumulative compensation loss without corrupting PnL."""

    start = PortfolioAccountingTotals(
        starting_equity=Decimal("10000"),
        price_pnl=Decimal("-5"),
        compensation_loss=Decimal("5"),
        net_pnl=Decimal("-5"),
    )
    end = PortfolioAccountingTotals(
        starting_equity=Decimal("10000"),
        price_pnl=Decimal("0"),
        compensation_loss=Decimal("0"),
        net_pnl=Decimal("0"),
    )

    attribution = PortfolioPerformanceAttribution.between(start, end)

    assert attribution.net_pnl == Decimal("5")
    assert attribution.compensation_loss == Decimal("-5")


def test_portfolio_performance_repairs_a_crash_gap_before_appending() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    accounts = SqlPortfolioStore(engine)
    performance = SqlPortfolioPerformanceStore(engine)
    baseline = PortfolioAccountSnapshot(
        snapshot_id="account-r0",
        cycle_id="cycle-r0",
        portfolio_id="primary",
        revision=0,
        as_of=NOW,
        observed_at=NOW,
        settlement_asset="USDT",
        cash_balance=Decimal("10000"),
        equity=Decimal("10000"),
        equity_high_water=Decimal("10000"),
    )
    missed = baseline.model_copy(
        update={
            "snapshot_id": "account-r1",
            "cycle_id": "cycle-r1",
            "revision": 1,
            "as_of": NOW + timedelta(minutes=1),
            "observed_at": NOW + timedelta(minutes=1),
            "cash_balance": Decimal("9999"),
            "equity": Decimal("9999"),
            "daily_pnl": Decimal("-1"),
            "drawdown_fraction": Decimal("0.0001"),
        }
    )
    current = missed.model_copy(
        update={
            "snapshot_id": "account-r2",
            "cycle_id": "cycle-r2",
            "revision": 2,
            "as_of": NOW + timedelta(minutes=2),
            "observed_at": NOW + timedelta(minutes=2),
            "cash_balance": Decimal("10001"),
            "equity": Decimal("10001"),
            "equity_high_water": Decimal("10001"),
            "daily_pnl": Decimal("1"),
            "drawdown_fraction": Decimal("0"),
        }
    )

    assert accounts.record_account(baseline)
    assert accounts.record_account(missed)  # Simulate exit before interval persistence.
    assert accounts.record_account(current)
    latest = performance.record(current)

    assert latest is not None
    assert latest.start_snapshot_id == missed.snapshot_id
    assert latest.net_pnl == Decimal("2")
    assert performance.count(portfolio_id="primary") == current.revision
