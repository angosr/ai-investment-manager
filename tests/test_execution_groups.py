from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, insert, select

from investment_manager.execution.group.engine import ExecutionGroupEngine
from investment_manager.execution.group.models import (
    ExecutionGroupStatus,
    ExecutionLegRole,
    client_order_id,
    execution_leg_id,
)
from investment_manager.execution.group.repository import SqlExecutionGroupStore
from investment_manager.execution.models import Side
from investment_manager.execution.planning.planner import (
    PlannedLegTrade,
    PlannedTradeGroup,
    TradePlan,
)
from investment_manager.execution.tables import mock_product_orders, trade_plans
from investment_manager.execution.venue.product import UnknownVenueResult
from investment_manager.execution.venue.product_mock import (
    MockSubmitBehavior,
    SqlMockProductVenue,
)
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.market.models import InstrumentId, InstrumentProduct
from investment_manager.schema import create_schema

NOW = datetime(2026, 8, 21, 5, 10, tzinfo=UTC)


def _plan(
    *,
    identity: str = "one",
    maximum_unhedged_notional: str = "600",
) -> TradePlan:
    approved_id = f"approved-{identity}"
    cycle_id = f"cycle-{identity}"
    sleeve_id = "sleeve-carry"
    planner_version = "planner-v2"
    group_id = stable_id("trade_group", approved_id, sleeve_id, planner_version)
    instruments = (
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
    legs = []
    for instrument, side in zip(instruments, (Side.BUY, Side.SELL), strict=True):
        quantity = Decimal("10")
        price = Decimal("100")
        leg_id = stable_id(
            "planned_leg",
            group_id,
            instrument.key,
            side.value,
            str(quantity),
            "False",
        )
        legs.append(
            PlannedLegTrade(
                leg_id=leg_id,
                group_id=group_id,
                approved_target_id=approved_id,
                cycle_id=cycle_id,
                sleeve_id=sleeve_id,
                instrument=instrument,
                side=side,
                quantity=quantity,
                reference_price=price,
                quote_notional=quantity * price,
                reduce_only=False,
                valid_until=NOW + timedelta(minutes=5),
            )
        )
    group = PlannedTradeGroup(
        group_id=group_id,
        approved_target_id=approved_id,
        cycle_id=cycle_id,
        sleeve_id=sleeve_id,
        planner_policy_version=planner_version,
        desired_gross_notional=Decimal("2000"),
        maximum_unhedged_notional=Decimal(maximum_unhedged_notional),
        maximum_unhedged_seconds=10,
        legs=tuple(legs),
        valid_until=NOW + timedelta(minutes=5),
    )
    values = {
        "plan_id": stable_id("trade_plan", approved_id),
        "approved_target_id": approved_id,
        "cycle_id": cycle_id,
        "planner_policy_version": planner_version,
        "created_at": NOW,
        "target_deltas": (),
        "groups": (group,),
        "omissions": (),
    }
    return TradePlan(**values, plan_hash=content_hash(values))


def _database(plan: TradePlan):
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(trade_plans).values(
                plan_id=plan.plan_id,
                approved_target_id=plan.approved_target_id,
                cycle_id=plan.cycle_id,
                created_at=plan.created_at,
                plan_hash=plan.plan_hash,
                payload=plan.model_dump(mode="json"),
            )
        )
    return engine


def _target_client_id(plan: TradePlan, index: int) -> str:
    planned_leg_id = plan.groups[0].legs[index].leg_id
    identity = execution_leg_id(
        planned_leg_id=planned_leg_id,
        role=ExecutionLegRole.TARGET,
        attempt=0,
    )
    return client_order_id(identity)


def _compensation_client_id(plan: TradePlan, index: int, attempt: int = 1) -> str:
    planned_leg_id = plan.groups[0].legs[index].leg_id
    identity = execution_leg_id(
        planned_leg_id=planned_leg_id,
        role=ExecutionLegRole.COMPENSATION,
        attempt=attempt,
    )
    return client_order_id(identity)


def test_response_lost_after_accept_recovers_without_duplicate_after_restart() -> None:
    plan = _plan()
    engine = _database(plan)
    store = SqlExecutionGroupStore(engine)
    first_venue = SqlMockProductVenue(
        engine,
        submit_behaviors={
            _target_client_id(plan, 0): (MockSubmitBehavior.AFTER_ACCEPT_RESPONSE_LOST,)
        },
    )
    first = ExecutionGroupEngine(store=store, venue=first_venue)
    group = first.start(plan=plan, planned=plan.groups[0], as_of=NOW)
    lost_client_id = _target_client_id(plan, 0)
    lost_leg = next(item for item in group.target_legs if item.client_order_id == lost_client_id)

    with pytest.raises(UnknownVenueResult):
        first_venue.submit(lost_leg, observed_at=NOW)

    restarted = ExecutionGroupEngine(
        store=SqlExecutionGroupStore(engine),
        venue=SqlMockProductVenue(engine),
    )
    recovered = restarted.run_once(group.group_id, as_of=NOW + timedelta(seconds=1))

    assert recovered.status == ExecutionGroupStatus.HEDGED
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(mock_product_orders)) == 2


def test_response_lost_before_accept_retries_same_identity() -> None:
    plan = _plan(maximum_unhedged_notional="2000")
    engine = _database(plan)
    unknown_client_id = _target_client_id(plan, 0)
    venue = SqlMockProductVenue(
        engine,
        submit_behaviors={unknown_client_id: (MockSubmitBehavior.BEFORE_ACCEPT_RESPONSE_LOST,)},
    )
    executor = ExecutionGroupEngine(
        store=SqlExecutionGroupStore(engine),
        venue=venue,
    )
    group = executor.start(plan=plan, planned=plan.groups[0], as_of=NOW)

    recovering = executor.run_once(group.group_id, as_of=NOW + timedelta(seconds=1))

    assert recovering.status == ExecutionGroupStatus.RECOVERING
    assert venue.query(unknown_client_id) is None
    completed = executor.run_once(group.group_id, as_of=NOW + timedelta(seconds=2))

    assert completed.status == ExecutionGroupStatus.HEDGED
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(mock_product_orders)) == 2


def test_rejected_target_and_failed_compensation_retry_until_flat() -> None:
    plan = _plan()
    engine = _database(plan)
    reject_target = 0
    filled_target = 1
    venue = SqlMockProductVenue(
        engine,
        submit_behaviors={
            _target_client_id(plan, reject_target): (MockSubmitBehavior.REJECT,),
            _compensation_client_id(plan, filled_target): (MockSubmitBehavior.REJECT,),
        },
    )
    executor = ExecutionGroupEngine(
        store=SqlExecutionGroupStore(engine),
        venue=venue,
    )
    group = executor.start(plan=plan, planned=plan.groups[0], as_of=NOW)

    recovering = executor.run_once(group.group_id, as_of=NOW + timedelta(seconds=1))
    completed = executor.run_once(group.group_id, as_of=NOW + timedelta(seconds=2))

    assert recovering.status == ExecutionGroupStatus.COMPENSATING
    assert recovering.residual_quantities
    assert completed.status == ExecutionGroupStatus.FLAT
    assert completed.residual_quantities == {}
    assert len(completed.compensation_legs) == 2


def test_partial_fill_blocks_same_sleeve_then_time_limit_forces_flat() -> None:
    plan = _plan()
    engine = _database(plan)
    venue = SqlMockProductVenue(
        engine,
        submit_behaviors={_target_client_id(plan, 0): (MockSubmitBehavior.PARTIAL_FILL,)},
    )
    store = SqlExecutionGroupStore(engine)
    executor = ExecutionGroupEngine(store=store, venue=venue)
    group = executor.start(plan=plan, planned=plan.groups[0], as_of=NOW)

    partial = executor.run_once(group.group_id, as_of=NOW + timedelta(seconds=1))

    assert partial.status == ExecutionGroupStatus.RECOVERING
    assert partial.unhedged_notional == Decimal("500")
    second_plan = _plan(identity="two")
    with engine.begin() as connection:
        connection.execute(
            insert(trade_plans).values(
                plan_id=second_plan.plan_id,
                approved_target_id=second_plan.approved_target_id,
                cycle_id=second_plan.cycle_id,
                created_at=second_plan.created_at,
                plan_hash=second_plan.plan_hash,
                payload=second_plan.model_dump(mode="json"),
            )
        )
    with pytest.raises(ValueError, match="相同 Sleeve"):
        executor.start(
            plan=second_plan,
            planned=second_plan.groups[0],
            as_of=NOW + timedelta(seconds=2),
        )

    completed = executor.run_once(group.group_id, as_of=NOW + timedelta(seconds=11))

    assert completed.status == ExecutionGroupStatus.FLAT
    assert completed.residual_quantities == {}
