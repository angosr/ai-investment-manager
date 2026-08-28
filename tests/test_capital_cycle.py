from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, delete, func, insert, select, update

from investment_manager.decision_cycle.capital import (
    CapitalForecastSource,
    CapitalTriggerConsumer,
    assemble_capital_cycle,
)
from investment_manager.decision_cycle.portfolio import TradePlanExecutionResult
from investment_manager.entrypoints.dashboard.capital import (
    CapitalDashboardReader,
    serialize_capital_activity,
    serialize_capital_equity,
    serialize_capital_overview,
)
from investment_manager.entrypoints.dashboard.evaluation import EvaluationDashboardReader
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
    ForecastSlotCause,
    ForecastSlotOrigin,
)
from investment_manager.forecast.models import (
    ExposureDirection,
    ForecastLeg,
    ForecastTarget,
)
from investment_manager.forecast.quant.runtime import PortfolioQuantForecastProducer
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.forecast.results import BaseForecast, ForecastBucketProbability
from investment_manager.forecast.tables import forecast_decision_slots, forecasts
from investment_manager.kernel.errors import PointInTimeInputUnavailable
from investment_manager.kernel.identity import canonical_json, content_hash, stable_id
from investment_manager.market.models import (
    InstrumentId,
    InstrumentProduct,
    MarketQuote,
    TradFiMarket,
)
from investment_manager.market.perpetual.models import (
    FundingRateType,
    FundingSettlement,
    PerpetualQuote,
    TradingScheduleSnapshot,
    TradingSession,
    TradingSessionType,
)
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.market.tables import perpetual_quotes
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


def _assemble_capital_cycle(config, engine, **kwargs):
    execution = assemble_product_execution_runtime(config, engine)
    quant = config.outcome_evaluation.quant_baseline
    if quant is not None and quant.enabled and "quant_artifact_paths" not in kwargs:
        kwargs["quant_artifact_paths"] = {
            item.artifact_id: Path(item.relative_path) for item in quant.artifacts
        }
    return assemble_capital_cycle(
        config,
        engine,
        venue=execution.venue,
        initial_cash=execution.initial_cash,
        **kwargs,
    )


def _test_btc_target() -> ForecastTarget:
    return ForecastTarget.create(
        (
            ForecastLeg(
                instrument=InstrumentId(
                    venue="BINANCE",
                    product=InstrumentProduct.USD_M_PERPETUAL,
                    symbol="BTCUSDT",
                    base_asset="BTC",
                    quote_asset="USDT",
                    settlement_asset="USDT",
                ),
                direction=ExposureDirection.LONG,
                gross_weight=Decimal("1"),
            ),
        ),
    )


def _test_paxg_target() -> ForecastTarget:
    return ForecastTarget.create(
        (
            ForecastLeg(
                instrument=InstrumentId(
                    venue="BINANCE",
                    product=InstrumentProduct.USD_M_PERPETUAL,
                    symbol="PAXGUSDT",
                    base_asset="PAXG",
                    quote_asset="USDT",
                    settlement_asset="USDT",
                ),
                direction=ExposureDirection.LONG,
                gross_weight=Decimal("1"),
            ),
        )
    )


def _test_spy_target() -> ForecastTarget:
    return ForecastTarget.create(
        (
            ForecastLeg(
                instrument=InstrumentId(
                    venue="BINANCE",
                    product=InstrumentProduct.TRADFI_PERPETUAL,
                    symbol="SPYUSDT",
                    base_asset="SPY",
                    quote_asset="USDT",
                    settlement_asset="USDT",
                    tradfi_market=TradFiMarket.EQUITY,
                ),
                direction=ExposureDirection.LONG,
                gross_weight=Decimal("1"),
            ),
        )
    )


@dataclass(frozen=True)
class _FixedMockForecastProducer:
    store: SqlForecastStore
    contracts: SqlForecastContractStore
    contract: ForecastContract
    binding: ForecastProducerBinding
    raw_score: Decimal
    available_delay_seconds: int = 0

    def existing_result(self, *, as_of: datetime, cause=None):
        slot_id = ForecastDecisionSlot.identity_for(
            self.contract.contract_id,
            as_of,
            cause=cause or ForecastSlotCause.cadence(self.contract),
        )
        return self.store.result_for_behavior(
            decision_slot_id=slot_id,
            producer_behavior_id=self.binding.producer_behavior_id,
        )

    def produce(
        self,
        *,
        as_of: datetime,
        cause: ForecastSlotCause | None = None,
    ) -> BaseForecast:
        available_at = as_of + timedelta(seconds=self.available_delay_seconds)
        target = self.contract.target
        anchors = tuple(
            ForecastPriceAnchor(
                instrument_id=item.instrument.key,
                price=(
                    Decimal("765.1")
                    if item.instrument.symbol == "SPYUSDT"
                    else Decimal("4623.49")
                    if item.instrument.symbol == "PAXGUSDT"
                    else Decimal("100000")
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
            cause=cause,
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
        cause: ForecastSlotCause | None = None,
    ) -> BaseForecast:
        del completed_at
        return self.produce(as_of=as_of, cause=cause)

    def recover_deadline_missed(self, **kwargs) -> tuple[()]:
        del kwargs
        return ()


class _NoForecastProducer:
    def __init__(self, *, contracts, contract, binding):
        self.contracts = contracts
        self.contract = contract
        self.binding = binding

    def existing_result(self, *, as_of: datetime, cause=None):
        slot_id = ForecastDecisionSlot.identity_for(
            self.contract.contract_id,
            as_of,
            cause=cause or ForecastSlotCause.cadence(self.contract),
        )
        return self.contracts.no_estimate(
            stable_id(
                "forecast_no_estimate",
                slot_id,
                self.binding.producer_behavior_id,
            )
        )

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
        target=target or _test_btc_target(),
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
    )
    configured = config.model_copy(
        update={
            "capital": config.capital.model_copy(
                update={
                    "candidate_capital_authorizations": (authorization,),
                    "decision": config.capital.decision.model_copy(
                        update={
                            "maximum_single_sleeve_fraction": (
                                maximum_allocation_fraction
                            )
                        }
                    ),
                }
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
    include_paxg: bool = True,
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
    btc = _test_btc_target().legs[0].instrument
    market.put_perpetual_quote(
        PerpetualQuote(
            quote_id=stable_id("perpetual_quote", btc.key, sequence),
            instrument=btc,
            exchange_time=at,
            observed_at=at,
            bid=Decimal(spot_bid),
            bid_quantity=Decimal("2"),
            ask=Decimal(spot_ask),
            ask_quantity=Decimal("2"),
            update_id=sequence,
            source="test",
        )
    )
    if include_paxg:
        market.put_quote(
            MarketQuote(
                quote_id=f"paxg-capital-quote-{sequence}",
                symbol="PAXGUSDT",
                observed_at=at,
                bid=Decimal("4623.48"),
                bid_quantity=Decimal("2"),
                ask=Decimal("4623.49"),
                ask_quantity=Decimal("2"),
                source="test",
            )
        )
        paxg = _test_paxg_target().legs[0].instrument
        market.put_perpetual_quote(
            PerpetualQuote(
                quote_id=stable_id("perpetual_quote", paxg.key, sequence),
                instrument=paxg,
                exchange_time=at,
                observed_at=at,
                bid=Decimal("4623.48"),
                bid_quantity=Decimal("2"),
                ask=Decimal("4623.49"),
                ask_quantity=Decimal("2"),
                update_id=sequence,
                source="test",
            )
        )


def _put_spy_market(
    market: SqlMarketDataStore,
    *,
    at: datetime,
    sequence: int,
    bid: str = "765.0",
    ask: str = "765.1",
    session_type: TradingSessionType = TradingSessionType.REGULAR,
) -> InstrumentId:
    instrument = _test_spy_target().legs[0].instrument
    exchange_time = at - timedelta(seconds=1)
    market.put_perpetual_quote(
        PerpetualQuote(
            quote_id=stable_id("perpetual_quote", instrument.key, sequence),
            instrument=instrument,
            exchange_time=exchange_time,
            observed_at=at,
            bid=Decimal(bid),
            bid_quantity=Decimal("100"),
            ask=Decimal(ask),
            ask_quantity=Decimal("100"),
            update_id=sequence,
            source="test",
        )
    )
    market.put_trading_schedule(
        TradingScheduleSnapshot(
            schedule_id=stable_id(
                "tradfi_trading_schedule",
                exchange_time.isoformat(),
            ),
            exchange_time=exchange_time,
            observed_at=at,
            sessions=(
                TradingSession(
                    market=TradFiMarket.EQUITY,
                    starts_at=at - timedelta(hours=1),
                    ends_at=at + timedelta(hours=1),
                    session_type=session_type,
                ),
            ),
            source="test",
        )
    )
    return instrument


def _put_spy_funding(
    market: SqlMarketDataStore,
    *,
    instrument: InstrumentId,
    funding_time: datetime,
    observed_at: datetime,
    rate: str,
    mark_price: str,
    rate_type: FundingRateType,
) -> None:
    market.put_funding_settlement(
        FundingSettlement(
            settlement_id=stable_id(
                "funding_settlement",
                instrument.key,
                funding_time.isoformat(),
                rate_type.value,
            ),
            instrument=instrument,
            funding_time=funding_time,
            observed_at=observed_at,
            funding_rate=Decimal(rate),
            mark_price=Decimal(mark_price),
            rate_type=rate_type,
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

    def __init__(self, *, completed: bool = False, outputs_complete: bool = False) -> None:
        self.calls: list[tuple[str, datetime]] = []
        self.completed = completed
        self.outputs_complete = outputs_complete
        self.completed_causes: set[str] = set()
        self.causes: list[ForecastSlotCause | None] = []
        self.produced_trigger_types: list[tuple[str, ...]] = []

    def recover_missed_forecasts(self, *, before_slot_at, completed_at):
        self.calls.append(("recover", before_slot_at))
        return ()

    def record_missed_forecast(self, *, slot_at, completed_at, **kwargs):
        del completed_at, kwargs
        self.calls.append(("missed", slot_at))
        return ()

    def review(self, batch):
        self.calls.append(("review", batch.created_at))
        return None

    def cause_completed(self, cause_id):
        return self.completed or cause_id in self.completed_causes

    def forecast_outputs_complete(self, **kwargs):
        del kwargs
        return self.outputs_complete

    def produce(self, *, as_of, **kwargs):
        cause_id = kwargs["cause_id"]
        self.completed_causes.add(cause_id)
        self.causes.append(kwargs.get("cause"))
        self.produced_trigger_types.append(kwargs["trigger_types"])
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


def _runtime_mixed_forecast_batch(
    *,
    at: datetime,
    material_at: datetime | None = None,
) -> TriggerBatch:
    material_at = material_at or at
    triggers = []
    for trigger_type in (
        AnalysisTriggerType.FORECAST_EVENT_DUE,
        AnalysisTriggerType.FORECAST_SLOT_DUE,
    ):
        occurred_at = material_at if trigger_type == AnalysisTriggerType.FORECAST_EVENT_DUE else at
        triggers.append(
            build_trigger_event(
                trigger_type=trigger_type,
                symbol="BTCUSDT",
                pipeline_id="test-pipeline",
                occurred_at=occurred_at,
                observed_at=at,
                priority=100,
                dedup_key=f"{trigger_type.value}:{occurred_at.isoformat()}",
            )
        )
    frozen_triggers = tuple(sorted(triggers, key=lambda item: item.trigger_id))
    deadline = at + timedelta(minutes=5)
    return TriggerBatch(
        batch_id=stable_id(
            "trigger_batch",
            "BTCUSDT",
            "test-pipeline",
            1,
            *(item.trigger_id for item in frozen_triggers),
            deadline.isoformat(),
        ),
        symbol="BTCUSDT",
        pipeline_id="test-pipeline",
        plan_revision=1,
        created_at=at,
        deadline=deadline,
        triggers=frozen_triggers,
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


def test_material_world_model_update_near_cadence_keeps_event_slot_independent() -> None:
    capital = _TriggerCapitalStub()
    consumer = CapitalTriggerConsumer(
        capital=capital,
        context_cadence_minutes=240,
        context_completion_deadline_seconds=1500,
        material_event_slots_enabled=True,
        material_event_slot_policy_version="material-world-model-slot-v1",
        owner_symbol="BTCUSDT",
    )

    consumer.consume(_runtime_batch(AnalysisTriggerType.FORECAST_EVENT_DUE, at=NOW))

    assert capital.calls == [("produce", NOW)]
    assert capital.causes[0] is not None
    assert capital.causes[0].origins == (ForecastSlotOrigin.MATERIAL_STATE,)


def test_mixed_forecast_batch_preserves_independent_cadence_and_event_calls() -> None:
    at = NOW.replace(hour=4, minute=5)
    capital = _TriggerCapitalStub()
    consumer = CapitalTriggerConsumer(
        capital=capital,
        context_cadence_minutes=240,
        context_completion_deadline_seconds=1500,
        material_event_slots_enabled=True,
        material_event_slot_policy_version="material-world-model-slot-v1",
        owner_symbol="BTCUSDT",
    )

    consumer.consume(_runtime_mixed_forecast_batch(at=at))

    assert capital.calls == [
        ("recover", at.replace(minute=0)),
        ("produce", at.replace(minute=0)),
        ("produce", at),
    ]
    assert capital.produced_trigger_types == [
        ("FORECAST_CADENCE",),
        ("FORECAST_EVENT_DUE",),
    ]
    assert capital.causes[0] is None
    assert capital.causes[1] is not None
    assert capital.causes[1].origins == (ForecastSlotOrigin.MATERIAL_STATE,)


def test_mixed_batch_runs_earlier_material_slot_before_cadence_slot() -> None:
    cadence_at = NOW.replace(hour=4, minute=0)
    event_at = cadence_at - timedelta(minutes=5)
    capital = _TriggerCapitalStub()
    consumer = CapitalTriggerConsumer(
        capital=capital,
        context_cadence_minutes=240,
        context_completion_deadline_seconds=1500,
        material_event_slots_enabled=True,
        material_event_slot_policy_version="material-world-model-slot-v1",
        owner_symbol="BTCUSDT",
    )

    consumer.consume(_runtime_mixed_forecast_batch(at=cadence_at, material_at=event_at))

    assert capital.calls == [
        ("produce", event_at),
        ("recover", cadence_at),
        ("produce", cadence_at),
    ]
    assert capital.produced_trigger_types == [
        ("FORECAST_EVENT_DUE",),
        ("FORECAST_CADENCE",),
    ]


def test_completed_material_cause_does_not_consume_mixed_cadence_slot() -> None:
    at = NOW.replace(hour=4, minute=5)
    batch = _runtime_mixed_forecast_batch(at=at)
    material = next(
        item
        for item in batch.triggers
        if item.trigger_type == AnalysisTriggerType.FORECAST_EVENT_DUE
    )
    policy_version = "material-world-model-slot-v1"
    cause = ForecastSlotCause.material_state(
        policy_version=policy_version,
        trigger_refs=tuple(sorted({material.trigger_id, *material.evidence_ids})),
    )
    capital = _TriggerCapitalStub()
    capital.completed_causes.add(
        stable_id(
            "context_forecast_material_event",
            capital.portfolio_id,
            cause.policy_version,
            *cause.trigger_refs,
        )
    )
    consumer = CapitalTriggerConsumer(
        capital=capital,
        context_cadence_minutes=240,
        context_completion_deadline_seconds=1500,
        material_event_slots_enabled=True,
        material_event_slot_policy_version=policy_version,
        owner_symbol="BTCUSDT",
    )

    consumer.consume(batch)

    assert capital.calls == [
        ("recover", at.replace(minute=0)),
        ("produce", at.replace(minute=0)),
    ]


def test_material_cycle_uses_economic_cause_and_recovers_only_missing_receipt() -> None:
    at = NOW.replace(minute=30)
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=at, sequence=80)
    config, service = _candidate_service(config, engine, raw_score=Decimal("0"))
    consumer = CapitalTriggerConsumer(
        capital=service,
        context_cadence_minutes=240,
        context_completion_deadline_seconds=1500,
        material_event_slots_enabled=True,
        material_event_slot_policy_version="material-world-model-slot-v1",
        owner_symbol="BTCUSDT",
    )
    batch = _runtime_batch(AnalysisTriggerType.FORECAST_EVENT_DUE, at=at)

    consumer.consume(batch)

    with engine.begin() as connection:
        record = CapitalCycleRecord.model_validate(
            connection.execute(select(capital_cycle_records.c.payload)).scalar_one()
        )
        slot = ForecastDecisionSlot.model_validate(
            connection.execute(select(forecast_decision_slots.c.payload)).scalar_one()
        )
        assert record.cause_id != batch.batch_id
        assert record.trigger_batch_id == batch.batch_id
        assert slot.origins == (ForecastSlotOrigin.MATERIAL_STATE,)
        durable_counts = (
            connection.scalar(select(func.count()).select_from(forecasts)),
            connection.scalar(select(func.count()).select_from(portfolio_targets)),
            connection.scalar(select(func.count()).select_from(portfolio_account_snapshots)),
        )
        connection.execute(delete(capital_cycle_records))

    consumer.consume(batch)

    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(capital_cycle_records)) == 1
        assert (
            connection.scalar(select(func.count()).select_from(forecasts)),
            connection.scalar(select(func.count()).select_from(portfolio_targets)),
            connection.scalar(select(func.count()).select_from(portfolio_account_snapshots)),
        ) == durable_counts
    consumer.consume(batch)
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(capital_cycle_records)) == 1


def test_mixed_cycle_persists_two_causes_and_shared_batch_provenance() -> None:
    at = NOW.replace(minute=5)
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=at.replace(minute=0), sequence=81)
    config, service = _candidate_service(config, engine, raw_score=Decimal("0"))
    consumer = CapitalTriggerConsumer(
        capital=service,
        context_cadence_minutes=240,
        context_completion_deadline_seconds=1500,
        material_event_slots_enabled=True,
        material_event_slot_policy_version="material-world-model-slot-v1",
        owner_symbol="BTCUSDT",
    )
    batch = _runtime_mixed_forecast_batch(at=at)

    consumer.consume(batch)

    with engine.connect() as connection:
        records = tuple(
            CapitalCycleRecord.model_validate(payload)
            for payload in connection.execute(
                select(capital_cycle_records.c.payload).order_by(
                    capital_cycle_records.c.evaluated_at,
                    capital_cycle_records.c.record_id,
                )
            ).scalars()
        )
        slots = tuple(
            ForecastDecisionSlot.model_validate(payload)
            for payload in connection.execute(
                select(forecast_decision_slots.c.payload).order_by(
                    forecast_decision_slots.c.slot_as_of,
                    forecast_decision_slots.c.slot_id,
                )
            ).scalars()
        )
    assert len(records) == len(slots) == 2
    assert len({record.cause_id for record in records}) == 2
    assert all(record.cause_id != batch.batch_id for record in records)
    assert {record.trigger_batch_id for record in records} == {batch.batch_id}
    assert tuple(slot.origins for slot in slots) == (
        (ForecastSlotOrigin.CADENCE,),
        (ForecastSlotOrigin.MATERIAL_STATE,),
    )


def test_material_slot_before_cadence_preserves_second_forecast_call() -> None:
    event_at = NOW.replace(hour=3, minute=50)
    cadence_at = NOW.replace(hour=4, minute=0)
    capital = _TriggerCapitalStub()
    consumer = CapitalTriggerConsumer(
        capital=capital,
        context_cadence_minutes=240,
        context_completion_deadline_seconds=1500,
        material_event_slots_enabled=True,
        material_event_slot_policy_version="material-world-model-slot-v1",
        owner_symbol="BTCUSDT",
    )

    consumer.consume(_runtime_batch(AnalysisTriggerType.FORECAST_EVENT_DUE, at=event_at))
    consumer.consume(_runtime_batch(AnalysisTriggerType.FORECAST_SLOT_DUE, at=cadence_at))

    assert capital.calls == [
        ("produce", event_at),
        ("recover", cadence_at),
        ("produce", cadence_at),
    ]
    assert len(capital.causes) == 2
    assert capital.causes[0] is not None
    assert capital.causes[0].origins == (ForecastSlotOrigin.MATERIAL_STATE,)
    assert capital.causes[1] is None


def test_late_material_world_model_update_records_no_estimate() -> None:
    event_at = NOW
    batch = _runtime_batch(AnalysisTriggerType.FORECAST_EVENT_DUE, at=event_at)
    created_at = event_at + timedelta(minutes=30)
    deadline = event_at + timedelta(minutes=35)
    late_batch = TriggerBatch(
        batch_id=stable_id(
            "trigger_batch",
            batch.symbol,
            batch.pipeline_id,
            batch.plan_revision,
            *(item.trigger_id for item in batch.triggers),
            deadline.isoformat(),
        ),
        symbol=batch.symbol,
        pipeline_id=batch.pipeline_id,
        plan_revision=batch.plan_revision,
        created_at=created_at,
        deadline=deadline,
        triggers=batch.triggers,
    )
    capital = _TriggerCapitalStub()
    consumer = CapitalTriggerConsumer(
        capital=capital,
        context_cadence_minutes=240,
        context_completion_deadline_seconds=1500,
        material_event_slots_enabled=True,
        material_event_slot_policy_version="material-world-model-slot-v1",
        owner_symbol="BTCUSDT",
    )

    consumer.consume(late_batch)

    assert capital.calls == [("missed", event_at), ("review", late_batch.created_at)]


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


def test_late_cadence_resumes_an_existing_forecast_instead_of_marking_it_missed() -> None:
    at = NOW.replace(hour=4, minute=30)
    slot = at.replace(minute=0)
    capital = _TriggerCapitalStub(outputs_complete=True)
    consumer = CapitalTriggerConsumer(
        capital=capital,
        context_cadence_minutes=240,
        context_completion_deadline_seconds=1500,
        owner_symbol="BTCUSDT",
    )

    consumer.consume(_runtime_batch(AnalysisTriggerType.HEARTBEAT, at=at))

    assert capital.calls == [("recover", slot), ("produce", slot)]


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
    _put_market(market, config, at=NOW, sequence=6, include_paxg=False)

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
    reader = CapitalDashboardReader(engine, config)
    assert reader.activity() == ()
    dto = serialize_capital_overview(reader.overview(now=NOW))
    assert [item["symbol"] for item in dto["instruments"]] == [
        "BTCUSDT",
        "PAXGUSDT",
        "SPYUSDT",
    ]
    assert {item["quantity"] for item in dto["instruments"]} == {"0"}
    assert dto["instruments"][0]["price"] == "99995"
    assert dto["instruments"][1]["price"] is None


def test_forecast_evidence_reads_quant_state_from_program_snapshot() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    reader = EvaluationDashboardReader(
        engine,
        load_config("config/investment-manager.shadow.yaml"),
    )
    forecast = SimpleNamespace(
        analysis_input_json=None,
        program_input_json=canonical_json(
            {
                "quant_prior": {
                    "model_name": "momentum_reversal_volatility",
                    "outcome_probabilities": [],
                },
                "candidate_predictions": [
                    {
                        "model_name": "momentum",
                        "cell_key": "momentum=HIGH",
                    },
                    {
                        "model_name": "momentum_reversal_volatility",
                        "cell_key": "momentum=HIGH|short_return=LOW|volatility=HIGH",
                    },
                ],
            }
        ),
    )

    assert reader._forecast_market_state_key(forecast) == (
        "momentum=HIGH|short_return=LOW|volatility=HIGH"
    )


def test_forecast_evidence_reads_quant_state_from_posterior_snapshot() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    reader = EvaluationDashboardReader(
        engine,
        load_config("config/investment-manager.shadow.yaml"),
    )
    slot_id = "forecast_decision_slot_quant_posterior"
    forecast = SimpleNamespace(
        decision_slot_id=slot_id,
        program_input_json=None,
        analysis_input_json=canonical_json(
            {
                "forecast_targets": [
                    {
                        "decision_slot": {"decision_slot_id": slot_id},
                        "target_state": {
                            "asset_states": [{"regime": "TRENDING_UP"}],
                        },
                        "quant_panel": {
                            "quant_prior": {
                                "model_name": "momentum_reversal_volatility",
                                "outcome_probabilities": [],
                            },
                            "candidate_predictions": [
                                {
                                    "model_name": "momentum_reversal_volatility",
                                    "cell_key": (
                                        "momentum=LOW|short_return=MID|volatility=LOW"
                                    ),
                                }
                            ],
                        },
                    }
                ]
            }
        ),
    )

    assert reader._forecast_market_state_key(forecast) == (
        "momentum=LOW|short_return=MID|volatility=LOW"
    )


def test_trading_cost_evidence_reuses_unchanged_execution_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    reader = EvaluationDashboardReader(engine, config)
    original = reader._evaluate_trading_cost
    calls = 0

    def counted(groups, **kwargs):
        nonlocal calls
        calls += 1
        return original(groups, **kwargs)

    monkeypatch.setattr(reader, "_evaluate_trading_cost", counted)

    first = reader.trading_cost_evidence()
    second = reader.trading_cost_evidence()

    assert first is second
    assert calls == 1


def test_candidate_requires_only_its_own_executable_quote() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=NOW, sequence=7, include_paxg=False)
    _config, service = _candidate_service(
        config,
        engine,
        target=_test_paxg_target(),
    )

    with pytest.raises(
        PointInTimeInputUnavailable,
        match="PAXGUSDT 可执行报价",
    ):
        service.produce(as_of=NOW)


def test_held_product_still_requires_valuation_and_execution_quotes() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=NOW, sequence=8)
    _config, service = _candidate_service(
        config,
        engine,
        raw_score=Decimal("80"),
        target=_test_paxg_target(),
    )
    opened = service.produce(as_of=NOW)
    assert isinstance(opened, TradePlanExecutionResult)
    assert opened.account.positions[0].instrument.symbol == "PAXGUSDT"
    with engine.begin() as connection:
        connection.execute(
            delete(perpetual_quotes).where(
                perpetual_quotes.c.instrument_id
                == "BINANCE:USD_M_PERPETUAL:PAXGUSDT"
            )
        )
    later = NOW + timedelta(minutes=1)
    _put_market(
        market,
        config,
        at=later,
        sequence=9,
        include_paxg=False,
    )

    with pytest.raises(
        PointInTimeInputUnavailable,
        match="PAXGUSDT 估值与可执行报价",
    ):
        service.produce(as_of=later)


def test_tradfi_candidate_uses_schedule_funding_and_one_product_account() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    instrument = _put_spy_market(market, at=NOW, sequence=81)
    # This test isolates funding/account mechanics; production fee semantics are
    # asserted in test_config and would correctly reject this synthetic 80bps edge.
    execution_specs = tuple(
        item.model_copy(update={"fee_bps": Decimal("10")})
        if item.instrument.symbol == "SPYUSDT"
        else item
        for item in config.capital.execution_specs
    )
    config = config.model_copy(
        update={"capital": config.capital.model_copy(update={"execution_specs": execution_specs})}
    )
    configured, service = _candidate_service(
        config,
        engine,
        raw_score=Decimal("80"),
        maximum_allocation_fraction=Decimal("0.10"),
        target=_test_spy_target(),
    )

    opened = service.produce(
        as_of=NOW,
        cause_id="tradfi-open",
        trigger_batch_id="tradfi-open",
        symbol="SPYUSDT",
        trigger_types=("REFERENCE_PRODUCT_QUALIFICATION",),
    )

    assert isinstance(opened, TradePlanExecutionResult)
    assert opened.account.positions[0].instrument == instrument
    assert opened.account.positions[0].quantity == Decimal("1.30")
    assert opened.account.cash_balance > opened.account.equity
    assert opened.account.cash_balance < Decimal("10000")

    _put_spy_funding(
        market,
        instrument=instrument,
        funding_time=NOW + timedelta(hours=4),
        observed_at=NOW + timedelta(hours=4, seconds=1),
        rate="0.0001",
        mark_price="765",
        rate_type=FundingRateType.REGULAR,
    )
    _put_spy_funding(
        market,
        instrument=instrument,
        funding_time=NOW + timedelta(hours=6),
        observed_at=NOW + timedelta(hours=6, seconds=1),
        rate="-0.0002",
        mark_price="768",
        rate_type=FundingRateType.SPECIAL,
    )
    later = NOW + timedelta(hours=8)
    _put_spy_market(
        market,
        at=later,
        sequence=82,
        bid="770.0",
        ask="770.1",
    )

    reviewed = service.produce(
        as_of=later,
        cause_id="tradfi-review",
        trigger_batch_id="tradfi-review",
        symbol="SPYUSDT",
        trigger_types=("HEARTBEAT",),
    )

    assert isinstance(reviewed, TradePlanExecutionResult)
    assert reviewed.groups
    account = SqlPortfolioStore(engine).latest_account(
        portfolio_id=configured.capital.decision.portfolio_id,
        as_of=later,
    )
    assert account is not None
    assert account.accounting is not None
    assert account.accounting.funding_pnl == Decimal("0.10023")
    assert account.positions[0].quantity == Decimal("1.29")
    assert account.reconciled
    assert configured.capital.reference_policy is None


def test_tradfi_candidate_cannot_trade_during_an_official_closed_session() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    _put_spy_market(
        market,
        at=NOW,
        sequence=83,
        session_type=TradingSessionType.NO_TRADING,
    )
    _configured, service = _candidate_service(
        config,
        engine,
        raw_score=Decimal("80"),
        maximum_allocation_fraction=Decimal("0.10"),
        target=_test_spy_target(),
    )

    with pytest.raises(PointInTimeInputUnavailable, match=r"候选当前不可执行.*SPYUSDT"):
        service.produce(
            as_of=NOW,
            cause_id="tradfi-closed",
            trigger_batch_id="tradfi-closed",
            symbol="SPYUSDT",
            trigger_types=("REFERENCE_PRODUCT_QUALIFICATION",),
        )


def test_pending_group_requires_quotes_without_a_prior_account_position() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=NOW, sequence=10)
    _config, service = _candidate_service(
        config,
        engine,
        raw_score=Decimal("80"),
        target=_test_paxg_target(),
    )
    opened = service.produce(as_of=NOW)
    assert isinstance(opened, TradePlanExecutionResult)
    pending = opened.groups[0].model_copy(update={"terminal": False})
    with engine.begin() as connection:
        connection.execute(delete(portfolio_account_snapshots))

    valuation, execution = service._current_quote_requirements(
        as_of=NOW,
        recovered_groups=(pending,),
    )

    assert tuple(item.symbol for item in valuation) == ("PAXGUSDT",)
    assert tuple(item.symbol for item in execution) == ("PAXGUSDT",)


def test_shadow_research_chain_contains_only_active_quant_producer() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")

    service = _assemble_capital_cycle(
        config,
        engine,
        producer_activation_at=NOW,
    )

    assert {item.instrument.product for item in config.capital.execution_specs} == {
        InstrumentProduct.TRADFI_PERPETUAL,
        InstrumentProduct.USD_M_PERPETUAL,
    }
    assert service._forecast_sources == ()
    assert service._source_by_family == {}
    assert len(service._research_forecast_producers) == 1
    assert isinstance(
        service._research_forecast_producers[0],
        PortfolioQuantForecastProducer,
    )


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
        connection.execute(update(capital_cycle_records).values(outcome="NO_OPPORTUNITY"))
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
        assert connection.scalar(select(func.count()).select_from(portfolio_account_snapshots)) == 1
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
    assert first.account.equity == Decimal("9998.20")
    assert first.account.revision == 1
    assert content_hash(first.account) == content_hash(
        first.account.model_copy(update={"revision": 0})
    )
    assert {abs(item.quantity) for item in first.account.positions} == {Decimal("0.03")}
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
    assert dto["policy"] == {
        "mandate_version": "provisional-total-portfolio-real-growth-v2",
        "mandate_status": "PROVISIONAL",
        "objective": "REAL_CAPITAL_GROWTH",
        "horizon_years": 5,
        "base_currency": "USDT",
            "universe_version": "binance-shadow-investable-v9",
        "covered_exposures": [
            "CASH",
            "CRYPTO_NETWORK",
            "INFLATION_SENSITIVE",
            "US_EQUITY",
        ],
        "reference_policy_version": None,
    }
    assert dto["account"]["equity"] == "9998.20"
    assert dto["decision"]["risk_outcome"] == "APPROVED"
    assert dto["execution"] == {
        "active_group_count": 0,
        "active_groups": [],
        "total_order_count": 1,
    }
    assert dto["performance"]["interval_count"] == 1
    assert dto["performance"]["cumulative_net_pnl"] == "-1.80"
    assert dto["performance"]["attribution"] == {
        "price_pnl": "-0.30",
        "funding_pnl": "0",
        "fee_cost": "1.50",
        "net_pnl": "-1.80",
    }
    assert dto["performance"]["latest"]["kind"] == "EXECUTION"
    assert dto["performance"]["latest"]["net_pnl"] == "-1.80"
    equity_points = CapitalDashboardReader(engine, config).equity_history()
    by_revision = sorted(equity_points, key=lambda item: item.revision)
    assert [item.revision for item in by_revision] == [0, 1]
    assert [item.equity for item in by_revision] == [Decimal("10000"), Decimal("9998.20")]
    assert serialize_capital_equity(tuple(by_revision))["points"][-1] == {
        "snapshot_id": first.account.snapshot_id,
        "at": NOW.isoformat(),
        "revision": 1,
        "equity": "9998.20",
        "net_pnl": "-1.80",
        "drawdown_fraction": "0.00018",
        "cash_benchmark_equity": "10000",
        "increment_vs_cash": "-1.80",
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
    assert len(activity_by_symbol["BTCUSDT"].position_changes) == 1
    opening_change = activity_by_symbol["BTCUSDT"].position_changes[0]
    assert opening_change.instrument.key == "BINANCE:USD_M_PERPETUAL:BTCUSDT"
    assert opening_change.side.value == "BUY"
    assert opening_change.effect == "OPEN_LONG"
    assert opening_change.filled_quantity == Decimal("0.03")
    serialized_change = serialize_capital_activity(
        (activity_by_symbol["BTCUSDT"],)
    )["actions"][0]["position_changes"][0]
    assert serialized_change["effect"] == "OPEN_LONG"
    assert serialized_change["filled_quantity"] == "0.03"
    first_page = CapitalDashboardReader(engine, config).activity(limit=1)
    second_page = CapitalDashboardReader(engine, config).activity(
        cursor=PageCursor(first_page[0].at, first_page[0].activity_id),
        limit=1,
    )
    assert len(first_page) == len(second_page) == 1
    assert first_page[0].activity_id != second_page[0].activity_id


def test_recovered_forecast_uses_current_capital_time_without_backdating() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    resumed_at = NOW + timedelta(minutes=10)
    _put_market(market, config, at=NOW, sequence=70)
    _put_market(market, config, at=resumed_at, sequence=71)
    _configured, service = _candidate_service(config, engine)

    result = service.produce(
        as_of=NOW,
        decision_at=resumed_at,
        cause_id="recovered-capital-cause",
        trigger_batch_id="recovered-capital-batch",
        symbol="BTCUSDT",
        trigger_types=("FORECAST_CADENCE",),
    )

    assert isinstance(result, TradePlanExecutionResult)
    assert result.account.as_of == resumed_at
    target = load_portfolio_target(
        engine.connect().execute(select(portfolio_targets.c.payload)).scalar_one()
    )
    assert target.as_of == resumed_at


def test_recovered_forecast_after_entry_window_records_cash_without_hindsight_trade() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    resumed_at = NOW + timedelta(minutes=31)
    _put_market(market, config, at=NOW, sequence=72)
    _put_market(market, config, at=resumed_at, sequence=73)
    _configured, service = _candidate_service(config, engine)

    result = service.produce(
        as_of=NOW,
        decision_at=resumed_at,
        cause_id="recovered-expired-capital-cause",
        trigger_batch_id="recovered-expired-capital-batch",
        symbol="BTCUSDT",
        trigger_types=("FORECAST_CADENCE",),
    )

    assert result.outcome.value == "PLANNED"
    assert result.trade_plan is not None
    assert result.trade_plan.groups == ()
    target = load_portfolio_target(
        engine.connect().execute(select(portfolio_targets.c.payload)).scalar_one()
    )
    assert target.as_of == resumed_at
    assert target.sleeves == ()
    assert target.reason_codes == ("CASH_SELECTED_FORECAST_INVALID",)
    assert target.candidate_evaluations is not None
    assert target.candidate_evaluations[0].validity_reason_codes == (
        "FORECAST_TIME_WINDOW_INVALID",
    )
    record = CapitalCycleRecord.model_validate(
        engine.connect().execute(select(capital_cycle_records.c.payload)).scalar_one()
    )
    assert record.forecast_ids == (target.candidate_evaluations[0].forecast_id,)
    assert record.target_id == target.target_id
    order_count = engine.connect().execute(
        select(func.count()).select_from(mock_product_orders)
    ).scalar_one()
    assert order_count == 0


def test_recovery_projects_current_account_after_an_interrupted_prior_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    resumed_at = NOW + timedelta(minutes=31)
    _put_market(market, config, at=NOW, sequence=74)
    _put_market(market, config, at=resumed_at, sequence=75)
    _configured, service = _candidate_service(config, engine)
    original_run = service._decisions.run

    def interrupt_after_account_projection(**_kwargs):
        raise RuntimeError("simulated interruption after account projection")

    monkeypatch.setattr(service._decisions, "run", interrupt_after_account_projection)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        service.produce(
            as_of=NOW,
            cause_id="interrupted-capital-cause",
            trigger_batch_id="interrupted-capital-batch",
            symbol="BTCUSDT",
            trigger_types=("FORECAST_CADENCE",),
        )
    store = SqlPortfolioStore(engine)
    interrupted_account = store.head_account(portfolio_id="primary")
    assert interrupted_account is not None
    assert interrupted_account.as_of == NOW

    monkeypatch.setattr(service._decisions, "run", original_run)
    result = service.produce(
        as_of=NOW,
        decision_at=resumed_at,
        cause_id="interrupted-capital-cause",
        trigger_batch_id="interrupted-capital-batch",
        symbol="BTCUSDT",
        trigger_types=("FORECAST_CADENCE",),
    )

    assert result.outcome.value == "PLANNED"
    assert result.trade_plan is not None and result.trade_plan.groups == ()
    target = load_portfolio_target(
        engine.connect().execute(select(portfolio_targets.c.payload)).scalar_one()
    )
    current_account = store.head_account(portfolio_id="primary")
    assert current_account is not None
    assert current_account.as_of == target.as_of == resumed_at
    assert current_account.snapshot_id != interrupted_account.snapshot_id
    assert current_account.cycle_id != target.cycle_id
    assert target.account_snapshot_id == current_account.snapshot_id
    assert target.reason_codes == ("CASH_SELECTED_FORECAST_INVALID",)


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

    config, service = _candidate_service(config, engine, raw_score=Decimal("0"))
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
    assert economics.candidate_id
    assert economics.payoff_projection_id is None
    assert economics.target_legs == (
        ("BINANCE:USD_M_PERPETUAL:BTCUSDT", "BTCUSDT", "USD_M_PERPETUAL", "LONG"),
    )
    assert economics.estimated_cost_bps == (
        economics.fee_bps
        + economics.exit_spread_bps
        + economics.depth_slippage_bps
    )
    serialized = serialize_capital_activity((activity,))["actions"][0]
    assert serialized["candidate_economics"][0]["net_bps"] == str(economics.net_bps)
    assert serialized["candidate_economics"][0]["candidate_id"] == economics.candidate_id
    assert serialized["candidate_economics"][0]["target_legs"] == [
        {
            "instrument": "BINANCE:USD_M_PERPETUAL:BTCUSDT",
            "symbol": "BTCUSDT",
            "product": "USD_M_PERPETUAL",
            "direction": "LONG",
        }
    ]
    assert serialized["candidate_economics_recorded"] is True
    assert serialized["candidate_summaries"] == [
        {
            "candidate_id": economics.candidate_id,
            "outcome_family_id": economics.outcome_family_id,
            "target_legs": serialized["candidate_economics"][0]["target_legs"],
            "net_bps": str(economics.net_bps),
            "desired_gross_notional": str(economics.desired_gross_notional),
            "validity_reason_codes": [],
        }
    ]
    assert serialized["analysis_input"] is None
    assert "analysis_input" not in serialized["candidate_economics"][0]
    with_input = serialize_capital_activity(
        (replace(activity, analysis_input={"purpose": "FORECAST_ESTIMATE"}),)
    )["actions"][0]
    assert with_input["analysis_input"] == {"purpose": "FORECAST_ESTIMATE"}
    assert "analysis_input" not in with_input["candidate_economics"][0]

    summary_activity = CapitalDashboardReader(engine, config).activity(
        include_details=False
    )[0]
    assert len(summary_activity.candidate_economics) == 1
    assert summary_activity.analysis_input is None
    summary_payload = serialize_capital_activity(
        (summary_activity,),
        include_details=False,
    )["actions"][0]
    assert "candidate_economics" not in summary_payload
    assert "analysis_input" not in summary_payload
    detail = CapitalDashboardReader(engine, config).activity_detail(
        summary_activity.activity_id
    )
    assert detail is not None
    assert detail.candidate_economics[0].candidate_id == economics.candidate_id
    assert CapitalDashboardReader(engine, config).activity_detail("missing") is None

    assert result.trade_plan is not None
    target = SqlPortfolioStore(engine).target_for_cycle(result.trade_plan.cycle_id)
    assert target is not None and target.candidate_evaluations is not None
    frozen = target.candidate_evaluations[0]
    changed_specs = tuple(
        item.model_copy(update={"fee_bps": Decimal("999")})
        for item in config.capital.execution_specs
    )
    changed_config = config.model_copy(
        update={
            "capital": config.capital.model_copy(
                update={
                    "decision": config.capital.decision.model_copy(
                        update={"cost_model_version": "changed-current-policy"}
                    ),
                    "execution_specs": changed_specs,
                }
            )
        }
    )
    historical = CapitalDashboardReader(engine, changed_config).activity()[0]
    assert historical.candidate_economics[0].net_bps == frozen.decision_net_bps
    assert historical.candidate_economics[0].decision_threshold_bps == frozen.minimum_net_bps


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


def test_single_product_forecast_receives_only_its_executable_quote() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=NOW, sequence=72)
    instrument = next(
        item.instrument
        for item in config.capital.execution_specs
        if item.instrument.key == "BINANCE:USD_M_PERPETUAL:BTCUSDT"
    )
    config, service = _candidate_service(
        config,
        engine,
        raw_score=Decimal("0"),
        target=ForecastTarget.single_long(instrument),
    )

    result = service.produce(
        as_of=NOW,
        cause_id="single-product-candidate-batch",
        trigger_batch_id="single-product-candidate-batch",
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

    missed = NOW + timedelta(days=8)
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
    assert "EXPIRED_FORECAST_EXIT" in dto["decision"]["reason_codes"]
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(portfolio_targets)) == 2
        assert (
            connection.scalar(select(func.count()).select_from(risk_execution_authorizations)) == 2
        )
        assert connection.scalar(select(func.count()).select_from(mock_product_orders)) == 2
    activity = CapitalDashboardReader(engine, config).activity()
    assert activity[0].outcome == "EXECUTED"
    assert "EXPIRED_FORECAST_EXIT" in activity[0].reason_codes
    assert activity[0].position_changes[0].effect == "CLOSE_LONG"


def test_trigger_review_records_an_exit_that_finishes_in_cash() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=NOW, sequence=71)
    config, service = _candidate_service(config, engine)
    opened = service.produce(as_of=NOW)
    assert isinstance(opened, TradePlanExecutionResult)

    review_at = NOW + timedelta(days=8)
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
    assert exit_record.outcome == CapitalCycleOutcome.TARGET_DECIDED
    assert "EXPIRED_FORECAST_EXIT" in exit_record.reason_codes
    activity = CapitalDashboardReader(engine, config).activity()
    assert activity[0].outcome == "EXECUTED"
    assert "EXPIRED_FORECAST_EXIT" in activity[0].reason_codes


def test_current_holding_review_does_not_create_a_new_alpha_target() -> None:
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
    assert record.outcome == CapitalCycleOutcome.HOLD
    assert record.target_id is None
    assert record.execution_authorization_id is None
    assert record.reason_codes == (
        "HOLDING_RISK_REVIEWED",
        "PROGRAMMATIC_RISK_REVIEW",
    )
    activity = CapitalDashboardReader(engine, config).activity()
    assert len(activity) == 1
    assert activity[0].outcome == "EXECUTED"
    assert activity[0].order_count == 1
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(portfolio_targets)) == 1


def test_research_successor_cannot_reauthorize_or_redirect_an_existing_holding() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    base_config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    _put_market(market, base_config, at=NOW, sequence=740)
    capital_config, capital_service = _candidate_service(
        base_config,
        engine,
        maximum_allocation_fraction=Decimal("0.10"),
    )
    opened = capital_service.produce(as_of=NOW)
    assert isinstance(opened, TradePlanExecutionResult)
    assert opened.account.sleeves

    old_source = capital_service._source_by_family[_TEST_FORECAST_FAMILY]
    research_binding = ForecastProducerBinding.create(
        contract_id=old_source.contract.contract_id,
        producer_kind=ForecastProducerKind.PROGRAM,
        producer_id=_TEST_PRODUCER_ID,
        producer_behavior_id="test-capital-research-successor-v1",
        permission=ForecastPermission.RESEARCH,
    )
    research_producer = _FixedMockForecastProducer(
        store=SqlForecastStore(engine),
        contracts=SqlForecastContractStore(engine),
        contract=old_source.contract,
        binding=research_binding,
        raw_score=Decimal("-80"),
    )
    research_at = NOW + timedelta(minutes=5)
    research_forecast = research_producer.produce(as_of=research_at)
    research_config = capital_config.model_copy(
        update={
            "capital": capital_config.capital.model_copy(
                update={
                    "version": "research-only-capital-successor-v1",
                    "candidate_capital_authorizations": (),
                }
            )
        }
    )
    research_source = CapitalForecastSource(
        contract=old_source.contract,
        binding=research_binding,
        producer=research_producer,
        risk_template=old_source.risk_template,
        capital_authorization=None,
    )
    restarted = _assemble_capital_cycle(
        research_config,
        engine,
        forecast_sources=(research_source,),
    )
    assert restarted._forecast_sources == ()
    assert SqlForecastStore(engine).latest_base_for_target(
        target_id=old_source.contract.target.target_id,
        outcome_family_id=_TEST_FORECAST_FAMILY,
        as_of=research_at,
    ) == research_forecast
    historical_support = restarted._latest_forecast(
        source=research_source,
        target_id=old_source.contract.target.target_id,
        as_of=research_at,
    )
    assert historical_support is not None
    assert historical_support.producer_behavior_id == _TEST_PRODUCER_VERSION

    _put_market(market, research_config, at=research_at, sequence=741)
    held = restarted.review(
        _runtime_batch(AnalysisTriggerType.HEARTBEAT, at=research_at)
    )
    assert not isinstance(held, TradePlanExecutionResult)
    assert held.holding_risk_review is not None
    assert held.holding_risk_review.outcome == HoldingRiskOutcome.HOLD
    assert len(SqlPortfolioStore(engine).head_account(portfolio_id="primary").sleeves) == 1

    expired_at = NOW + timedelta(days=8)
    _put_market(market, research_config, at=expired_at, sequence=742)
    exited = restarted.review(
        _runtime_batch(AnalysisTriggerType.HEARTBEAT, at=expired_at)
    )
    assert isinstance(exited, TradePlanExecutionResult)
    assert not exited.account.sleeves
    assert len(exited.groups) == 1
    exit_plan = restarted._plans.plan(exited.plan_id)
    assert exit_plan is not None
    assert all(leg.reduce_only for group in exit_plan.groups for leg in group.legs)


def test_holding_review_cannot_increase_capital_without_a_fresh_forecast() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=NOW, sequence=75)
    config, service = _candidate_service(
        config,
        engine,
        maximum_allocation_fraction=Decimal("0.10"),
    )
    opened = service.produce(as_of=NOW)
    assert isinstance(opened, TradePlanExecutionResult)
    review_at = NOW + timedelta(minutes=5)
    _put_market(market, config, at=review_at, sequence=76)
    upgraded = config.model_copy(
        update={
            "capital": config.capital.model_copy(
                update={
                    "version": "upgraded-capital-behavior",
                    "decision": config.capital.decision.model_copy(
                        update={"version": "upgraded-portfolio-decision"}
                    ),
                }
            )
        }
    )
    config, restarted = _candidate_service(
        upgraded,
        engine,
        maximum_allocation_fraction=Decimal("0.50"),
        emit=False,
    )

    reviewed = restarted.review(
        _runtime_batch(AnalysisTriggerType.WORLD_MODEL_UPDATED, at=review_at)
    )

    assert not isinstance(reviewed, TradePlanExecutionResult)
    assert reviewed.target is None
    assert reviewed.holding_risk_review is not None
    assert reviewed.holding_risk_review.outcome.value == "HOLD"
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(mock_product_orders)) == 1
        assert connection.scalar(select(func.count()).select_from(portfolio_targets)) == 1


def test_completed_economic_cause_is_not_reopened_by_a_new_capital_behavior() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=NOW, sequence=77)
    _configured, service = _candidate_service(config, engine)
    cause_id = "already-consumed-cadence"

    service.produce(
        as_of=NOW,
        cause_id=cause_id,
        trigger_batch_id=cause_id,
        symbol="BTCUSDT",
        trigger_types=("FORECAST_CADENCE",),
    )

    changed = config.model_copy(
        update={
            "capital": config.capital.model_copy(
                update={
                    "version": "different-capital-behavior",
                    "decision": config.capital.decision.model_copy(
                        update={"version": "different-portfolio-decision"}
                    ),
                }
            )
        }
    )
    _changed, restarted = _candidate_service(changed, engine, emit=False)

    assert restarted.cause_completed(cause_id)
    replayed = restarted.produce(
        as_of=NOW,
        cause_id=cause_id,
        trigger_batch_id=cause_id,
        symbol="BTCUSDT",
        trigger_types=("FORECAST_CADENCE",),
    )
    assert replayed == service.produce(
        as_of=NOW,
        cause_id=cause_id,
        trigger_batch_id=cause_id,
        symbol="BTCUSDT",
        trigger_types=("FORECAST_CADENCE",),
    )
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(capital_cycle_records)) == 1


def test_same_forecasts_cannot_add_risk_after_portfolio_decision_upgrade() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    market = SqlMarketDataStore(engine)
    _put_market(market, config, at=NOW, sequence=78)
    configured, service = _candidate_service(
        config,
        engine,
        raw_score=Decimal("80"),
        maximum_allocation_fraction=Decimal("0.50"),
    )
    opened = service.produce(
        as_of=NOW,
        cause_id="decision-v16-cause",
        trigger_batch_id="decision-v16-cause",
        symbol="BTCUSDT",
        trigger_types=("FORECAST_CADENCE",),
    )
    assert isinstance(opened, TradePlanExecutionResult)
    with engine.connect() as connection:
        opened_target = load_portfolio_target(
            connection.execute(select(portfolio_targets.c.payload)).scalar_one()
        )
    opened_notional = opened_target.sleeves[0].desired_gross_notional
    assert opened_notional > 0

    upgraded = configured.model_copy(
        update={
            "capital": configured.capital.model_copy(
                update={
                    "version": "capital-release-v17",
                    "decision": configured.capital.decision.model_copy(
                        update={"version": "portfolio-decision-v17"}
                    ),
                }
            )
        }
    )
    _upgraded, restarted = _candidate_service(
        upgraded,
        engine,
        raw_score=Decimal("80"),
        maximum_allocation_fraction=Decimal("0.50"),
    )
    replayed = restarted.produce(
        as_of=NOW,
        cause_id="release-rebound-cause",
        trigger_batch_id="release-rebound-cause",
        symbol="BTCUSDT",
        trigger_types=("FORECAST_CADENCE",),
    )

    assert replayed == opened
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(mock_product_orders)) == 1
        assert connection.scalar(select(func.count()).select_from(portfolio_targets)) == 1
        records = tuple(
            CapitalCycleRecord.model_validate(item)
            for item in connection.execute(
                select(capital_cycle_records.c.payload).order_by(
                    capital_cycle_records.c.evaluated_at,
                    capital_cycle_records.c.record_id,
                )
            ).scalars()
        )
    assert len(records) == 2
    original = next(item for item in records if item.cause_id == "decision-v16-cause")
    rebound = next(item for item in records if item.cause_id == "release-rebound-cause")
    assert rebound.outcome == CapitalCycleOutcome.FORECAST_ALREADY_DECIDED
    assert rebound.forecast_ids == original.forecast_ids
    assert rebound.target_id == original.target_id


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
