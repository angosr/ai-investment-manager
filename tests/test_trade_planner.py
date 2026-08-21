from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from investment_manager.execution.models import Side
from investment_manager.execution.planning.planner import (
    InstrumentExecutionSpec,
    TradePlanner,
    TradePlannerPolicy,
)
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
    SleevePosition,
    SleeveTarget,
)
from investment_manager.risk.portfolio import (
    ApprovedPortfolioTarget,
    ApprovedSleeve,
)

NOW = datetime(2026, 8, 20, 11, tzinfo=UTC)
HASH = "a" * 64


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


def _sleeve_id() -> str:
    return SleeveTarget.identity_for(
        portfolio_id="primary",
        forecast_family="delta-neutral-funding-carry",
        forecast_target_id=_target().target_id,
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


def _account(
    *,
    gross: str = "0",
    pending: tuple[str, ...] = (),
) -> PortfolioAccountSnapshot:
    gross_value = Decimal(gross)
    positions: tuple[InstrumentPosition, ...] = ()
    sleeves: tuple[SleevePosition, ...] = ()
    if gross_value > 0:
        quantity = gross_value / Decimal("2") / Decimal("100")
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
        pending_execution_group_ids=pending,
    )


def _approved(
    account: PortfolioAccountSnapshot,
    *,
    desired: str = "2000",
    quotes: tuple[ExecutableQuote, ...] | None = None,
) -> ApprovedPortfolioTarget:
    quotes = quotes or _quotes()
    desired_value = Decimal(desired)
    sleeve = ApprovedSleeve(
        sleeve_id=_sleeve_id(),
        forecast_family="delta-neutral-funding-carry",
        forecast_target=_target(),
        requested_gross_notional=desired_value,
        approved_gross_notional=desired_value,
        sleeve_scale=Decimal("1") if desired_value > 0 else Decimal("0"),
        risk_profile_version="carry-risk-v1",
        maximum_unhedged_notional=min(Decimal("500"), desired_value),
        maximum_unhedged_seconds=10,
        reason_codes=("TARGET_WITHIN_RISK_ENVELOPE",),
    )
    return ApprovedPortfolioTarget(
        approved_target_id="approved-1",
        target_id="target-1",
        cycle_id="cycle-1",
        portfolio_id="primary",
        policy_version="risk-v2",
        as_of=NOW,
        valid_until=NOW + timedelta(minutes=30),
        reference_equity=Decimal("10000"),
        target_hash=HASH,
        account_snapshot_id=account.snapshot_id,
        account_snapshot_hash=content_hash(account),
        quote_hashes=tuple(sorted(content_hash(item) for item in quotes)),
        risk_profile_hashes=(HASH,),
        sleeves=(sleeve,),
    )


def _specs(*, perpetual_minimum: str = "10"):
    return tuple(
        InstrumentExecutionSpec(
            instrument=instrument,
            quantity_step=Decimal("0.01"),
            minimum_order_notional=(
                Decimal(perpetual_minimum)
                if instrument.product == InstrumentProduct.USD_M_PERPETUAL
                else Decimal("10")
            ),
        )
        for instrument in _instruments()
    )


def _planner() -> TradePlanner:
    return TradePlanner(
        TradePlannerPolicy(
            version="planner-v2",
            managed_instruments=tuple(item.key for item in _instruments()),
        )
    )


def test_planner_creates_one_group_with_spot_long_and_perpetual_short() -> None:
    account = _account()
    plan = _planner().plan(
        approved=_approved(account),
        account=account,
        quotes=_quotes(),
        specs=_specs(),
        as_of=NOW,
    )

    assert len(plan.groups) == 1
    group = plan.groups[0]
    assert tuple(
        (item.instrument.product, item.side, item.reduce_only, item.quantity)
        for item in group.legs
    ) == (
        (InstrumentProduct.SPOT, Side.BUY, False, Decimal("10")),
        (
            InstrumentProduct.USD_M_PERPETUAL,
            Side.SELL,
            False,
            Decimal("10"),
        ),
    )
    assert group.maximum_unhedged_notional == Decimal("500")


def test_one_new_risk_leg_below_minimum_omits_whole_group() -> None:
    account = _account()
    plan = _planner().plan(
        approved=_approved(account),
        account=account,
        quotes=_quotes(),
        specs=_specs(perpetual_minimum="1001"),
        as_of=NOW,
    )

    assert plan.groups == ()
    assert plan.omissions[0].reason_code == (
        "GROUP_NEW_RISK_LEG_BELOW_EXECUTION_MINIMUM"
    )


def test_planner_reduces_both_long_and_short_legs() -> None:
    account = _account(gross="2000")
    plan = _planner().plan(
        approved=_approved(account, desired="1000"),
        account=account,
        quotes=_quotes(),
        specs=_specs(),
        as_of=NOW,
    )

    legs = plan.groups[0].legs
    assert tuple((item.side, item.reduce_only, item.quantity) for item in legs) == (
        (Side.SELL, True, Decimal("5")),
        (Side.BUY, True, Decimal("5")),
    )


def test_planner_rejects_quote_snapshot_drift() -> None:
    account = _account()
    approved = _approved(account)
    changed = _quotes()[0].model_copy(update={"bid": Decimal("99")})

    with pytest.raises(ValueError, match=r"报价.*不一致"):
        _planner().plan(
            approved=approved,
            account=account,
            quotes=(changed, _quotes()[1]),
            specs=_specs(),
            as_of=NOW,
        )


def test_planner_rejects_account_snapshot_drift() -> None:
    approved_account = _account()

    with pytest.raises(ValueError, match="快照不一致"):
        _planner().plan(
            approved=_approved(approved_account),
            account=_account(gross="1000"),
            quotes=_quotes(),
            specs=_specs(),
            as_of=NOW,
        )


def test_planner_waits_for_pending_group_reconciliation() -> None:
    account = _account(pending=("group-existing",))
    plan = _planner().plan(
        approved=_approved(account),
        account=account,
        quotes=_quotes(),
        specs=_specs(),
        as_of=NOW,
    )

    assert plan.groups == ()
    assert plan.omissions[0].reason_code == (
        "EXECUTION_GROUP_REQUIRES_RECONCILIATION"
    )


def test_planner_records_missing_leg_spec_without_partial_group() -> None:
    account = _account()
    plan = _planner().plan(
        approved=_approved(account),
        account=account,
        quotes=_quotes(),
        specs=(_specs()[0],),
        as_of=NOW,
    )

    assert plan.groups == ()
    assert plan.omissions[0].reason_code == "QUOTE_OR_EXECUTION_SPEC_MISSING"
