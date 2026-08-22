from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, func, insert, select

from investment_manager.decision_cycle.capital import (
    CapitalForecastSource,
    assemble_capital_cycle,
)
from investment_manager.decision_cycle.portfolio import TradePlanExecutionResult
from investment_manager.entrypoints.dashboard.capital import (
    CapitalDashboardReader,
    serialize_capital_activity,
    serialize_capital_overview,
)
from investment_manager.entrypoints.dashboard.pagination import PageCursor
from investment_manager.execution.tables import mock_product_orders, trade_plans
from investment_manager.forecast.carry import _carry_target
from investment_manager.forecast.models import (
    BaseForecast,
    DirectionalView,
    ForecastReferencePrice,
)
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.market.models import InstrumentProduct, MarketQuote
from investment_manager.market.perpetual.models import (
    FundingRateType,
    FundingSettlement,
    PerpetualMarketState,
    PerpetualQuote,
)
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.portfolio.models import (
    CapitalCycleRecord,
    MockCandidateAuthorization,
    PortfolioEdgeBasis,
)
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

_TEST_PRODUCER_ID = "test-capital-candidate"
_TEST_PRODUCER_VERSION = "test-capital-candidate-v1"
_TEST_FORECAST_FAMILY = "test-delta-neutral-candidate"


@dataclass(frozen=True)
class _FixedMockForecastProducer:
    store: SqlForecastStore
    raw_score: Decimal
    available_delay_seconds: int = 0

    def produce(self, *, as_of: datetime) -> BaseForecast:
        available_at = as_of + timedelta(seconds=self.available_delay_seconds)
        target = _carry_target(
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
        )
        reference_prices = tuple(
            ForecastReferencePrice(
                instrument_id=item.instrument.key,
                price=(
                    Decimal("100000")
                    if item.instrument.product == InstrumentProduct.SPOT
                    else Decimal("100300")
                ),
            )
            for item in target.legs
        )
        forecast_id = stable_id(
            "base_forecast",
            _TEST_PRODUCER_ID,
            _TEST_PRODUCER_VERSION,
            target.target_id,
            available_at,
            self.raw_score,
        )
        existing = self.store.forecast(forecast_id)
        if existing is not None:
            assert isinstance(existing, BaseForecast)
            return existing
        forecast = BaseForecast(
            forecast_id=forecast_id,
            producer_id=_TEST_PRODUCER_ID,
            producer_version=_TEST_PRODUCER_VERSION,
            forecast_family=_TEST_FORECAST_FAMILY,
            target=target,
            horizon_minutes=7 * 24 * 60,
            direction=DirectionalView.UP,
            reference_prices=reference_prices,
            observed_at=as_of,
            available_at=available_at,
            valid_until=available_at + timedelta(minutes=30),
            raw_score=self.raw_score,
            input_refs=(stable_id("test_candidate_input", as_of),),
            unknowns=("TEST_FORECAST",),
        )
        self.store.record(forecast)
        return forecast


class _NoForecastProducer:
    def produce(self, *, as_of: datetime) -> None:
        return None


def _candidate_service(
    config,
    engine,
    *,
    raw_score: Decimal = Decimal("40"),
    available_delay_seconds: int = 0,
    emit: bool = True,
):
    authorization = MockCandidateAuthorization(
        version="test-mock-candidate-authorization-v1",
        producer_id=_TEST_PRODUCER_ID,
        producer_version=_TEST_PRODUCER_VERSION,
        forecast_family=_TEST_FORECAST_FAMILY,
        hypothesis_fingerprint="a" * 64,
        evaluation_plan_id="test-capital-candidate-plan",
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        valid_until=datetime(2027, 1, 1, tzinfo=UTC),
        maximum_allocation_fraction=Decimal("0.30"),
        minimum_entry_net_bps=Decimal("5"),
        minimum_hold_net_bps=Decimal("-5"),
    )
    configured = config.model_copy(
        update={
            "capital": config.capital.model_copy(
                update={"mock_candidate_authorizations": (authorization,)}
            )
        }
    )
    source = CapitalForecastSource(
        forecast_family=_TEST_FORECAST_FAMILY,
        producer=(
            _FixedMockForecastProducer(
                store=SqlForecastStore(engine),
                raw_score=raw_score,
                available_delay_seconds=available_delay_seconds,
            )
            if emit
            else _NoForecastProducer()
        ),
        estimated_variable_cost_bps=Decimal("20"),
        risk_template=configured.capital.sleeve_risk,
        mock_authorization=authorization,
    )
    return configured, assemble_capital_cycle(
        configured,
        engine,
        forecast_sources=(source,),
    )


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


def test_capital_cycle_observes_cash_without_an_active_candidate() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=NOW, sequence=6)

    result = assemble_capital_cycle(config, engine).produce(
        as_of=NOW,
        cause_id="cash-observation-batch",
        trigger_batch_id="cash-observation-batch",
        symbol="BTCUSDT",
        trigger_types=("HEARTBEAT",),
    )

    assert result.outcome.value == "NO_CHANGE"
    account = SqlPortfolioStore(engine).latest_account(
        portfolio_id=config.capital.decision.portfolio_id,
        as_of=NOW,
    )
    assert account is not None
    assert account.cash_balance == account.equity == Decimal("10000")
    assert not account.positions
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(mock_product_orders)) == 0
    activity = CapitalDashboardReader(engine, config).activity()[0]
    assert activity.outcome == "NO_OPPORTUNITY"
    assert activity.reason_codes == ("NO_ACTIVE_CAPITAL_OPPORTUNITY",)


def test_capital_cycle_assembles_the_exact_configured_mock_carry_candidate() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    carry = config.carry_forecast
    authorization = MockCandidateAuthorization(
        version="test-carry-mock-authorization-v1",
        producer_id=carry.producer_id,
        producer_version=carry.version,
        forecast_family=carry.forecast_family,
        hypothesis_fingerprint="b" * 64,
        evaluation_plan_id="test-carry-capital-plan",
        valid_from=datetime(2026, 9, 1, tzinfo=UTC),
        valid_until=datetime(2027, 9, 1, tzinfo=UTC),
        maximum_allocation_fraction=Decimal("0.30"),
        minimum_entry_net_bps=Decimal("5"),
        minimum_hold_net_bps=Decimal("-5"),
    )
    configured = config.model_copy(
        update={
            "capital": config.capital.model_copy(
                update={"mock_candidate_authorizations": (authorization,)}
            )
        }
    )
    market = SqlMarketDataStore(engine)
    _put_market(market, configured, at=NOW, sequence=7)

    result = assemble_capital_cycle(configured, engine).produce(
        as_of=NOW,
        cause_id="configured-carry-candidate-batch",
        trigger_batch_id="configured-carry-candidate-batch",
        symbol="BTCUSDT",
        trigger_types=("HEARTBEAT",),
    )

    assert isinstance(result, TradePlanExecutionResult)
    assert len(result.groups) == 1
    assert len(result.groups[0].target_legs) == 2
    account = SqlPortfolioStore(engine).latest_account(
        portfolio_id=configured.capital.decision.portfolio_id,
        as_of=NOW,
    )
    assert account is not None
    assert account.positions
    target = result.account.sleeves[0]
    assert target.forecast_family == carry.forecast_family


def test_capital_cycle_turns_an_explicit_candidate_into_idempotent_mock_trade() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=NOW, sequence=7)
    _put_funding_history(market, config, at=NOW)
    config, service = _candidate_service(config, engine)

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
    assert first.groups[0].valid_until == NOW + timedelta(minutes=30)
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
    first_page = CapitalDashboardReader(engine, config).activity(limit=1)
    second_page = CapitalDashboardReader(engine, config).activity(
        cursor=PageCursor(first_page[0].at, first_page[0].activity_id),
        limit=1,
    )
    assert len(first_page) == len(second_page) == 1
    assert first_page[0].activity_id != second_page[0].activity_id


def test_explicit_mock_candidate_can_trade_outside_monthly_window_via_same_chain() -> None:
    at = datetime(2026, 8, 21, 18, 5, tzinfo=UTC)
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=at, sequence=70)
    _put_funding_history(market, config, at=at)

    config, service = _candidate_service(config, engine)
    result = service.produce(
        as_of=at,
        cause_id="explicit-candidate-batch",
        trigger_batch_id="explicit-candidate-batch",
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


def test_unprofitable_candidate_explains_cash_without_fake_rebalance() -> None:
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

    config, service = _candidate_service(config, engine, raw_score=Decimal("20"))
    result = service.produce(
        as_of=at,
        cause_id="unprofitable-candidate-batch",
        trigger_batch_id="unprofitable-candidate-batch",
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
    _put_funding_history(market, config, at=NOW)
    available_at = NOW + timedelta(seconds=5)
    config, service = _candidate_service(
        config,
        engine,
        available_delay_seconds=5,
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
    _put_funding_history(market, config, at=NOW)
    config, service = _candidate_service(config, engine)

    opened = service.produce(as_of=NOW)
    assert isinstance(opened, TradePlanExecutionResult)

    missed = NOW + timedelta(hours=25)
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
    config, restarted = _candidate_service(config, engine, emit=False)
    after_restart = restarted.produce(
        as_of=missed,
        cause_id="capital-test-batch-2",
        trigger_batch_id="capital-test-batch-2",
        symbol="BTCUSDT",
        trigger_types=("HEARTBEAT",),
    )
    _put_trigger_batch(engine, config, at=missed, sequence=2)

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


def test_candidate_risk_forced_cash_is_idempotent_for_the_same_cause() -> None:
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
    _put_funding_history(market, config, at=NOW)
    config, service = _candidate_service(config, engine)

    forced_cash = service.produce(as_of=NOW)
    replay = service.produce(as_of=NOW)

    assert forced_cash.outcome.value == "PLANNED"
    assert forced_cash.trade_plan is not None
    assert forced_cash.trade_plan.groups == ()
    assert replay == forced_cash
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
    _put_funding_history(market, loaded, at=NOW)
    loaded, service = _candidate_service(loaded, engine)
    opened = service.produce(as_of=NOW)
    assert isinstance(opened, TradePlanExecutionResult)
    assert opened.account.positions

    protected = loaded.model_copy(
        update={
            "capital": loaded.capital.model_copy(
                update={"risk": loaded.capital.risk.model_copy(update={"kill_switch": True})}
            )
        }
    )
    heartbeat = NOW + timedelta(hours=25)
    _put_market(market, protected, at=heartbeat, sequence=21)
    protected, service = _candidate_service(protected, engine, emit=False)
    exited = service.produce(as_of=heartbeat)

    assert isinstance(exited, TradePlanExecutionResult)
    assert not exited.account.positions
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(mock_product_orders)) == 4
        payload = connection.execute(select(portfolio_holding_risk_reviews.c.payload)).scalar_one()
    review = PortfolioHoldingRiskReview.model_validate(payload)
    assert review.outcome == HoldingRiskOutcome.EXIT
