from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, func, insert, select, update

from investment_manager.decision_cycle.capital import (
    CapitalForecastSource,
    CapitalTriggerConsumer,
    assemble_capital_cycle,
    forecast_external_validity,
)
from investment_manager.decision_cycle.portfolio import TradePlanExecutionResult
from investment_manager.entrypoints.dashboard.capital import (
    CapitalDashboardReader,
    serialize_capital_activity,
    serialize_capital_equity,
    serialize_capital_overview,
)
from investment_manager.entrypoints.dashboard.pagination import PageCursor
from investment_manager.execution.tables import mock_product_orders, trade_plans
from investment_manager.execution.venue.runtime import assemble_product_execution_runtime
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
    ForecastTarget,
)
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.forecast.results import BaseForecast, ForecastBucketProbability
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.market.models import InstrumentId, InstrumentProduct, MarketQuote
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.portfolio.models import (
    CandidateCapitalAuthorization,
    CapitalCycleOutcome,
    CapitalCycleRecord,
    PortfolioEdgeBasis,
)
from investment_manager.portfolio.repository import SqlPortfolioStore, load_portfolio_target
from investment_manager.portfolio.tables import (
    capital_cycle_records,
    portfolio_account_snapshots,
    portfolio_performance_intervals,
    portfolio_targets,
)
from investment_manager.risk.portfolio import HoldingRiskOutcome, PortfolioHoldingRiskReview
from investment_manager.risk.tables import (
    portfolio_holding_risk_reviews,
    portfolio_risk_decisions,
    risk_execution_authorizations,
)
from investment_manager.scheduling.models import (
    AnalysisTriggerType,
    TriggerBatch,
    build_trigger_event,
)
from investment_manager.scheduling.tables import analysis_trigger_batches
from investment_manager.schema import create_schema
from investment_manager.settings import load_config

NOW = datetime(2026, 9, 1, 0, 5, tzinfo=UTC)

_TEST_PRODUCER_ID = "test-capital-candidate"
_TEST_PRODUCER_VERSION = "test-capital-candidate-v1"
_TEST_FORECAST_FAMILY = "test-delta-neutral-candidate"


@dataclass(frozen=True)
class _MechanismRef:
    mechanism_id: str
    continuity_ref: str | None = None


@dataclass(frozen=True)
class _RetirementRef:
    previous_mechanism_id: str


@dataclass(frozen=True)
class _AssessmentRef:
    assessment_id: str
    analysis_scope: str
    available_at: datetime
    mechanisms: tuple[_MechanismRef, ...]
    retired_mechanisms: tuple[_RetirementRef, ...] = ()


@dataclass(frozen=True)
class _PreviousContextRef:
    assessment_id: str


@dataclass(frozen=True)
class _PacketRef:
    previous_context: _PreviousContextRef | None


@dataclass(frozen=True)
class _WorldModelLineageStub:
    assessments: tuple[_AssessmentRef, ...]
    latest_id: str
    previous_by_id: dict[str, str]

    def latest_before(self, **_kwargs):
        return self.assessment(self.latest_id)

    def assessment(self, assessment_id):
        return next(
            (item for item in self.assessments if item.assessment_id == assessment_id),
            None,
        )

    def packet_for_assessment(self, assessment_id):
        previous_id = self.previous_by_id.get(assessment_id)
        return _PacketRef(
            None if previous_id is None else _PreviousContextRef(previous_id)
        )


def test_world_model_evidence_refresh_preserves_forecast_validity() -> None:
    source = _AssessmentRef(
        assessment_id="world-model-old",
        analysis_scope="portfolio",
        available_at=NOW - timedelta(minutes=20),
        mechanisms=(_MechanismRef("old-a"), _MechanismRef("old-b")),
    )
    refreshed = _AssessmentRef(
        assessment_id="world-model-new",
        analysis_scope="portfolio",
        available_at=NOW - timedelta(minutes=10),
        mechanisms=(
            _MechanismRef("new-a", continuity_ref="old-a"),
            _MechanismRef("new-b", continuity_ref="old-b"),
        ),
    )

    validity = forecast_external_validity(
        world_models=_WorldModelLineageStub(
            assessments=(source, refreshed),
            latest_id=refreshed.assessment_id,
            previous_by_id={refreshed.assessment_id: source.assessment_id},
        ),
        world_model_scope="portfolio",
        forecast_world_model_id=source.assessment_id,
        as_of=NOW,
    )

    assert validity is not None
    assert validity.current
    assert validity.reason_codes == ()
    assert validity.evidence_refs == ("world-model-new", "world-model-old")


def test_world_model_causal_structure_change_invalidates_forecast() -> None:
    source = _AssessmentRef(
        assessment_id="world-model-old",
        analysis_scope="portfolio",
        available_at=NOW - timedelta(minutes=20),
        mechanisms=(_MechanismRef("old-a"), _MechanismRef("old-b")),
    )
    changed = _AssessmentRef(
        assessment_id="world-model-new",
        analysis_scope="portfolio",
        available_at=NOW - timedelta(minutes=10),
        mechanisms=(
            _MechanismRef("new-a", continuity_ref="old-a"),
            _MechanismRef("new-c"),
        ),
        retired_mechanisms=(_RetirementRef("old-b"),),
    )

    validity = forecast_external_validity(
        world_models=_WorldModelLineageStub(
            assessments=(source, changed),
            latest_id=changed.assessment_id,
            previous_by_id={changed.assessment_id: source.assessment_id},
        ),
        world_model_scope="portfolio",
        forecast_world_model_id=source.assessment_id,
        as_of=NOW,
    )

    assert validity is not None
    assert not validity.current
    assert validity.reason_codes == (
        "FORECAST_WORLD_MODEL_CAUSAL_STRUCTURE_CHANGED",
    )
    assert validity.evidence_refs == ("world-model-new", "world-model-old")


def _assemble_capital_cycle(config, engine, **kwargs):
    execution = assemble_product_execution_runtime(config, engine)
    return assemble_capital_cycle(
        config,
        engine,
        venue=execution.venue,
        initial_cash=execution.initial_cash,
        **kwargs,
    )


def _test_spot_target() -> ForecastTarget:
    return ForecastTarget.create(
        (
            ForecastLeg(
                instrument=InstrumentId.binance_spot(
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
        ),
                direction=ExposureDirection.LONG,
                gross_weight=Decimal("1"),
            ),
        ),
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
        self.contracts.record_binding(self.binding, activated_at=as_of)
        slot = ForecastDecisionSlot.create(
            self.contract,
            slot_as_of=as_of,
            cutoff_prices=anchors,
        )
        self.contracts.record_slot(slot, binding=self.binding)
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

    def record_deadline_missed(
        self,
        *,
        as_of: datetime,
        completed_at: datetime,
    ) -> BaseForecast:
        del completed_at
        return self.produce(as_of=as_of)

    def recover_deadline_missed(self, **kwargs) -> tuple[()]:
        del kwargs
        return ()


class _NoForecastProducer:
    def __init__(self, *, contracts, contract, binding):
        self.contracts = contracts
        self.contract = contract
        self.binding = binding

    def produce(self, *, as_of: datetime) -> ForecastNoEstimate:
        self.contracts.record_contract(self.contract)
        self.contracts.record_binding(self.binding, activated_at=as_of)
        slot = ForecastDecisionSlot.create(
            self.contract,
            slot_as_of=as_of,
            cutoff_prices=(),
        )
        self.contracts.record_slot(slot, binding=self.binding)
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
        target=target or _test_spot_target(),
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
    binding = ForecastProducerBinding.create(
        contract_id=contract.contract_id,
        producer_kind=ForecastProducerKind.PROGRAM,
        producer_id=_TEST_PRODUCER_ID,
        producer_behavior_id=_TEST_PRODUCER_VERSION,
        permission=ForecastPermission.CAPITAL_CANDIDATE,
    )
    return contract, binding


def _candidate_service(
    config,
    engine,
    *,
    raw_score: Decimal = Decimal("40"),
    maximum_allocation_fraction: Decimal = Decimal("0.30"),
    available_delay_seconds: int = 0,
    emit: bool = True,
    target: ForecastTarget | None = None,
):
    contract, binding = _test_contract_and_binding(target=target)
    contract_store = SqlForecastContractStore(engine)
    authorization = CandidateCapitalAuthorization(
        version="test-candidate-capital-authorization-v1",
        producer_id=_TEST_PRODUCER_ID,
        producer_behavior_id=_TEST_PRODUCER_VERSION,
        outcome_family_id=_TEST_FORECAST_FAMILY,
        hypothesis_fingerprint="a" * 64,
        maximum_allocation_fraction=maximum_allocation_fraction,
        minimum_entry_net_bps=Decimal("5"),
        minimum_hold_net_bps=Decimal("-5"),
    )
    configured = config.model_copy(
        update={
            "capital": config.capital.model_copy(
                update={"candidate_capital_authorizations": (authorization,)}
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
        capital_authorization=authorization,
    )
    return configured, _assemble_capital_cycle(
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


class _TriggerCapitalStub:
    portfolio_id = "primary"

    def __init__(self, *, completed: bool = False) -> None:
        self.calls: list[tuple[str, datetime]] = []
        self.completed = completed

    def recover_missed_forecasts(self, *, before_slot_at, completed_at):
        self.calls.append(("recover", before_slot_at))
        return ()

    def record_missed_forecast(self, *, slot_at, completed_at):
        self.calls.append(("missed", slot_at))
        return ()

    def review(self, batch):
        self.calls.append(("review", batch.created_at))
        return None

    def cause_completed(self, cause_id):
        del cause_id
        return self.completed

    def produce(self, *, as_of, **kwargs):
        del kwargs
        self.calls.append(("produce", as_of))
        return None


def _runtime_batch(trigger_type: AnalysisTriggerType, *, at: datetime) -> TriggerBatch:
    event = build_trigger_event(
        trigger_type=trigger_type,
        symbol="BTCUSDT",
        pipeline_id="test-pipeline",
        occurred_at=at,
        observed_at=at,
        priority=50,
        dedup_key=f"{trigger_type.value}:{at.isoformat()}",
    )
    deadline = at + timedelta(minutes=5)
    return TriggerBatch(
        batch_id=stable_id(
            "trigger_batch",
            "BTCUSDT",
            "test-pipeline",
            1,
            event.trigger_id,
            deadline.isoformat(),
        ),
        symbol="BTCUSDT",
        pipeline_id="test-pipeline",
        plan_revision=1,
        created_at=at,
        deadline=deadline,
        triggers=(event,),
    )


def test_world_model_wakeup_reviews_risk_without_creating_forecast_slot() -> None:
    capital = _TriggerCapitalStub()
    consumer = CapitalTriggerConsumer(
        capital=capital,
        context_cadence_minutes=240,
        context_completion_deadline_seconds=1500,
        owner_symbol="BTCUSDT",
    )

    consumer.consume(_runtime_batch(AnalysisTriggerType.WORLD_MODEL_UPDATED, at=NOW))

    assert capital.calls == [("review", NOW)]


def test_late_cadence_is_a_no_estimate_then_current_risk_review() -> None:
    at = NOW.replace(hour=4, minute=30)
    slot = at.replace(minute=0)
    capital = _TriggerCapitalStub()
    consumer = CapitalTriggerConsumer(
        capital=capital,
        context_cadence_minutes=240,
        context_completion_deadline_seconds=1500,
        owner_symbol="BTCUSDT",
    )

    consumer.consume(_runtime_batch(AnalysisTriggerType.HEARTBEAT, at=at))

    assert capital.calls == [
        ("recover", slot),
        ("missed", slot),
        ("review", at),
    ]


def test_late_heartbeat_reviews_completed_cadence_without_marking_it_missed() -> None:
    at = NOW.replace(hour=4, minute=30)
    slot = at.replace(minute=0)
    capital = _TriggerCapitalStub(completed=True)
    consumer = CapitalTriggerConsumer(
        capital=capital,
        context_cadence_minutes=240,
        context_completion_deadline_seconds=1500,
        owner_symbol="BTCUSDT",
    )

    consumer.consume(_runtime_batch(AnalysisTriggerType.HEARTBEAT, at=at))

    assert capital.calls == [
        ("recover", slot),
        ("review", at),
    ]


def test_cadence_slot_before_release_activation_is_not_assigned() -> None:
    at = NOW.replace(hour=4, minute=30)
    capital = _TriggerCapitalStub()
    consumer = CapitalTriggerConsumer(
        capital=capital,
        context_cadence_minutes=240,
        context_completion_deadline_seconds=1500,
        owner_symbol="BTCUSDT",
        context_activation_at=at.replace(hour=4, minute=10),
    )

    consumer.consume(_runtime_batch(AnalysisTriggerType.HEARTBEAT, at=at))

    assert capital.calls == [("review", at)]


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
                    "candidate_capital_authorizations": (),
                }
            )
        }
    )
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=NOW, sequence=6)

    result = _assemble_capital_cycle(config, engine).produce(
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
    assert CapitalDashboardReader(engine, config).activity() == ()


def test_dashboard_hides_retired_no_opportunity_receipts() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=NOW, sequence=61)
    _assemble_capital_cycle(config, engine, forecast_sources=()).produce(
        as_of=NOW,
        cause_id="retired-no-opportunity",
        trigger_batch_id="retired-no-opportunity",
        symbol="BTCUSDT",
        trigger_types=("HEARTBEAT",),
    )
    assert CapitalDashboardReader(engine, config).activity() == ()
    with engine.begin() as connection:
        connection.execute(
            update(capital_cycle_records).values(outcome="NO_OPPORTUNITY")
        )
        assert connection.scalar(select(func.count()).select_from(capital_cycle_records)) == 1

    assert CapitalDashboardReader(engine, config).activity() == ()


def test_recovered_old_cadence_never_backdates_the_account_ledger() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    old_slot = NOW - timedelta(hours=1)
    _put_market(market, config, at=old_slot, sequence=62)
    _put_market(market, config, at=NOW, sequence=63)
    service = _assemble_capital_cycle(config, engine, forecast_sources=())
    service.produce(
        as_of=NOW,
        cause_id="current-cash-observation",
        trigger_batch_id="current-cash-observation",
        symbol="BTCUSDT",
        trigger_types=("HEARTBEAT",),
    )

    service.produce(
        as_of=old_slot,
        cause_id="recovered-old-cadence",
        trigger_batch_id="recovered-old-cadence",
        symbol="BTCUSDT",
        trigger_types=("FORECAST_CADENCE",),
    )

    with engine.connect() as connection:
        assert (
            connection.scalar(select(func.count()).select_from(portfolio_account_snapshots))
            == 1
        )
        assert connection.scalar(select(func.count()).select_from(capital_cycle_records)) == 1


def test_capital_cycle_turns_an_explicit_candidate_into_idempotent_order() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=NOW, sequence=7)
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
    assert first.account.equity == Decimal("9997.9750")
    assert first.account.revision == 1
    assert content_hash(first.account) == content_hash(
        first.account.model_copy(update={"revision": 0})
    )
    assert {abs(item.quantity) for item in first.account.positions} == {Decimal("0.015")}
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(mock_product_orders)) == 1
        assert (
            connection.scalar(select(func.count()).select_from(portfolio_performance_intervals))
            == 1
        )
        assert connection.scalar(select(func.count()).select_from(capital_cycle_records)) == 2

    overview = CapitalDashboardReader(engine, config).overview()
    assert (
        SqlPortfolioStore(engine).latest_account(
            portfolio_id=config.capital.decision.portfolio_id,
            as_of=NOW,
        )
        == first.account
    )
    dto = serialize_capital_overview(overview)
    assert "forecast_evidence" not in dto
    assert dto["account"]["equity"] == "9997.9750"
    assert dto["decision"]["risk_outcome"] == "APPROVED"
    assert dto["execution"] == {
        "active_group_count": 0,
        "active_groups": [],
        "total_order_count": 1,
    }
    assert dto["performance"]["interval_count"] == 1
    assert dto["performance"]["cumulative_net_pnl"] == "-2.0250"
    assert dto["performance"]["latest"]["kind"] == "EXECUTION"
    assert dto["performance"]["latest"]["net_pnl"] == "-2.0250"
    equity_points = CapitalDashboardReader(engine, config).equity_history()
    by_revision = sorted(equity_points, key=lambda item: item.revision)
    assert [item.revision for item in by_revision] == [0, 1]
    assert [item.equity for item in by_revision] == [Decimal("10000"), Decimal("9997.9750")]
    assert serialize_capital_equity(tuple(by_revision))["points"][-1] == {
        "snapshot_id": first.account.snapshot_id,
        "at": NOW.isoformat(),
        "revision": 1,
        "equity": "9997.9750",
        "net_pnl": "-2.0250",
        "drawdown_fraction": "0.0002025",
        "cash_benchmark_equity": None,
        "passive_benchmark_equity": None,
        "increment_vs_cash": None,
        "increment_vs_passive": None,
        "passive_drawdown_fraction": None,
    }
    newest_equity_page = CapitalDashboardReader(engine, config).equity_history(limit=1)
    older_equity_page = CapitalDashboardReader(engine, config).equity_history(
        cursor=PageCursor(
            newest_equity_page[0].at,
            newest_equity_page[0].snapshot_id,
        ),
        limit=1,
    )
    assert len(newest_equity_page) == len(older_equity_page) == 1
    assert newest_equity_page[0].snapshot_id != older_equity_page[0].snapshot_id
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


def test_explicit_candidate_can_trade_via_the_authoritative_capital_chain() -> None:
    at = datetime(2026, 8, 21, 18, 5, tzinfo=UTC)
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=at, sequence=70)

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
    assert target.sleeves[0].edge_basis == PortfolioEdgeBasis.EXPERIMENTAL_HYPOTHESIS
    assert target.sleeves[0].decision_net_bps > Decimal("5")
    assert target.sleeves[0].desired_gross_notional == Decimal("3000")
    legacy_payload = target.model_dump(mode="json")
    legacy_payload["sleeves"][0]["edge_basis"] = "MOCK_HYPOTHESIS"
    assert (
        load_portfolio_target(legacy_payload).sleeves[0].edge_basis
        == PortfolioEdgeBasis.EXPERIMENTAL_HYPOTHESIS
    )
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(mock_product_orders)) == 1


def test_unprofitable_candidate_explains_cash_without_fake_rebalance() -> None:
    at = datetime(2026, 8, 21, 18, 5, tzinfo=UTC)
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=at, sequence=71)

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
    assert activity.candidate_economics_recorded
    economics = activity.candidate_economics[0]
    assert economics.net_bps < economics.decision_threshold_bps
    serialized = serialize_capital_activity((activity,))["actions"][0]
    assert serialized["candidate_economics"][0]["net_bps"] == str(economics.net_bps)
    assert serialized["candidate_economics_recorded"] is True

    assert result.trade_plan is not None
    target = SqlPortfolioStore(engine).target_for_cycle(result.trade_plan.cycle_id)
    assert target is not None and target.candidate_evaluations is not None
    frozen = target.candidate_evaluations[0]
    changed_authorization = config.capital.candidate_capital_authorizations[0].model_copy(
        update={"minimum_entry_net_bps": Decimal("999")}
    )
    changed_specs = tuple(
        item.model_copy(update={"fee_bps": Decimal("999")})
        for item in config.capital.execution_specs
    )
    changed_config = config.model_copy(
        update={
            "capital": config.capital.model_copy(
                update={
                    "candidate_capital_authorizations": (changed_authorization,),
                    "execution_specs": changed_specs,
                }
            )
        }
    )
    historical = CapitalDashboardReader(engine, changed_config).activity()[0]
    assert historical.candidate_economics[0].net_bps == frozen.decision_net_bps
    assert (
        historical.candidate_economics[0].decision_threshold_bps
        == frozen.minimum_net_bps
    )


def test_late_slot_accepts_an_existing_forecast_after_pipeline_change() -> None:
    at = datetime(2026, 8, 21, 16, 0, tzinfo=UTC)
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    _put_market(SqlMarketDataStore(engine), config, at=at, sequence=73)
    _, service = _candidate_service(config, engine)

    produced = service.produce(
        as_of=at,
        cause_id="original-pipeline-cadence",
        symbol="BTCUSDT",
        trigger_types=("FORECAST_CADENCE",),
    )
    assert isinstance(produced, TradePlanExecutionResult)

    terminal = service.record_missed_forecast(
        slot_at=at,
        completed_at=at + timedelta(minutes=30),
    )

    assert len(terminal) == 1
    assert isinstance(terminal[0], BaseForecast)


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
    assert record.pipeline_id == config.capital.version
    assert record.pipeline_id != config.pipeline.version


def test_capital_cycle_uses_forecast_identity_and_holds_without_one() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=NOW, sequence=1)
    config, service = _candidate_service(config, engine)

    opened = service.produce(as_of=NOW)
    assert isinstance(opened, TradePlanExecutionResult)

    missed = NOW + timedelta(hours=25)
    _put_market(
        market,
        config,
        at=missed,
        sequence=3,
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
    overview = CapitalDashboardReader(engine, config).overview()
    dto = serialize_capital_overview(overview)
    assert dto["decision"]["as_of"] == missed.isoformat()
    assert "PROGRAMMATIC_RISK_EXIT" in dto["decision"]["reason_codes"]
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(portfolio_targets)) == 1
        assert (
            connection.scalar(select(func.count()).select_from(risk_execution_authorizations))
            == 2
        )
        assert connection.scalar(select(func.count()).select_from(mock_product_orders)) == 2
    activity = CapitalDashboardReader(engine, config).activity()
    assert activity[0].outcome == "EXECUTED"
    assert "PROGRAMMATIC_RISK_EXIT" in activity[0].reason_codes


def test_trigger_review_records_an_exit_that_finishes_in_cash() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=NOW, sequence=71)
    config, service = _candidate_service(config, engine)
    opened = service.produce(as_of=NOW)
    assert isinstance(opened, TradePlanExecutionResult)

    review_at = NOW + timedelta(hours=25)
    _put_market(market, config, at=review_at, sequence=72)
    config, restarted = _candidate_service(config, engine, emit=False)
    batch = _runtime_batch(AnalysisTriggerType.HEARTBEAT, at=review_at)

    exited = restarted.review(batch)

    assert isinstance(exited, TradePlanExecutionResult)
    assert not exited.account.sleeves
    with engine.connect() as connection:
        payloads = connection.execute(select(capital_cycle_records.c.payload)).scalars()
        records = tuple(CapitalCycleRecord.model_validate(item) for item in payloads)
    exit_record = next(item for item in records if item.cause_id == batch.batch_id)
    assert exit_record.outcome == CapitalCycleOutcome.RISK_EXIT
    assert "PROGRAMMATIC_RISK_EXIT" in exit_record.reason_codes
    activity = CapitalDashboardReader(engine, config).activity()
    assert activity[0].outcome == "EXECUTED"
    assert "PROGRAMMATIC_RISK_EXIT" in activity[0].reason_codes


def test_holding_review_target_is_not_mislabeled_as_risk_exit() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=NOW, sequence=73)
    config, service = _candidate_service(
        config,
        engine,
        maximum_allocation_fraction=Decimal("0.10"),
    )
    opened = service.produce(as_of=NOW)
    assert isinstance(opened, TradePlanExecutionResult)

    review_at = NOW + timedelta(minutes=5)
    _put_market(market, config, at=review_at, sequence=74)
    config, restarted = _candidate_service(
        config,
        engine,
        maximum_allocation_fraction=Decimal("0.10"),
        emit=False,
    )
    batch = _runtime_batch(AnalysisTriggerType.HEARTBEAT, at=review_at)

    reviewed = restarted.review(batch)

    assert not isinstance(reviewed, TradePlanExecutionResult)
    with engine.connect() as connection:
        payloads = connection.execute(select(capital_cycle_records.c.payload)).scalars()
        records = tuple(CapitalCycleRecord.model_validate(item) for item in payloads)
    record = next(item for item in records if item.cause_id == batch.batch_id)
    assert record.outcome == CapitalCycleOutcome.TARGET_DECIDED
    assert record.target_id is not None
    assert record.execution_authorization_id is None
    assert "REBALANCE_BELOW_MINIMUM" in record.reason_codes
    activity = CapitalDashboardReader(engine, config).activity()
    assert activity[0].outcome == "NO_ORDER"
    assert activity[0].order_count == 0


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
        assert connection.scalar(select(func.count()).select_from(mock_product_orders)) == 2
        payload = connection.execute(select(portfolio_holding_risk_reviews.c.payload)).scalar_one()
    review = PortfolioHoldingRiskReview.model_validate(payload)
    assert review.outcome == HoldingRiskOutcome.EXIT
