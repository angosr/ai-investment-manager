from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine

from investment_manager.forecast.contract_repository import SqlForecastContractStore
from investment_manager.forecast.contracts import (
    MATERIAL_SLOT_POLICY_VERSION,
    ForecastDecisionSlot,
    ForecastPermission,
    ForecastProducerBinding,
    ForecastProducerKind,
    ForecastSlotCause,
)
from investment_manager.forecast.program.baseline import load_forecast_baseline
from investment_manager.forecast.program.prior import (
    RollingPriorForecastProducer,
    build_prior_targets,
    prior_slot_at_or_before,
)
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.forecast.results import BaseForecast
from investment_manager.market.models import MarketQuote
from investment_manager.market.repository import InMemoryMarketDataStore
from investment_manager.schema import create_schema


def _artifact():
    root = Path(__file__).resolve().parents[1]
    return load_forecast_baseline(
        root
        / "evidence/forecast-baselines/forecast_baseline_7edf2cf090b47cdad2e5.json"
    )


def test_prior_producer_records_one_research_forecast_per_target_idempotently() -> None:
    slot_at = datetime(2026, 8, 30, tzinfo=UTC)
    observed_at = slot_at + timedelta(minutes=1)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    market = InMemoryMarketDataStore()
    for symbol, price in (("BTCUSDT", "110000"), ("PAXGUSDT", "3500")):
        market.put_quote(
            MarketQuote(
                quote_id=f"{symbol}-slot-quote",
                symbol=symbol,
                observed_at=slot_at,
                bid=price,
                bid_quantity="1",
                ask=str(float(price) + 1),
                ask_quantity="1",
                source="test",
            )
        )
    producer = RollingPriorForecastProducer(
        artifact=_artifact(),
        market=market,
        contracts=SqlForecastContractStore(engine),
        forecasts=SqlForecastStore(engine),
        outcome_evaluation_version="forecast-target-outcome-v1",
        activated_at=slot_at - timedelta(hours=1),
        maximum_quote_age_seconds=300,
        clock=lambda: observed_at,
    )

    first = producer.produce(as_of=observed_at)
    repeated = producer.produce(as_of=observed_at + timedelta(minutes=1))

    assert len(first) == len(repeated) == 2
    assert all(isinstance(item, BaseForecast) for item in first)
    assert tuple(item.forecast_id for item in first) == tuple(
        item.forecast_id for item in repeated
    )
    assert all(item.program_input_json is not None for item in first)
    assert all(item.analysis_input_json is None for item in first)


def test_prior_targets_are_one_joint_portfolio_behavior() -> None:
    targets = build_prior_targets(_artifact())

    assert len(targets) == 2
    assert len({item.binding.producer_behavior_id for item in targets}) == 1


def test_prior_producer_records_material_event_as_independent_slot() -> None:
    event_at = datetime(2026, 8, 29, 14, tzinfo=UTC)
    completed_at = event_at + timedelta(minutes=1)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    market = InMemoryMarketDataStore()
    for symbol, price in (("BTCUSDT", "110000"), ("PAXGUSDT", "3500")):
        market.put_quote(
            MarketQuote(
                quote_id=f"{symbol}-event-quote",
                symbol=symbol,
                observed_at=event_at,
                bid=price,
                bid_quantity="1",
                ask=str(float(price) + 1),
                ask_quantity="1",
                source="test",
            )
        )
    contracts = SqlForecastContractStore(engine)
    producer = RollingPriorForecastProducer(
        artifact=_artifact(),
        market=market,
        contracts=contracts,
        forecasts=SqlForecastStore(engine),
        outcome_evaluation_version="forecast-target-outcome-v1",
        activated_at=event_at - timedelta(hours=1),
        maximum_quote_age_seconds=300,
        clock=lambda: completed_at,
    )
    cause = ForecastSlotCause.material_state(
        policy_version=MATERIAL_SLOT_POLICY_VERSION,
        trigger_refs=("official-event-1",),
    )

    results = producer.produce(as_of=event_at, cause=cause)
    repeated = producer.produce(as_of=event_at + timedelta(minutes=5), cause=cause)
    replayed_under_new_policy = producer.produce(
        as_of=event_at + timedelta(minutes=10),
        cause=ForecastSlotCause.material_state(
            policy_version="replacement-material-policy-v1",
            trigger_refs=("official-event-1",),
        ),
    )

    assert len(results) == len(repeated) == 2
    assert replayed_under_new_policy == ()
    assert all(isinstance(item, BaseForecast) for item in results)
    assert tuple(item.forecast_id for item in results) == tuple(
        item.forecast_id for item in repeated
    )
    slots = tuple(contracts.slot(item.decision_slot_id) for item in results)
    assert all(slot is not None and slot.slot_as_of == event_at for slot in slots)
    assert all(slot is not None and slot.cause == cause for slot in slots)


def test_new_behavior_cannot_backfill_an_existing_material_event() -> None:
    event_at = datetime(2026, 8, 29, 14, tzinfo=UTC)
    activation_at = event_at + timedelta(hours=1)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    contracts = SqlForecastContractStore(engine)
    cause = ForecastSlotCause.material_state(
        policy_version=MATERIAL_SLOT_POLICY_VERSION,
        trigger_refs=("official-event-before-activation",),
    )
    for target in build_prior_targets(_artifact()):
        contracts.record_contract(target.contract)
        old_binding = ForecastProducerBinding.create(
            contract_id=target.contract.contract_id,
            producer_kind=ForecastProducerKind.PROGRAM,
            producer_id="retired-prior",
            producer_behavior_id="retired-prior-behavior",
            permission=ForecastPermission.RESEARCH,
        )
        contracts.record_binding(old_binding, activated_at=event_at - timedelta(hours=1))
        contracts.record_slot(
            ForecastDecisionSlot.create(
                target.contract,
                slot_as_of=event_at,
                cutoff_prices=(),
                cause=cause,
            ),
            binding=old_binding,
        )
    producer = RollingPriorForecastProducer(
        artifact=_artifact(),
        market=InMemoryMarketDataStore(),
        contracts=contracts,
        forecasts=SqlForecastStore(engine),
        outcome_evaluation_version="forecast-target-outcome-v1",
        activated_at=activation_at,
        maximum_quote_age_seconds=300,
        clock=lambda: activation_at + timedelta(hours=1),
    )

    assert producer.produce(
        as_of=activation_at + timedelta(hours=1),
        cause=cause,
    ) == ()


def test_prior_cadence_never_backfills_before_activation() -> None:
    as_of = datetime(2026, 8, 29, 12, tzinfo=UTC)
    assert prior_slot_at_or_before(as_of) == datetime(2026, 8, 27, tzinfo=UTC)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    producer = RollingPriorForecastProducer(
        artifact=_artifact(),
        market=InMemoryMarketDataStore(),
        contracts=SqlForecastContractStore(engine),
        forecasts=SqlForecastStore(engine),
        outcome_evaluation_version="forecast-target-outcome-v1",
        activated_at=datetime(2026, 8, 29, tzinfo=UTC),
        maximum_quote_age_seconds=300,
    )

    assert producer.produce(as_of=as_of) == ()
