from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from investment_manager.asset_management import AssetTarget, PortfolioTarget
from investment_manager.domain import AccountSnapshot, Position, RiskOutcome
from investment_manager.portfolio_risk import (
    ApprovedAssetTarget,
    PortfolioRiskEngine,
    PortfolioRiskPolicy,
    ProtectiveStop,
)

NOW = datetime(2026, 8, 20, 11, tzinfo=UTC)


def _policy() -> PortfolioRiskPolicy:
    return PortfolioRiskPolicy(
        version="portfolio-risk-v1",
        symbol_allowlist=("BTCUSDT", "ETHUSDT"),
        maximum_market_age_seconds=180,
        maximum_account_age_seconds=60,
        maximum_daily_loss=Decimal("200"),
        maximum_drawdown_fraction=Decimal("0.05"),
        maximum_risk_fraction=Decimal("0.005"),
        maximum_total_exposure_fraction=Decimal("0.5"),
        maximum_position_notional=Decimal("2000"),
        maximum_spread_bps=Decimal("20"),
    )


def _target(
    *,
    desired: str = "3000",
    as_of=NOW,
    valid_until=None,
) -> PortfolioTarget:
    target = AssetTarget(
        symbol="BTCUSDT",
        desired_quote_notional=Decimal(desired),
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
        policy_version="portfolio-v1",
        as_of=as_of,
        valid_until=valid_until or as_of + timedelta(minutes=30),
        reference_equity=Decimal("10000"),
        targets=(target,),
    )


def _account(
    *,
    quantity: str = "0",
    reconciled: bool = True,
    observed_at=NOW,
    daily_pnl: str = "0",
    drawdown: str = "0",
    kill_switch: bool = False,
    equity: str = "10000",
    symbol: str = "BTCUSDT",
) -> AccountSnapshot:
    positions = (
        (
            Position(
                symbol=symbol,
                quantity=Decimal(quantity),
                average_price=Decimal("100"),
            ),
        )
        if Decimal(quantity) != 0
        else ()
    )
    return AccountSnapshot(
        cycle_id="cycle-1",
        as_of=NOW,
        observed_at=observed_at,
        quote_balance=Decimal("10000"),
        positions=positions,
        equity=Decimal(equity),
        reconciled=reconciled,
        daily_pnl=Decimal(daily_pnl),
        drawdown_fraction=Decimal(drawdown),
        kill_switch_active=kill_switch,
    )


def _evaluate(
    replay_input,
    *,
    target=None,
    account=None,
    market=None,
    stops=None,
):
    base_market = replay_input.market.model_copy(
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
    return PortfolioRiskEngine(_policy()).evaluate(
        target=target or _target(),
        account=account or _account(),
        markets=((market or base_market),),
        protective_stops=(
            stops
            if stops is not None
            else (ProtectiveStop(symbol="BTCUSDT", stop_price=Decimal("95")),)
        ),
        as_of=NOW,
    )


def test_risk_clamps_target_by_stop_loss_budget(replay_input) -> None:
    decision = _evaluate(replay_input)

    assert decision.outcome == RiskOutcome.APPROVED
    assert decision.approved_target is not None
    approved = decision.approved_target.targets[0]
    assert approved.requested_quote_notional == Decimal("3000")
    assert approved.approved_quote_notional == Decimal("1000")
    assert approved.approved_quote_notional <= approved.requested_quote_notional


def test_unreconciled_account_cannot_increase_risk(replay_input) -> None:
    decision = _evaluate(
        replay_input,
        account=_account(quantity="10", reconciled=False),
    )

    assert decision.approved_target is not None
    approved = decision.approved_target.targets[0]
    assert approved.approved_quote_notional == Decimal("1000")
    assert approved.reason_codes == ("NEW_RISK_CLAMPED_TO_CURRENT",)


def test_risk_reduction_is_not_blocked_by_missing_stop_or_stale_account(
    replay_input,
) -> None:
    decision = _evaluate(
        replay_input,
        target=_target(desired="500"),
        account=_account(
            quantity="10",
            reconciled=False,
            observed_at=NOW - timedelta(hours=1),
        ),
        stops=(),
    )

    assert decision.approved_target is not None
    approved = decision.approved_target.targets[0]
    assert approved.approved_quote_notional == Decimal("500")
    assert approved.reason_codes == ("RISK_REDUCTION_ALLOWED",)


def test_risk_explicitly_authorizes_cash_target_for_unselected_position(
    replay_input,
) -> None:
    btc_market = replay_input.market.model_copy(
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
    eth_market = btc_market.model_copy(update={"symbol": "ETHUSDT"})

    decision = PortfolioRiskEngine(_policy()).evaluate(
        target=_target(),
        account=_account(quantity="5", symbol="ETHUSDT"),
        markets=(btc_market, eth_market),
        protective_stops=(
            ProtectiveStop(symbol="BTCUSDT", stop_price=Decimal("95")),
        ),
        as_of=NOW,
    )

    assert decision.approved_target is not None
    approved = decision.approved_target.targets
    assert tuple(item.symbol for item in approved) == ("BTCUSDT", "ETHUSDT")
    assert approved[1].requested_quote_notional == 0
    assert approved[1].approved_quote_notional == 0
    assert approved[1].reason_codes == ("RISK_REDUCTION_ALLOWED",)


def test_kill_switch_forces_target_to_cash(replay_input) -> None:
    decision = _evaluate(
        replay_input,
        account=_account(quantity="10", kill_switch=True),
    )

    assert decision.approved_target is not None
    approved = decision.approved_target.targets[0]
    assert approved.approved_quote_notional == Decimal("0")
    assert approved.reason_codes == ("ACCOUNT_RISK_FORCED_CASH",)


def test_expired_target_is_rejected(replay_input) -> None:
    decision = _evaluate(
        replay_input,
        target=_target(
            as_of=NOW - timedelta(hours=1),
            valid_until=NOW - timedelta(minutes=1),
        ),
    )

    assert decision.outcome == RiskOutcome.REJECTED
    assert decision.approved_target is None


def test_zero_equity_fails_closed_to_zero_target(replay_input) -> None:
    decision = _evaluate(
        replay_input,
        account=_account(equity="0"),
    )

    assert decision.approved_target is not None
    assert decision.approved_target.reference_equity == 0
    assert decision.approved_target.targets[0].approved_quote_notional == 0


def test_approved_target_contract_cannot_increase_requested_exposure() -> None:
    with pytest.raises(ValidationError, match="不得增加"):
        ApprovedAssetTarget(
            symbol="BTCUSDT",
            requested_quote_notional=Decimal("1000"),
            approved_quote_notional=Decimal("1001"),
            reason_codes=("INVALID",),
        )
