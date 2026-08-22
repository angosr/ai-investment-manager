from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from investment_manager.forecast.models import (
    ExposureDirection,
    ForecastLeg,
    ForecastTarget,
)
from investment_manager.kernel.identity import content_hash
from investment_manager.market.models import (
    ExecutableQuote,
    InstrumentId,
    InstrumentProduct,
)
from investment_manager.portfolio.models import (
    InstrumentPosition,
    PortfolioAccountSnapshot,
    PortfolioTarget,
    SleevePosition,
    SleeveTarget,
)
from investment_manager.risk.models import RiskOutcome
from investment_manager.risk.portfolio import (
    ApprovedSleeve,
    HoldingRiskOutcome,
    PortfolioRiskEngine,
    PortfolioRiskPolicy,
    SleeveRiskProfile,
)

NOW = datetime(2026, 8, 20, 11, tzinfo=UTC)


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


def _forecast_target() -> ForecastTarget:
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


def _sleeve_id() -> str:
    return SleeveTarget.identity_for(
        portfolio_id="primary",
        forecast_family="delta-neutral-funding-carry",
        forecast_target_id=_forecast_target().target_id,
    )


def _target(
    *,
    desired: str = "3000",
    as_of: datetime = NOW,
    valid_until: datetime | None = None,
    account: PortfolioAccountSnapshot | None = None,
    quotes: tuple[ExecutableQuote, ...] | None = None,
) -> PortfolioTarget:
    account = account or _account()
    quotes = quotes or _quotes()
    sleeve = SleeveTarget(
        sleeve_id=_sleeve_id(),
        forecast_family="delta-neutral-funding-carry",
        forecast_target=_forecast_target(),
        desired_gross_notional=Decimal(desired),
        forecast_ids=("forecast-1",),
        conservative_gross_bps=Decimal("20"),
        estimated_variable_cost_bps=Decimal("5"),
        conservative_net_bps=Decimal("15"),
        reason_codes=("POSITIVE_CONSERVATIVE_NET_EDGE",),
    )
    return PortfolioTarget(
        target_id="target-1",
        cycle_id="cycle-1",
        portfolio_id="primary",
        policy_version="portfolio-v2",
        as_of=as_of,
        valid_until=valid_until or as_of + timedelta(minutes=30),
        reference_equity=account.equity,
        account_snapshot_id=account.snapshot_id,
        account_snapshot_hash=content_hash(account),
        considered_forecast_ids=("forecast-1",),
        quotes=quotes,
        sleeves=(sleeve,),
        reason_codes=("TEST_TARGET",),
    )


def _quotes(*, observed_at: datetime = NOW) -> tuple[ExecutableQuote, ...]:
    return tuple(
        ExecutableQuote(
            source_quote_id=f"quote-{instrument.product.value}",
            instrument=instrument,
            as_of=NOW,
            observed_at=observed_at,
            bid=Decimal("100"),
            bid_quantity=Decimal("100"),
            ask=Decimal("100.01"),
            ask_quantity=Decimal("100"),
            source="test",
        )
        for instrument in _instruments()
    )


def _account(
    *,
    gross: str = "0",
    reconciled: bool = True,
    observed_at: datetime = NOW,
    daily_pnl: str = "0",
    drawdown: str = "0",
    kill_switch: bool = False,
    equity: str = "10000",
    pending: tuple[str, ...] = (),
) -> PortfolioAccountSnapshot:
    gross_value = Decimal(gross)
    positions: tuple[InstrumentPosition, ...] = ()
    sleeves: tuple[SleevePosition, ...] = ()
    if gross_value > 0:
        quantity = gross_value / Decimal("2") / Decimal("100")
        spot, perpetual = _instruments()
        legs = (
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
        positions = legs
        sleeves = (
            SleevePosition(
                sleeve_id=_sleeve_id(),
                forecast_family="delta-neutral-funding-carry",
                target=_forecast_target(),
                legs=legs,
            ),
        )
    return PortfolioAccountSnapshot(
        snapshot_id="account-1",
        cycle_id="cycle-1",
        portfolio_id="primary",
        as_of=NOW,
        observed_at=observed_at,
        settlement_asset="USDT",
        cash_balance=Decimal("10000") - gross_value,
        equity=Decimal(equity),
        equity_high_water=max(Decimal("10000"), Decimal(equity)),
        daily_pnl=Decimal(daily_pnl),
        drawdown_fraction=Decimal(drawdown),
        positions=positions,
        sleeves=sleeves,
        pending_execution_group_ids=pending,
        kill_switch_active=kill_switch,
        reconciled=reconciled,
    )


def _profile() -> SleeveRiskProfile:
    return SleeveRiskProfile(
        sleeve_id=_sleeve_id(),
        version="carry-risk-v1",
        basis_stress_bps=Decimal("50"),
        funding_stress_bps=Decimal("30"),
        execution_stress_bps=Decimal("20"),
        derivative_initial_margin_fraction=Decimal("1"),
    )


def _policy() -> PortfolioRiskPolicy:
    return PortfolioRiskPolicy(
        version="portfolio-risk-v2",
        instrument_allowlist=tuple(item.key for item in _instruments()),
        maximum_quote_age_seconds=180,
        maximum_quote_skew_seconds=15,
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


def _evaluate(*, target=None, account=None, quotes=None):
    account = account or _account()
    quotes = quotes or _quotes()
    return PortfolioRiskEngine(_policy()).evaluate(
        target=target or _target(account=account, quotes=quotes),
        account=account,
        quotes=quotes,
        risk_profiles=(_profile(),),
        as_of=NOW,
    )


def test_risk_clamps_entire_carry_sleeve_by_stress_budget() -> None:
    decision = _evaluate()

    assert decision.outcome == RiskOutcome.APPROVED
    assert decision.approved_target is not None
    approved = decision.approved_target.sleeves[0]
    assert approved.requested_gross_notional == Decimal("3000")
    assert approved.approved_gross_notional == Decimal("2000")
    assert approved.sleeve_scale == Decimal("2") / Decimal("3")
    assert approved.maximum_unhedged_notional == Decimal("500")
    assert len(approved.forecast_target.legs) == 2


def test_stale_one_leg_quote_clamps_whole_new_sleeve_to_zero() -> None:
    quotes = _quotes()
    stale = quotes[1].model_copy(update={"observed_at": NOW - timedelta(hours=1)})
    decision = _evaluate(quotes=(quotes[0], stale))

    assert decision.approved_target is not None
    approved = decision.approved_target.sleeves[0]
    assert approved.approved_gross_notional == 0
    assert "NEW_RISK_CLAMPED_TO_CURRENT" in approved.reason_codes


def test_cross_market_quote_skew_clamps_whole_new_sleeve_to_zero() -> None:
    spot, perpetual = _quotes()
    misaligned = perpetual.model_copy(
        update={"observed_at": NOW - timedelta(seconds=16)}
    )
    quotes = (spot, misaligned)
    decision = _evaluate(
        target=_target(quotes=quotes),
        quotes=quotes,
    )

    assert decision.approved_target is not None
    approved = decision.approved_target.sleeves[0]
    assert approved.approved_gross_notional == 0
    assert "NEW_RISK_CLAMPED_TO_CURRENT" in approved.reason_codes
    assert any(
        item.reason_code == "QUOTES_MISALIGNED"
        for item in decision.rule_results
    )


def test_cross_market_quote_skew_defers_holding_review() -> None:
    spot, perpetual = _quotes()
    misaligned = perpetual.model_copy(
        update={"observed_at": NOW - timedelta(seconds=16)}
    )

    review = PortfolioRiskEngine(_policy()).review_holding(
        account=_account(gross="1000"),
        quotes=(spot, misaligned),
        risk_profiles=(_profile(),),
        as_of=NOW,
    )

    assert review.outcome == HoldingRiskOutcome.DEFER
    assert any(
        item.reason_code == "HOLDING_QUOTES_MISALIGNED"
        for item in review.rule_results
    )


def test_pending_execution_group_blocks_new_risk_for_whole_sleeve() -> None:
    decision = _evaluate(account=_account(pending=("group-1",)))

    assert decision.approved_target is not None
    assert decision.approved_target.sleeves[0].approved_gross_notional == 0


def test_risk_reduction_is_allowed_with_stale_unreconciled_account() -> None:
    account = _account(
        gross="1000",
        reconciled=False,
        observed_at=NOW - timedelta(hours=1),
    )
    decision = _evaluate(
        target=_target(desired="500", account=account),
        account=account,
    )

    assert decision.approved_target is not None
    approved = decision.approved_target.sleeves[0]
    assert approved.approved_gross_notional == Decimal("500")
    assert "RISK_REDUCTION_ALLOWED" in approved.reason_codes


def test_kill_switch_forces_whole_sleeve_to_cash() -> None:
    decision = _evaluate(account=_account(gross="1000", kill_switch=True))

    assert decision.approved_target is not None
    assert decision.approved_target.sleeves[0].approved_gross_notional == 0


def test_holding_risk_review_distinguishes_hold_exit_and_defer() -> None:
    engine = PortfolioRiskEngine(_policy())
    holding = engine.review_holding(
        account=_account(gross="1000"),
        quotes=_quotes(),
        risk_profiles=(_profile(),),
        as_of=NOW,
    )
    exiting = engine.review_holding(
        account=_account(gross="1000", kill_switch=True),
        quotes=_quotes(),
        risk_profiles=(_profile(),),
        as_of=NOW,
    )
    deferred = engine.review_holding(
        account=_account(
            gross="1000",
            kill_switch=True,
            pending=("group-1",),
        ),
        quotes=_quotes(),
        risk_profiles=(_profile(),),
        as_of=NOW,
    )

    assert holding.outcome == HoldingRiskOutcome.HOLD
    assert exiting.outcome == HoldingRiskOutcome.EXIT
    assert deferred.outcome == HoldingRiskOutcome.DEFER
    assert any(item.reason_code == "KILL_SWITCH_ACTIVE" for item in exiting.rule_results)


def test_expired_target_is_rejected() -> None:
    target_as_of = NOW - timedelta(hours=1)
    account = _account().model_copy(update={"as_of": target_as_of, "observed_at": target_as_of})
    quotes = tuple(
        item.model_copy(update={"as_of": target_as_of, "observed_at": target_as_of})
        for item in _quotes()
    )
    decision = _evaluate(
        target=_target(
            as_of=target_as_of,
            valid_until=NOW - timedelta(minutes=1),
            account=account,
            quotes=quotes,
        ),
        account=account,
        quotes=quotes,
    )

    assert decision.outcome == RiskOutcome.REJECTED
    assert decision.approved_target is None


def test_target_must_explicitly_close_every_current_sleeve() -> None:
    target = _target().model_copy(update={"sleeves": ()})

    with pytest.raises(ValueError, match="显式包含全部当前 Sleeve"):
        _evaluate(target=target, account=_account(gross="1000"))


def test_approved_sleeve_contract_cannot_increase_requested_exposure() -> None:
    with pytest.raises(ValidationError, match="不得增加"):
        ApprovedSleeve(
            sleeve_id=_sleeve_id(),
            forecast_family="delta-neutral-funding-carry",
            forecast_target=_forecast_target(),
            requested_gross_notional=Decimal("1000"),
            approved_gross_notional=Decimal("1001"),
            sleeve_scale=Decimal("1"),
            risk_profile_version="carry-risk-v1",
            maximum_unhedged_notional=Decimal("100"),
            maximum_unhedged_seconds=10,
            reason_codes=("INVALID",),
        )
