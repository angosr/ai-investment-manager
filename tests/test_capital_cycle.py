from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, func, insert, select

from investment_manager.decision_cycle.capital import assemble_capital_cycle
from investment_manager.decision_cycle.portfolio import TradePlanExecutionResult
from investment_manager.entrypoints.dashboard.capital import (
    CapitalDashboardReader,
    serialize_capital_activity,
    serialize_capital_overview,
)
from investment_manager.execution.tables import mock_product_orders, trade_plans
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.market.models import InstrumentProduct, MarketQuote
from investment_manager.market.perpetual.models import (
    FundingRateType,
    FundingSettlement,
    PerpetualMarketState,
    PerpetualQuote,
)
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.portfolio.models import CapitalCycleRecord, PortfolioEdgeBasis
from investment_manager.portfolio.repository import SqlPortfolioStore
from investment_manager.portfolio.tables import (
    capital_cycle_records,
    portfolio_performance_intervals,
    portfolio_targets,
)
from investment_manager.risk.portfolio import HoldingRiskOutcome, PortfolioHoldingRiskReview
from investment_manager.risk.tables import (
    portfolio_holding_risk_reviews,
    portfolio_risk_decisions,
)
from investment_manager.scheduling.tables import analysis_trigger_batches
from investment_manager.schema import create_schema
from investment_manager.settings import load_config

NOW = datetime(2026, 9, 1, 0, 5, tzinfo=UTC)


def _put_market(
    market: SqlMarketDataStore,
    config,
    *,
    at: datetime,
    sequence: int,
    spot_bid: str = "99990",
    spot_ask: str = "100000",
    perpetual_bid: str = "100300",
    perpetual_ask: str = "100310",
) -> None:
    market.put_quote(
        MarketQuote(
            quote_id=f"spot-capital-quote-{sequence}",
            symbol="BTCUSDT",
            observed_at=at,
            bid=Decimal(spot_bid),
            bid_quantity=Decimal("2"),
            ask=Decimal(spot_ask),
            ask_quantity=Decimal("2"),
            source="test",
        )
    )

    perpetual = next(
        item.instrument
        for item in config.capital.execution_specs
        if item.instrument.product == InstrumentProduct.USD_M_PERPETUAL
    )
    market.put_perpetual_quote(
        PerpetualQuote(
            quote_id=stable_id("perpetual_quote", perpetual.key, sequence),
            instrument=perpetual,
            exchange_time=at,
            observed_at=at,
            bid=Decimal(perpetual_bid),
            bid_quantity=Decimal("2"),
            ask=Decimal(perpetual_ask),
            ask_quantity=Decimal("2"),
            update_id=sequence,
            source="test",
        )
    )
    market.put_perpetual_state(
        PerpetualMarketState(
            state_id=stable_id(
                "perpetual_market_state",
                perpetual.key,
                at.isoformat(),
            ),
            instrument=perpetual,
            exchange_time=at,
            observed_at=at,
            mark_price=Decimal(perpetual_bid),
            index_price=Decimal(spot_ask),
            last_funding_rate=Decimal("0.0001"),
            interest_rate=Decimal("0.0001"),
            next_funding_time=at + timedelta(hours=4),
            source="test",
        )
    )


def _put_trigger_batch(engine, config, *, at: datetime, sequence: int) -> None:
    batch_id = f"capital-test-batch-{sequence}"
    with engine.begin() as connection:
        connection.execute(
            insert(analysis_trigger_batches).values(
                batch_id=batch_id,
                symbol="BTCUSDT",
                pipeline_id=config.pipeline.version,
                plan_revision=1,
                first_occurred_at=at,
                first_observed_at=at,
                batched_at=at,
                analysis_submitted_at=at,
                payload={
                    "batch_id": batch_id,
                    "triggers": [{"trigger_type": "HEARTBEAT"}],
                },
            )
        )


def _put_funding_history(
    market: SqlMarketDataStore,
    config,
    *,
    at: datetime,
    rates: tuple[str, str, str] = ("0.0003", "0.0002", "0.0001"),
) -> None:
    perpetual = next(
        item.instrument
        for item in config.capital.execution_specs
        if item.instrument.product == InstrumentProduct.USD_M_PERPETUAL
    )
    for hours, rate in zip((24, 16, 8), rates, strict=True):
        funding_at = at - timedelta(hours=hours)
        market.put_funding_settlement(
            FundingSettlement(
                settlement_id=stable_id(
                    "funding_settlement",
                    perpetual.key,
                    funding_at.isoformat(),
                    FundingRateType.REGULAR.value,
                ),
                instrument=perpetual,
                funding_time=funding_at,
                observed_at=funding_at + timedelta(seconds=1),
                funding_rate=rate,
                mark_price="100000",
                rate_type=FundingRateType.REGULAR,
                source="test",
            )
        )


def test_capital_cycle_turns_monthly_released_carry_into_idempotent_mock_trade() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=NOW, sequence=7)
    service = assemble_capital_cycle(config, engine)

    first = service.produce(
        as_of=NOW,
        cause_id="capital-test-batch-1",
        trigger_batch_id="capital-test-batch-1",
        symbol="BTCUSDT",
        trigger_types=("HEARTBEAT",),
    )
    replay = service.produce(
        as_of=NOW,
        cause_id="capital-test-batch-1",
        trigger_batch_id="capital-test-batch-1",
        symbol="BTCUSDT",
        trigger_types=("HEARTBEAT",),
    )
    same_time_other_batch = service.produce(
        as_of=NOW,
        cause_id="capital-test-batch-other-symbol",
        trigger_batch_id="capital-test-batch-other-symbol",
        symbol="ETHUSDT",
        trigger_types=("MARKET_SHOCK",),
    )
    _put_trigger_batch(engine, config, at=NOW, sequence=1)

    assert isinstance(first, TradePlanExecutionResult)
    assert replay == first
    assert same_time_other_batch == first
    assert len(first.groups) == 1
    assert first.groups[0].terminal
    assert first.groups[0].valid_until == NOW.replace(minute=30)
    assert first.account.equity < Decimal("10000")
    assert first.account.equity == Decimal("9996.91685")
    assert first.account.revision == 1
    assert content_hash(first.account) == content_hash(
        first.account.model_copy(update={"revision": 0})
    )
    assert {abs(item.quantity) for item in first.account.positions} == {Decimal("0.014")}
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(mock_product_orders)) == 2
        assert (
            connection.scalar(select(func.count()).select_from(portfolio_performance_intervals))
            == 1
        )
        assert connection.scalar(select(func.count()).select_from(capital_cycle_records)) == 2

    overview = CapitalDashboardReader(engine, config).overview(now=NOW)
    assert (
        SqlPortfolioStore(engine).latest_account(
            portfolio_id=config.capital.decision.portfolio_id,
            as_of=NOW,
        )
        == first.account
    )
    dto = serialize_capital_overview(overview)
    assert dto["account"]["equity"] == "9996.91685"
    assert dto["decision"]["risk_outcome"] == "APPROVED"
    assert dto["execution"] == {
        "active_group_count": 0,
        "active_groups": [],
        "total_order_count": 2,
    }
    assert dto["performance"]["interval_count"] == 1
    assert dto["performance"]["cumulative_net_pnl"] == "-3.08315"
    assert dto["performance"]["latest"]["kind"] == "EXECUTION"
    assert dto["performance"]["latest"]["net_pnl"] == "-3.08315"
    activity = CapitalDashboardReader(engine, config).activity()
    assert len(activity) == 2
    activity_by_symbol = {item.symbol: item for item in activity}
    assert activity_by_symbol["ETHUSDT"].outcome == "OPPORTUNITY_ALREADY_DECIDED"
    assert activity_by_symbol["ETHUSDT"].order_count == 0
    assert activity_by_symbol["ETHUSDT"].trigger_types == ("MARKET_SHOCK",)
    assert activity_by_symbol["BTCUSDT"].outcome == "EXECUTED"
    assert activity_by_symbol["BTCUSDT"].trigger_types == ("HEARTBEAT",)


def test_dynamic_mock_candidate_can_trade_outside_monthly_window_via_same_chain() -> None:
    at = datetime(2026, 8, 21, 18, 5, tzinfo=UTC)
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=at, sequence=70)
    _put_funding_history(market, config, at=at)

    result = assemble_capital_cycle(config, engine, forecast_clock=lambda: at).produce(
        as_of=at,
        cause_id="dynamic-carry-batch",
        trigger_batch_id="dynamic-carry-batch",
        symbol="BTCUSDT",
        trigger_types=("HEARTBEAT",),
    )

    assert isinstance(result, TradePlanExecutionResult)
    assert result.groups and result.groups[0].terminal
    target = SqlPortfolioStore(engine).target_for_cycle(result.groups[0].cycle_id)
    assert target is not None
    assert target.sleeves[0].edge_basis == PortfolioEdgeBasis.MOCK_HYPOTHESIS
    assert target.sleeves[0].decision_net_bps > Decimal("5")
    assert target.sleeves[0].desired_gross_notional == Decimal("3000")
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(mock_product_orders)) == 2


def test_unprofitable_dynamic_candidate_explains_cash_without_fake_rebalance() -> None:
    at = datetime(2026, 8, 21, 18, 5, tzinfo=UTC)
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=at, sequence=71)
    _put_funding_history(
        market,
        config,
        at=at,
        rates=("0.00001", "0.00001", "0.00001"),
    )

    result = assemble_capital_cycle(config, engine, forecast_clock=lambda: at).produce(
        as_of=at,
        cause_id="unprofitable-dynamic-carry-batch",
        trigger_batch_id="unprofitable-dynamic-carry-batch",
        symbol="BTCUSDT",
        trigger_types=("HEARTBEAT",),
    )

    assert result.trade_plan is not None
    assert result.trade_plan.groups == ()
    activity = CapitalDashboardReader(engine, config).activity()[0]
    assert activity.reason_codes == ("CASH_SELECTED_NO_ELIGIBLE_FORECAST",)
    assert len(activity.candidate_economics) == 1
    economics = activity.candidate_economics[0]
    assert economics.net_bps < economics.entry_threshold_bps
    serialized = serialize_capital_activity((activity,))["actions"][0]
    assert serialized["candidate_economics"][0]["net_bps"] == str(
        economics.net_bps
    )


def test_capital_cycle_decides_at_forecast_availability_not_trigger_creation() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=NOW, sequence=8)
    available_at = NOW + timedelta(seconds=5)
    service = assemble_capital_cycle(
        config,
        engine,
        forecast_clock=lambda: available_at,
    )

    result = service.produce(
        as_of=NOW,
        cause_id="delayed-capital-batch",
        trigger_batch_id="delayed-capital-batch",
        symbol="BTCUSDT",
        trigger_types=("HEARTBEAT",),
    )

    assert isinstance(result, TradePlanExecutionResult)
    assert result.account.as_of == available_at
    with engine.connect() as connection:
        payload = connection.execute(select(capital_cycle_records.c.payload)).scalar_one()
    record = CapitalCycleRecord.model_validate(payload)
    assert record.triggered_at == NOW
    assert record.evaluated_at == available_at


def test_capital_cycle_uses_forecast_opportunity_identity_and_holds_without_one() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=NOW, sequence=1)
    service = assemble_capital_cycle(config, engine)

    opened = service.produce(as_of=NOW)
    assert isinstance(opened, TradePlanExecutionResult)

    heartbeat = NOW + timedelta(minutes=15)
    _put_market(
        market,
        config,
        at=heartbeat,
        sequence=2,
        spot_bid="105000",
        spot_ask="105010",
        perpetual_bid="105310",
        perpetual_ask="105320",
    )
    held = service.produce(as_of=heartbeat)

    missed = datetime(2026, 10, 1, 0, 31, tzinfo=UTC)
    _put_market(
        market,
        config,
        at=missed,
        sequence=3,
        spot_bid="103000",
        spot_ask="103010",
        perpetual_bid="103280",
        perpetual_ask="103290",
    )
    after_restart = assemble_capital_cycle(config, engine).produce(
        as_of=missed,
        cause_id="capital-test-batch-2",
        trigger_batch_id="capital-test-batch-2",
        symbol="BTCUSDT",
        trigger_types=("HEARTBEAT",),
    )
    _put_trigger_batch(engine, config, at=missed, sequence=2)

    assert held.outcome.value == "NO_CHANGE"
    assert after_restart.outcome.value == "NO_CHANGE"
    overview = CapitalDashboardReader(engine, config).overview(now=missed)
    dto = serialize_capital_overview(overview)
    assert dto["decision"] == {
        "as_of": missed.isoformat(),
        "mode": "NO_CHANGE",
        "reason_codes": ["NO_NEW_OPPORTUNITY_HOLDING_REVIEWED"],
        "risk_outcome": None,
    }
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(portfolio_targets)) == 1
        assert connection.scalar(select(func.count()).select_from(mock_product_orders)) == 2
    activity = CapitalDashboardReader(engine, config).activity()
    assert activity[0].outcome == "HOLD"
    assert activity[0].reason_codes == ("NO_NEW_OPPORTUNITY_HOLDING_REVIEWED",)


def test_risk_forced_cash_is_not_retried_later_in_the_month() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    loaded = load_config("config/investment-manager.shadow.yaml")
    config = loaded.model_copy(
        update={
            "capital": loaded.capital.model_copy(
                update={"risk": loaded.capital.risk.model_copy(update={"kill_switch": True})}
            )
        }
    )
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=NOW, sequence=10)
    service = assemble_capital_cycle(config, engine)

    forced_cash = service.produce(as_of=NOW)
    later = NOW + timedelta(minutes=15)
    _put_market(market, config, at=later, sequence=11)
    held = service.produce(as_of=later)

    assert forced_cash.outcome.value == "PLANNED"
    assert forced_cash.trade_plan is not None
    assert forced_cash.trade_plan.groups == ()
    assert held.outcome.value == "NO_CHANGE"
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(portfolio_targets)) == 1
        assert connection.scalar(select(func.count()).select_from(portfolio_risk_decisions)) == 1
        assert connection.scalar(select(func.count()).select_from(trade_plans)) == 1
        assert connection.scalar(select(func.count()).select_from(mock_product_orders)) == 0


def test_holding_kill_switch_exits_through_the_normal_grouped_chain() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    loaded = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    _put_market(market, loaded, at=NOW, sequence=20)
    opened = assemble_capital_cycle(loaded, engine).produce(as_of=NOW)
    assert isinstance(opened, TradePlanExecutionResult)
    assert opened.account.positions

    protected = loaded.model_copy(
        update={
            "capital": loaded.capital.model_copy(
                update={"risk": loaded.capital.risk.model_copy(update={"kill_switch": True})}
            )
        }
    )
    heartbeat = NOW + timedelta(minutes=15)
    _put_market(market, protected, at=heartbeat, sequence=21)
    exited = assemble_capital_cycle(protected, engine).produce(as_of=heartbeat)

    assert isinstance(exited, TradePlanExecutionResult)
    assert not exited.account.positions
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(mock_product_orders)) == 4
        payload = connection.execute(select(portfolio_holding_risk_reviews.c.payload)).scalar_one()
    review = PortfolioHoldingRiskReview.model_validate(payload)
    assert review.outcome == HoldingRiskOutcome.EXIT
