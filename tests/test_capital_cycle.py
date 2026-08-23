from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, func, insert, select, update

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
from investment_manager.forecast.contract_repository import SqlForecastContractStore
from investment_manager.forecast.contracts import (
    ForecastBenchmarkProbability,
    ForecastContract,
    ForecastDecisionSlot,
    ForecastNoEstimate,
    ForecastNoEstimateReason,
    ForecastOutcomeBucket,
    ForecastPermission,
    ForecastPriceAnchor,
    ForecastProducerBinding,
    ForecastProducerKind,
)
from investment_manager.forecast.models import (
    ExposureDirection,
    ForecastLeg,
    ForecastQuantityMode,
    ForecastTarget,
)
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.forecast.results import BaseForecast, ForecastBucketProbability
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.market.models import InstrumentId, InstrumentProduct, MarketQuote
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


def _test_delta_neutral_target() -> ForecastTarget:
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
    return ForecastTarget.create(
        tuple(
            ForecastLeg(
                instrument=instrument,
                direction=(
                    ExposureDirection.LONG
                    if instrument.product == InstrumentProduct.SPOT
                    else ExposureDirection.SHORT
                ),
                gross_weight=Decimal("0.5"),
            )
            for instrument in instruments
        ),
        quantity_mode=ForecastQuantityMode.SAME_BASE_QUANTITY,
    )


@dataclass(frozen=True)
class _FixedMockForecastProducer:
    store: SqlForecastStore
    contracts: SqlForecastContractStore
    contract: ForecastContract
    binding: ForecastProducerBinding
    raw_score: Decimal
    available_delay_seconds: int = 0

    def produce(self, *, as_of: datetime) -> BaseForecast:
        available_at = as_of + timedelta(seconds=self.available_delay_seconds)
        target = self.contract.target
        anchors = tuple(
            ForecastPriceAnchor(
                instrument_id=item.instrument.key,
                price=(
                    Decimal("100000")
                    if item.instrument.product == InstrumentProduct.SPOT
                    else Decimal("100300")
                ),
                observed_at=as_of,
                available_at=as_of,
                quote_ref=f"test-cutoff-{item.instrument.key}-{as_of.isoformat()}",
            )
            for item in target.legs
        )
        self.contracts.record_contract(self.contract)
        self.contracts.record_binding(self.binding)
        slot = ForecastDecisionSlot.create(
            self.contract,
            slot_as_of=as_of,
            cutoff_prices=anchors,
        )
        self.contracts.record_slot(slot)
        forecast_id = stable_id("base_forecast", slot.slot_id, self.binding.producer_behavior_id)
        existing = self.store.forecast(forecast_id)
        if existing is not None:
            assert isinstance(existing, BaseForecast)
            return existing
        forecast = BaseForecast(
            forecast_id=forecast_id,
            contract_id=self.contract.contract_id,
            decision_slot_id=slot.slot_id,
            producer_id=self.binding.producer_id,
            producer_behavior_id=self.binding.producer_behavior_id,
            outcome_family_id=self.contract.outcome_family_id,
            target=target,
            horizon_minutes=self.contract.horizon_minutes,
            cutoff_prices=anchors,
            entry_prices=tuple(
                item.model_copy(update={"available_at": available_at}) for item in anchors
            ),
            information_cutoff_at=as_of,
            input_observed_at=as_of,
            available_at=available_at,
            valid_until=available_at + timedelta(minutes=30),
            outcome_probabilities=(
                ForecastBucketProbability(
                    bucket_id="LOSS",
                    probability=(Decimal("1") - self.raw_score / Decimal("100")) / Decimal("2"),
                ),
                ForecastBucketProbability(bucket_id="FLAT", probability=Decimal("0")),
                ForecastBucketProbability(
                    bucket_id="GAIN",
                    probability=(Decimal("1") + self.raw_score / Decimal("100")) / Decimal("2"),
                ),
            ),
            expected_gross_bps=self.raw_score,
            input_refs=(stable_id("test_candidate_input", as_of),),
        )
        self.store.record(forecast)
        return forecast


class _NoForecastProducer:
    def __init__(self, *, contracts, contract, binding):
        self.contracts = contracts
        self.contract = contract
        self.binding = binding

    def produce(self, *, as_of: datetime) -> ForecastNoEstimate:
        self.contracts.record_contract(self.contract)
        self.contracts.record_binding(self.binding)
        slot = ForecastDecisionSlot.create(
            self.contract,
            slot_as_of=as_of,
            cutoff_prices=(),
        )
        self.contracts.record_slot(slot)
        result = ForecastNoEstimate(
            result_id=stable_id(
                "forecast_no_estimate", slot.slot_id, self.binding.producer_behavior_id
            ),
            slot_id=slot.slot_id,
            contract_id=self.contract.contract_id,
            producer_kind=ForecastProducerKind.PROGRAM,
            producer_id=self.binding.producer_id,
            producer_behavior_id=self.binding.producer_behavior_id,
            reason=ForecastNoEstimateReason.MARKET_INPUT_INVALID,
            information_cutoff_at=as_of,
            attempted_at=as_of,
            completed_at=as_of,
        )
        self.contracts.record_no_estimate(result)
        return result


def _test_contract_and_binding(
    *, target: ForecastTarget | None = None
) -> tuple[ForecastContract, ForecastProducerBinding]:
    buckets = (
        ForecastOutcomeBucket(
            bucket_id="LOSS",
            upper_bps=Decimal("-20"),
            representative_bps=Decimal("-100"),
        ),
        ForecastOutcomeBucket(
            bucket_id="FLAT",
            lower_bps=Decimal("-20"),
            upper_bps=Decimal("20"),
            representative_bps=Decimal("0"),
        ),
        ForecastOutcomeBucket(
            bucket_id="GAIN",
            lower_bps=Decimal("20"),
            representative_bps=Decimal("100"),
        ),
    )
    contract = ForecastContract.create(
        contract_version="test-carry-contract-v1",
        outcome_family_id=_TEST_FORECAST_FAMILY,
        target=target or _test_delta_neutral_target(),
        outcome_buckets=buckets,
        horizon_minutes=7 * 24 * 60,
        decision_slot_rule="test-slot-v1",
        evaluation_trigger="test-trigger-v1",
        information_cutoff_rule="slot-as-of-v1",
        completion_deadline_seconds=30,
        minimum_remaining_horizon_minutes=7 * 24 * 60 - 60,
        entry_anchor_rule="first-quote-after-completion-v1",
        cost_semantics_version="executable-round-trip-v1",
        validity_minutes=30,
        validity_conditions=("TEST_QUOTES_VALID",),
        settlement_rule="test-executable-settlement-v1",
        forecast_benchmark=tuple(
            ForecastBenchmarkProbability(bucket_id=bucket.bucket_id, probability=probability)
            for bucket, probability in zip(
                buckets,
                (Decimal("0.25"), Decimal("0.5"), Decimal("0.25")),
                strict=True,
            )
        ),
        decision_benchmark="cash-v1",
    )
    binding = ForecastProducerBinding(
        binding_id=stable_id(
            "forecast_producer_binding",
            contract.contract_id,
            ForecastProducerKind.PROGRAM.value,
            _TEST_PRODUCER_ID,
            _TEST_PRODUCER_VERSION,
            ForecastPermission.MOCK.value,
            (),
            None,
        ),
        contract_id=contract.contract_id,
        producer_kind=ForecastProducerKind.PROGRAM,
        producer_id=_TEST_PRODUCER_ID,
        producer_behavior_id=_TEST_PRODUCER_VERSION,
        permission=ForecastPermission.MOCK,
    )
    return contract, binding


def _candidate_service(
    config,
    engine,
    *,
    raw_score: Decimal = Decimal("40"),
    available_delay_seconds: int = 0,
    emit: bool = True,
    target: ForecastTarget | None = None,
):
    contract, binding = _test_contract_and_binding(target=target)
    contract_store = SqlForecastContractStore(engine)
    authorization = MockCandidateAuthorization(
        version="test-mock-candidate-authorization-v1",
        producer_id=_TEST_PRODUCER_ID,
        producer_behavior_id=_TEST_PRODUCER_VERSION,
        outcome_family_id=_TEST_FORECAST_FAMILY,
        hypothesis_fingerprint="a" * 64,
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
        contract=contract,
        binding=binding,
        producer=(
            _FixedMockForecastProducer(
                store=SqlForecastStore(engine),
                contracts=contract_store,
                contract=contract,
                binding=binding,
                raw_score=raw_score,
                available_delay_seconds=available_delay_seconds,
            )
            if emit
            else _NoForecastProducer(
                contracts=contract_store,
                contract=contract,
                binding=binding,
            )
        ),
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
    assert config.capital.context_forecast is not None
    config = config.model_copy(
        update={
            "capital": config.capital.model_copy(
                update={
                    "context_forecast": config.capital.context_forecast.model_copy(
                        update={"enabled": False}
                    ),
                    "mock_candidate_authorizations": (),
                }
            )
        }
    )
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
    assert activity.outcome == "CASH"
    assert activity.reason_codes == ("NO_REGISTERED_FORECAST_SOURCE",)


def test_dashboard_hides_retired_no_opportunity_receipts() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=NOW, sequence=61)
    assemble_capital_cycle(config, engine, forecast_sources=()).produce(
        as_of=NOW,
        cause_id="retired-no-opportunity",
        trigger_batch_id="retired-no-opportunity",
        symbol="BTCUSDT",
        trigger_types=("HEARTBEAT",),
    )
    with engine.begin() as connection:
        connection.execute(
            update(capital_cycle_records).values(outcome="NO_OPPORTUNITY")
        )
        assert connection.scalar(select(func.count()).select_from(capital_cycle_records)) == 1

    assert CapitalDashboardReader(engine, config).activity() == ()


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
    assert activity_by_symbol["ETHUSDT"].outcome == "FORECAST_ALREADY_DECIDED"
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


def test_configured_cash_carry_program_uses_the_authoritative_capital_path() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    base = load_config("config/investment-manager.shadow.yaml")
    authorization = MockCandidateAuthorization(
        version="cash-carry-forward-authorization-v1",
        producer_id="btc-cash-carry",
        producer_behavior_id="btc-cash-carry-behavior-v1",
        outcome_family_id="btc-delta-neutral-carry",
        hypothesis_fingerprint="b" * 64,
        maximum_allocation_fraction=Decimal("0.10"),
        minimum_entry_net_bps=Decimal("5"),
        minimum_hold_net_bps=Decimal("-5"),
    )
    assert base.capital.cash_carry_program is not None
    program = base.capital.cash_carry_program.model_copy(
        update={
            "enabled": True,
            "funding_lookback_hours": 72,
            "minimum_funding_samples": 3,
            "minimum_positive_funding_fraction": Decimal("0.66"),
        }
    )
    config = base.model_copy(
        update={
            "capital": base.capital.model_copy(
                update={
                    "cash_carry_program": program,
                    "context_forecast": base.capital.context_forecast.model_copy(
                        update={"enabled": False}
                    ),
                    "mock_candidate_authorizations": (authorization,),
                }
            )
        }
    )
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=NOW, sequence=700)
    _put_funding_history(market, config, at=NOW)

    result = assemble_capital_cycle(config, engine).produce(
        as_of=NOW,
        cause_id="configured-carry-program",
        trigger_batch_id="configured-carry-program",
        symbol="BTCUSDT",
        trigger_types=("HEARTBEAT",),
    )

    assert isinstance(result, TradePlanExecutionResult)
    assert result.groups and result.groups[0].terminal
    target = SqlPortfolioStore(engine).target_for_cycle(result.groups[0].cycle_id)
    assert target is not None
    assert target.sleeves[0].forecast_family == program.outcome_family_id
    assert target.sleeves[0].edge_basis == PortfolioEdgeBasis.MOCK_HYPOTHESIS


def test_explicit_mock_candidate_can_trade_via_the_authoritative_capital_chain() -> None:
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
    assert activity.reason_codes == ("CASH_SELECTED_NO_POSITIVE_NET_EDGE",)
    assert len(activity.candidate_economics) == 1
    economics = activity.candidate_economics[0]
    assert economics.net_bps < economics.entry_threshold_bps
    serialized = serialize_capital_activity((activity,))["actions"][0]
    assert serialized["candidate_economics"][0]["net_bps"] == str(economics.net_bps)


def test_spot_only_forecast_receives_only_its_executable_quote() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=NOW, sequence=72)
    spot = next(
        item.instrument
        for item in config.capital.execution_specs
        if item.instrument.product == InstrumentProduct.SPOT
    )
    config, service = _candidate_service(
        config,
        engine,
        raw_score=Decimal("9"),
        target=ForecastTarget.single_long(spot),
    )

    result = service.produce(
        as_of=NOW,
        cause_id="spot-only-candidate-batch",
        trigger_batch_id="spot-only-candidate-batch",
        symbol="BTCUSDT",
        trigger_types=("WORLD_MODEL_UPDATED",),
    )

    assert result.outcome.value == "PLANNED"
    assert result.trade_plan is not None
    assert result.trade_plan.groups == ()
    activity = CapitalDashboardReader(engine, config).activity()[0]
    assert len(activity.candidate_economics) == 1


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


def test_capital_cycle_uses_forecast_identity_and_holds_without_one() -> None:
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

    assert isinstance(after_restart, TradePlanExecutionResult)
    assert not after_restart.account.sleeves
    overview = CapitalDashboardReader(engine, config).overview(now=missed)
    dto = serialize_capital_overview(overview)
    assert dto["decision"]["as_of"] == missed.isoformat()
    assert "EXPIRED_FORECAST_EXIT" in dto["decision"]["reason_codes"]
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(portfolio_targets)) == 2
        assert connection.scalar(select(func.count()).select_from(mock_product_orders)) == 4
    activity = CapitalDashboardReader(engine, config).activity()
    assert activity[0].outcome == "EXECUTED"
    assert "EXPIRED_FORECAST_EXIT" in activity[0].reason_codes


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
