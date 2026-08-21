from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, insert, select

from investment_manager.decision_cycle.portfolio import TradePlanExecutionPipeline
from investment_manager.execution.group.accounting import (
    ProductAccountProjectionService,
    ProductAccountProjector,
)
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
from investment_manager.execution.planning.repository import SqlTradePlanStore
from investment_manager.execution.tables import (
    mock_product_orders,
    product_order_observations,
    trade_plans,
)
from investment_manager.execution.venue.observation import SqlProductOrderObservationStore
from investment_manager.execution.venue.product import ProductOrderStatus, UnknownVenueResult
from investment_manager.execution.venue.product_mock import (
    MockSubmitBehavior,
    SqlMockProductVenue,
)
from investment_manager.forecast.models import (
    ExposureDirection,
    ForecastLeg,
    ForecastTarget,
)
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.market.models import (
    ExecutableQuote,
    InstrumentId,
    InstrumentProduct,
)
from investment_manager.market.perpetual.models import (
    FundingRateType,
    FundingSettlement,
)
from investment_manager.market.repository import InMemoryMarketDataStore
from investment_manager.portfolio.repository import SqlPortfolioStore
from investment_manager.risk.portfolio import ApprovedSleeve
from investment_manager.schema import create_schema

NOW = datetime(2026, 8, 21, 5, 10, tzinfo=UTC)


class _ApprovedReader:
    def __init__(self, approved_target_id: str, sleeve: ApprovedSleeve) -> None:
        self._approved_target_id = approved_target_id
        self._sleeve = sleeve

    def for_approved_targets(self, approved_target_ids: tuple[str, ...]):
        return {
            approved_target_id: SimpleNamespace(
                approved_target=SimpleNamespace(
                    approved_target_id=approved_target_id,
                    sleeves=(self._sleeve,),
                )
            )
            for approved_target_id in approved_target_ids
            if approved_target_id == self._approved_target_id
        }


class _EmptyAccountHistory:
    def latest_account(self, *, portfolio_id: str, as_of: datetime):
        del portfolio_id, as_of
        return None


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


def _approved_sleeve(plan: TradePlan) -> ApprovedSleeve:
    planned = plan.groups[0]
    target = ForecastTarget.create(
        tuple(
            ForecastLeg(
                instrument=leg.instrument,
                direction=(
                    ExposureDirection.LONG if leg.side == Side.BUY else ExposureDirection.SHORT
                ),
                gross_weight=Decimal("0.5"),
            )
            for leg in planned.legs
        )
    )
    return ApprovedSleeve(
        sleeve_id=planned.sleeve_id,
        forecast_family="delta-neutral-funding-carry",
        forecast_target=target,
        requested_gross_notional=Decimal("2000"),
        approved_gross_notional=Decimal("2000"),
        sleeve_scale=Decimal("1"),
        risk_profile_version="carry-risk-v1",
        maximum_unhedged_notional=planned.maximum_unhedged_notional,
        maximum_unhedged_seconds=planned.maximum_unhedged_seconds,
        reason_codes=("TARGET_WITHIN_RISK_ENVELOPE",),
    )


def _quotes(plan: TradePlan, *, as_of: datetime) -> tuple[ExecutableQuote, ...]:
    return tuple(
        ExecutableQuote(
            source_quote_id=f"quote-{leg.instrument.product.value}-{as_of.timestamp()}",
            instrument=leg.instrument,
            as_of=as_of,
            observed_at=as_of,
            bid=Decimal("100"),
            bid_quantity=Decimal("100"),
            ask=Decimal("100"),
            ask_quantity=Decimal("100"),
            source="test",
        )
        for leg in plan.groups[0].legs
    )


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
    first = ExecutionGroupEngine(
        store=store,
        venue=first_venue,
        observations=SqlProductOrderObservationStore(engine),
    )
    group = first.start(plan=plan, planned=plan.groups[0], as_of=NOW)
    lost_client_id = _target_client_id(plan, 0)
    lost_leg = next(item for item in group.target_legs if item.client_order_id == lost_client_id)

    with pytest.raises(UnknownVenueResult):
        first_venue.submit(lost_leg, observed_at=NOW)

    restarted = ExecutionGroupEngine(
        store=SqlExecutionGroupStore(engine),
        venue=SqlMockProductVenue(engine),
        observations=SqlProductOrderObservationStore(engine),
    )
    recovered = restarted.run_once(group.group_id, as_of=NOW + timedelta(seconds=1))

    assert recovered.status == ExecutionGroupStatus.HEDGED
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(mock_product_orders)) == 2


def test_persisted_trade_plan_executes_and_projects_account_idempotently() -> None:
    plan = _plan(maximum_unhedged_notional="2000")
    engine = _database(plan)
    group_store = SqlExecutionGroupStore(engine)
    observations = SqlProductOrderObservationStore(engine)
    portfolio = SqlPortfolioStore(engine)
    market = InMemoryMarketDataStore()
    projector = ProductAccountProjector(
        portfolio_id="primary",
        settlement_asset="USDT",
        initial_cash=Decimal("10000"),
    )
    pipeline = TradePlanExecutionPipeline(
        plans=SqlTradePlanStore(engine),
        groups=group_store,
        engine=ExecutionGroupEngine(
            store=group_store,
            venue=SqlMockProductVenue(engine),
            observations=observations,
        ),
        accounts=ProductAccountProjectionService(
            projector=projector,
            groups=group_store,
            observations=observations,
            funding=market,
            risks=_ApprovedReader(plan.approved_target_id, _approved_sleeve(plan)),
            accounts=portfolio,
        ),
        portfolio_store=portfolio,
    )
    as_of = NOW + timedelta(seconds=1)

    result = pipeline.run(plan_id=plan.plan_id, as_of=as_of, quotes=_quotes(plan, as_of=as_of))
    replayed = pipeline.run(plan_id=plan.plan_id, as_of=as_of, quotes=_quotes(plan, as_of=as_of))

    assert replayed == result
    assert result.groups[0].status == ExecutionGroupStatus.HEDGED
    assert result.account.cash_balance == Decimal("8999")
    assert result.account.equity == Decimal("9999")
    assert result.account.daily_pnl == Decimal("-1")
    assert len(result.account.positions) == 2
    assert portfolio.account(result.account.snapshot_id) == result.account

    perpetual = next(
        leg.instrument
        for leg in plan.groups[0].legs
        if leg.instrument.product == InstrumentProduct.USD_M_PERPETUAL
    )
    funding_time = NOW + timedelta(seconds=2)
    market.put_funding_settlement(
        FundingSettlement(
            settlement_id=stable_id(
                "funding_settlement",
                perpetual.key,
                funding_time.isoformat(),
                FundingRateType.REGULAR.value,
            ),
            instrument=perpetual,
            funding_time=funding_time,
            observed_at=NOW + timedelta(seconds=3),
            funding_rate=Decimal("0.001"),
            mark_price=Decimal("100"),
            rate_type=FundingRateType.REGULAR,
            source="test",
        )
    )
    after_funding = pipeline.run(
        plan_id=plan.plan_id,
        as_of=NOW + timedelta(seconds=4),
        quotes=_quotes(plan, as_of=NOW + timedelta(seconds=4)),
    )
    assert after_funding.account.cash_balance == Decimal("9000")
    assert after_funding.account.equity == Decimal("10000")
    assert after_funding.account.daily_pnl == Decimal("0")


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
        observations=SqlProductOrderObservationStore(engine),
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
        observations=SqlProductOrderObservationStore(engine),
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
    observations = SqlProductOrderObservationStore(engine)
    executor = ExecutionGroupEngine(
        store=store,
        venue=venue,
        observations=observations,
    )
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

    recovery = TradePlanExecutionPipeline(
        plans=SimpleNamespace(),
        groups=store,
        engine=executor,
        accounts=SimpleNamespace(),
        portfolio_store=SimpleNamespace(),
    )
    recovered = recovery.recover_pending(as_of=NOW + timedelta(seconds=11))
    completed = recovered[0]

    assert tuple(item.group_id for item in recovered) == (group.group_id,)
    assert completed.status == ExecutionGroupStatus.FLAT
    assert completed.residual_quantities == {}
    visible_at_partial = observations.for_group(
        group.group_id,
        as_of=NOW + timedelta(seconds=1),
    )
    assert any(
        item.order.status == ProductOrderStatus.PARTIALLY_FILLED for item in visible_at_partial
    )
    projector = ProductAccountProjector(
        portfolio_id="primary",
        settlement_asset="USDT",
        initial_cash=Decimal("10000"),
    )
    partial_account = projector.project(
        cycle_id="projection-partial",
        as_of=NOW + timedelta(seconds=1),
        groups=(completed,),
        observation_history_by_group={
            group.group_id: observations.history_for_groups(
                (group.group_id,),
                as_of=NOW + timedelta(seconds=1),
            )[group.group_id]
        },
        funding_settlements=(),
        approved_sleeves=(_approved_sleeve(plan),),
        quotes=_quotes(plan, as_of=NOW + timedelta(seconds=1)),
    )
    service_account = ProductAccountProjectionService(
        projector=projector,
        groups=store,
        observations=observations,
        funding=InMemoryMarketDataStore(),
        risks=_ApprovedReader(plan.approved_target_id, _approved_sleeve(plan)),
        accounts=_EmptyAccountHistory(),
    ).project(
        cycle_id="projection-partial",
        as_of=NOW + timedelta(seconds=1),
        quotes=_quotes(plan, as_of=NOW + timedelta(seconds=1)),
    )
    flat_account = projector.project(
        cycle_id="projection-flat",
        as_of=NOW + timedelta(seconds=11),
        groups=(completed,),
        observation_history_by_group={
            group.group_id: observations.history_for_groups(
                (group.group_id,),
                as_of=NOW + timedelta(seconds=11),
            )[group.group_id]
        },
        funding_settlements=(),
        approved_sleeves=(_approved_sleeve(plan),),
        quotes=_quotes(plan, as_of=NOW + timedelta(seconds=11)),
        previous=partial_account,
    )
    assert partial_account.pending_execution_group_ids == (group.group_id,)
    assert service_account == partial_account
    assert len(partial_account.positions) == 2
    assert flat_account.pending_execution_group_ids == ()
    assert flat_account.positions == ()
    assert flat_account.sleeves == ()
    assert flat_account.equity == Decimal("9998.5")
    assert flat_account.daily_pnl == Decimal("-1.5")
    with engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(product_order_observations)
        ) > connection.scalar(select(func.count()).select_from(mock_product_orders))
