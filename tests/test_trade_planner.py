from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from quant_core.domain import AccountSnapshot, Position, Side
from quant_core.ids import content_hash
from quant_core.portfolio_risk import ApprovedAssetTarget, ApprovedTarget
from quant_core.trade_planner import (
    MarketExecutionSpec,
    TradePlanner,
    TradePlannerPolicy,
)

NOW = datetime(2026, 8, 20, 11, tzinfo=UTC)
HASH = "a" * 64


def _account(
    *,
    btc_quantity: str = "0",
    eth_quantity: str = "0",
    open_order_count: int = 0,
) -> AccountSnapshot:
    positions = tuple(
        Position(
            symbol=symbol,
            quantity=Decimal(quantity),
            average_price=Decimal("100"),
        )
        for symbol, quantity in (
            ("BTCUSDT", btc_quantity),
            ("ETHUSDT", eth_quantity),
        )
        if Decimal(quantity) != 0
    )
    return AccountSnapshot(
        cycle_id="cycle-1",
        as_of=NOW,
        observed_at=NOW,
        quote_balance=Decimal("10000"),
        positions=positions,
        open_order_count=open_order_count,
        equity=Decimal("10000"),
        reconciled=True,
    )


def _approved(
    account: AccountSnapshot,
    *,
    symbol: str = "BTCUSDT",
    desired: str = "1000",
    market_hash: str = HASH,
) -> ApprovedTarget:
    return ApprovedTarget(
        approved_target_id="approved-1",
        target_id="target-1",
        cycle_id="cycle-1",
        portfolio_id="primary",
        policy_version="risk-v1",
        as_of=NOW,
        valid_until=NOW + timedelta(minutes=30),
        reference_equity=Decimal("10000"),
        target_hash=HASH,
        account_snapshot_hash=content_hash(account),
        market_snapshot_hashes=(market_hash,),
        targets=(
            ApprovedAssetTarget(
                symbol=symbol,
                requested_quote_notional=Decimal(desired),
                approved_quote_notional=Decimal(desired),
                protective_stop_price=Decimal("95"),
                reason_codes=("TARGET_WITHIN_RISK_ENVELOPE",),
            ),
        ),
    )


def _planner() -> TradePlanner:
    return TradePlanner(
        TradePlannerPolicy(
            version="planner-v1",
            managed_symbols=("BTCUSDT", "ETHUSDT"),
        )
    )


def _markets(replay_input):
    btc = replay_input.market.model_copy(
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
    return (
        btc,
        btc.model_copy(update={"symbol": "ETHUSDT"}),
    )


def _specs():
    return tuple(
        MarketExecutionSpec(
            symbol=symbol,
            quantity_step=Decimal("0.01"),
            minimum_order_notional=Decimal("10"),
        )
        for symbol in ("BTCUSDT", "ETHUSDT")
    )


def test_planner_translates_approved_increase_to_buy(replay_input) -> None:
    account = _account()
    markets = _markets(replay_input)
    plan = _planner().plan(
        approved=_approved(account, market_hash=content_hash(markets[0])),
        account=account,
        markets=markets,
        specs=_specs(),
        as_of=NOW,
    )

    assert len(plan.trades) == 1
    trade = plan.trades[0]
    assert trade.side == Side.BUY
    assert not trade.reduce_only
    assert trade.quantity == Decimal("10")
    assert trade.quote_notional == Decimal("1000")
    assert trade.protective_stop_price == Decimal("95")


def test_planner_reduces_non_target_position_before_buy(replay_input) -> None:
    account = _account(eth_quantity="5")
    markets = _markets(replay_input)
    plan = _planner().plan(
        approved=_approved(account, market_hash=content_hash(markets[0])),
        account=account,
        markets=markets,
        specs=_specs(),
        as_of=NOW,
    )

    assert tuple((item.symbol, item.side) for item in plan.trades) == (
        ("ETHUSDT", Side.SELL),
        ("BTCUSDT", Side.BUY),
    )
    assert plan.trades[0].reduce_only
    assert plan.trades[0].protective_stop_price is None


def test_planner_never_sells_more_than_current_position(replay_input) -> None:
    account = _account(btc_quantity="3")
    approved = _approved(account, desired="0")
    plan = _planner().plan(
        approved=approved,
        account=account,
        markets=_markets(replay_input),
        specs=_specs(),
        as_of=NOW,
    )

    assert len(plan.trades) == 1
    assert plan.trades[0].side == Side.SELL
    assert plan.trades[0].quantity == Decimal("3")


def test_planner_records_below_minimum_delta_instead_of_hiding_it(
    replay_input,
) -> None:
    account = _account()
    markets = _markets(replay_input)
    plan = _planner().plan(
        approved=_approved(
            account,
            desired="9",
            market_hash=content_hash(markets[0]),
        ),
        account=account,
        markets=markets,
        specs=_specs(),
        as_of=NOW,
    )

    assert plan.trades == ()
    assert len(plan.omissions) == 1
    assert plan.omissions[0].reason_code == "DELTA_BELOW_EXECUTION_MINIMUM"


def test_planner_rejects_account_snapshot_drift(replay_input) -> None:
    approved_account = _account()
    changed = _account(btc_quantity="1")

    with pytest.raises(ValueError, match="快照不一致"):
        _planner().plan(
            approved=_approved(approved_account),
            account=changed,
            markets=_markets(replay_input),
            specs=_specs(),
            as_of=NOW,
        )


def test_planner_rejects_market_snapshot_drift_for_new_risk(replay_input) -> None:
    account = _account()

    with pytest.raises(ValueError, match=r"行情.*不一致"):
        _planner().plan(
            approved=_approved(account),
            account=account,
            markets=_markets(replay_input),
            specs=_specs(),
            as_of=NOW,
        )


def test_planner_waits_for_reconciliation_when_orders_are_open(
    replay_input,
) -> None:
    account = _account(open_order_count=1)
    markets = _markets(replay_input)
    plan = _planner().plan(
        approved=_approved(account, market_hash=content_hash(markets[0])),
        account=account,
        markets=markets,
        specs=_specs(),
        as_of=NOW,
    )

    assert plan.trades == ()
    assert plan.omissions[0].reason_code == "OPEN_ORDERS_REQUIRE_RECONCILIATION"


def test_planner_rejects_open_position_without_market(replay_input) -> None:
    account = _account(eth_quantity="1")
    markets = _markets(replay_input)

    with pytest.raises(ValueError, match="已有持仓缺少行情"):
        _planner().plan(
            approved=_approved(account, market_hash=content_hash(markets[0])),
            account=account,
            markets=(markets[0],),
            specs=_specs(),
            as_of=NOW,
        )
