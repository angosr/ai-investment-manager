from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine

from investment_manager.decision_cycle.capital import CapitalTriggerConsumer
from investment_manager.decision_cycle.trigger import TriggerDispatchBuilder
from investment_manager.legacy.cycle import AnalysisCycle
from investment_manager.legacy.exchange import MockExchange
from investment_manager.legacy.repository import SqlFactLedger
from investment_manager.legacy.shadow import SqlShadowStateReader
from investment_manager.risk.budget import SqlRiskBudgetStore
from investment_manager.scheduling.models import (
    AnalysisTriggerType,
    build_initial_trigger_plan,
    build_trigger_batch,
    build_trigger_event,
)
from investment_manager.schema import create_schema
from investment_manager.settings import AppConfig
from investment_manager.state.decision.application import (
    DecisionPacketPreparationResult,
    PacketPreparationStatus,
)

NOW = datetime(2026, 8, 18, 12, 10, 30, tzinfo=UTC)


def _shadow_config(app_config) -> AppConfig:
    raw = app_config.model_dump(mode="python")
    raw["deployment"] = {
        "version": "deployment-shadow-test-v1",
        "stage": "SHADOW",
        "shadow_market_data_enabled": True,
        "testnet_order_submission_enabled": False,
        "live_order_submission_enabled": False,
        "credential_profile": None,
        "manual_approval_ref": None,
    }
    raw["market_data"]["symbols"] = ("BTCUSDT",)
    raw["market_data"]["cross_venue_spot"]["products"] = (
        raw["market_data"]["cross_venue_spot"]["products"][0],
    )
    raw["assessment"]["mandate"]["assets"] = (raw["assessment"]["mandate"]["assets"][0],)
    raw["decision_state"]["official_fact_policy"]["affected_assets"] = ("BTC",)
    return AppConfig.model_validate(raw)


class RecordingPacketPreparation:
    def __init__(self) -> None:
        self.intelligence_evidence_ids: tuple[str, ...] | None = None
        self.market_shock_symbols: tuple[str, ...] | None = None
        self.review_requests = None

    def prepare(self, **kwargs):
        self.intelligence_evidence_ids = kwargs["intelligence_evidence_ids"]
        self.market_shock_symbols = kwargs["market_shock_symbols"]
        self.review_requests = kwargs["review_requests"]
        return DecisionPacketPreparationResult(
            status=PacketPreparationStatus.NO_MATERIAL_DELTA,
            reason_code="NO_MATERIAL_STATE_CHANGE",
            state_id="state-1",
        )


class RecordingForecastProducer:
    def __init__(self) -> None:
        self.as_of = None

    def produce(self, *, as_of):
        self.as_of = as_of
        return None


class EmptyAssessmentHistory:
    def latest_before(self, *, analysis_scope, as_of):
        return None

    def observe_mechanisms(self, *, assessment, packet):
        return ()

    def mechanism_observations(self, assessment_id):
        return ()


class RecordingBatchConsumer:
    def __init__(self) -> None:
        self.batch = None

    def consume(self, batch):
        self.batch = batch
        return None


class RecordingCapital:
    portfolio_id = "primary"

    def __init__(self) -> None:
        self.produce_calls = []
        self.review_calls = []
        self.recovered_slots = []
        self.missed_slots = []

    def cause_completed(self, cause_id):
        return any(item["cause_id"] == cause_id for item in self.produce_calls)

    def forecast_outputs_complete(self, **kwargs):
        del kwargs
        return False

    def produce(self, **kwargs):
        self.produce_calls.append(kwargs)

    def review(self, batch):
        self.review_calls.append(batch)

    def recover_missed_forecasts(self, **kwargs):
        self.recovered_slots.append(kwargs)

    def record_missed_forecast(self, **kwargs):
        self.missed_slots.append(kwargs)


def test_trigger_builder_does_not_dispatch_retired_analysis_cycle(app_config) -> None:
    config = _shadow_config(app_config)
    plan = build_initial_trigger_plan(
        symbol="BTCUSDT",
        pipeline_id=config.pipeline.version,
        manifest_id="manifest-v1",
        updated_at=NOW,
        heartbeat_seconds=900,
    )
    trigger = build_trigger_event(
        trigger_type=AnalysisTriggerType.MARKET_SHOCK,
        symbol="BTCUSDT",
        pipeline_id=config.pipeline.version,
        occurred_at=NOW,
        observed_at=NOW,
        priority=90,
        dedup_key="shock-1",
    )
    batch = build_trigger_batch(
        plan=plan,
        triggers=(trigger,),
        created_at=NOW,
        deadline=NOW + timedelta(minutes=5),
    )
    dispatches = TriggerDispatchBuilder(
        config=config,
    ).build(batch)

    assert config.strategy.enabled
    assert dispatches == ()


def test_trigger_builder_advances_program_forecast_without_ai_dispatch(
    app_config,
) -> None:
    config = _shadow_config(app_config)
    plan = build_initial_trigger_plan(
        symbol="BTCUSDT",
        pipeline_id=config.pipeline.version,
        manifest_id="manifest-v1",
        updated_at=NOW,
        heartbeat_seconds=900,
    )
    trigger = build_trigger_event(
        trigger_type=AnalysisTriggerType.FORECAST_SLOT_DUE,
        symbol="BTCUSDT",
        pipeline_id=config.pipeline.version,
        occurred_at=NOW,
        observed_at=NOW,
        priority=1,
        dedup_key="forecast-slot-1",
    )
    producer = RecordingForecastProducer()
    consumer = RecordingBatchConsumer()
    batch = build_trigger_batch(
        plan=plan,
        triggers=(trigger,),
        created_at=NOW,
        deadline=NOW + timedelta(minutes=5),
    )

    dispatches = TriggerDispatchBuilder(
        config=config,
        program_forecast_producers=(producer,),
        program_batch_consumers=(consumer,),
    ).build(batch)

    assert dispatches == ()
    assert producer.as_of == NOW
    assert consumer.batch == batch


def test_heartbeat_reviews_consumers_without_updating_world_model(app_config) -> None:
    config = _shadow_config(app_config).model_copy(
        update={
            "assessment": app_config.assessment.model_copy(update={"enabled": True}),
            "codex_runtime": app_config.codex_runtime.model_copy(update={"enabled": True}),
        }
    )
    plan = build_initial_trigger_plan(
        symbol="BTCUSDT",
        pipeline_id=config.pipeline.version,
        manifest_id="manifest-v1",
        updated_at=NOW,
        heartbeat_seconds=900,
    )
    trigger = build_trigger_event(
        trigger_type=AnalysisTriggerType.HEARTBEAT,
        symbol=plan.symbol,
        pipeline_id=plan.pipeline_id,
        occurred_at=NOW,
        observed_at=NOW,
        priority=1,
        dedup_key="heartbeat-review-only",
    )
    preparation = RecordingPacketPreparation()
    consumer = RecordingBatchConsumer()
    batch = build_trigger_batch(
        plan=plan,
        triggers=(trigger,),
        created_at=NOW,
        deadline=NOW + timedelta(minutes=5),
    )

    dispatches = TriggerDispatchBuilder(
        config=config,
        packet_preparation=preparation,
        assessment_history=EmptyAssessmentHistory(),
        program_batch_consumers=(consumer,),
    ).build(batch)

    assert dispatches == ()
    assert preparation.intelligence_evidence_ids is None
    assert consumer.batch == batch


def test_capital_trigger_consumer_uses_one_stable_context_cadence_slot(app_config) -> None:
    config = _shadow_config(app_config)
    plan = build_initial_trigger_plan(
        symbol="BTCUSDT",
        pipeline_id=config.pipeline.version,
        manifest_id="manifest-v1",
        updated_at=NOW,
        heartbeat_seconds=900,
    )
    capital = RecordingCapital()
    consumer = CapitalTriggerConsumer(
        capital,
        context_cadence_minutes=240,
        context_completion_deadline_seconds=1500,
    )
    for index, created_at in enumerate((NOW, NOW + timedelta(minutes=20)), start=1):
        trigger = build_trigger_event(
            trigger_type=AnalysisTriggerType.FORECAST_SLOT_DUE,
            symbol=plan.symbol,
            pipeline_id=plan.pipeline_id,
            occurred_at=created_at,
            observed_at=created_at,
            priority=1,
            dedup_key=f"forecast-slot-due-{index}",
        )
        consumer.consume(
            build_trigger_batch(
                plan=plan,
                triggers=(trigger,),
                created_at=created_at,
                deadline=created_at + timedelta(minutes=5),
            )
        )

    assert [item["as_of"] for item in capital.produce_calls] == [
        datetime(2026, 8, 18, 12, tzinfo=UTC),
    ]
    assert len(capital.review_calls) == 1


def test_capital_trigger_consumer_does_not_backfill_an_expired_cadence_slot(
    app_config,
) -> None:
    config = _shadow_config(app_config)
    plan = build_initial_trigger_plan(
        symbol="BTCUSDT",
        pipeline_id=config.pipeline.version,
        manifest_id="manifest-v1",
        updated_at=NOW,
        heartbeat_seconds=900,
    )
    created_at = NOW.replace(minute=50)
    trigger = build_trigger_event(
        trigger_type=AnalysisTriggerType.HEARTBEAT,
        symbol=plan.symbol,
        pipeline_id=plan.pipeline_id,
        occurred_at=created_at,
        observed_at=created_at,
        priority=1,
        dedup_key="late-cadence-heartbeat",
    )
    capital = RecordingCapital()

    CapitalTriggerConsumer(
        capital,
        context_cadence_minutes=240,
        context_completion_deadline_seconds=1500,
    ).consume(
        build_trigger_batch(
            plan=plan,
            triggers=(trigger,),
            created_at=created_at,
            deadline=created_at + timedelta(minutes=5),
        )
    )

    assert not capital.produce_calls
    assert len(capital.review_calls) == 1


def test_capital_trigger_consumer_has_one_portfolio_scope_owner(app_config) -> None:
    config = _shadow_config(app_config)
    plan = build_initial_trigger_plan(
        symbol="ETHUSDT",
        pipeline_id=config.pipeline.version,
        manifest_id="manifest-v1",
        updated_at=NOW,
        heartbeat_seconds=900,
    )
    trigger = build_trigger_event(
        trigger_type=AnalysisTriggerType.HEARTBEAT,
        symbol=plan.symbol,
        pipeline_id=plan.pipeline_id,
        occurred_at=NOW,
        observed_at=NOW,
        priority=1,
        dedup_key="non-owner-heartbeat",
    )
    capital = RecordingCapital()

    result = CapitalTriggerConsumer(
        capital,
        context_cadence_minutes=240,
        context_completion_deadline_seconds=1500,
        owner_symbol="BTCUSDT",
    ).consume(
        build_trigger_batch(
            plan=plan,
            triggers=(trigger,),
            created_at=NOW,
            deadline=NOW + timedelta(minutes=5),
        )
    )

    assert result is None
    assert not capital.produce_calls
    assert not capital.review_calls


def test_world_model_update_reviews_capital_without_creating_a_forecast_slot(
    app_config,
) -> None:
    config = _shadow_config(app_config).model_copy(
        update={
            "assessment": app_config.assessment.model_copy(update={"enabled": True}),
            "codex_runtime": app_config.codex_runtime.model_copy(update={"enabled": True}),
        }
    )
    plan = build_initial_trigger_plan(
        symbol="BTCUSDT",
        pipeline_id=config.pipeline.version,
        manifest_id="manifest-v1",
        updated_at=NOW,
        heartbeat_seconds=900,
    )
    trigger = build_trigger_event(
        trigger_type=AnalysisTriggerType.WORLD_MODEL_UPDATED,
        symbol=plan.symbol,
        pipeline_id=plan.pipeline_id,
        occurred_at=NOW,
        observed_at=NOW,
        priority=100,
        dedup_key="world-model-1",
        evidence_ids=("world-model-1",),
    )
    capital = RecordingCapital()
    batch = build_trigger_batch(
        plan=plan,
        triggers=(trigger,),
        created_at=NOW,
        deadline=NOW + timedelta(minutes=5),
    )

    dispatches = TriggerDispatchBuilder(
        config=config,
        packet_preparation=RecordingPacketPreparation(),
        assessment_history=EmptyAssessmentHistory(),
        program_batch_consumers=(CapitalTriggerConsumer(capital, 240, 1500),),
    ).build(batch)

    assert dispatches == ()
    assert not capital.produce_calls
    assert capital.review_calls == [batch]


def test_trigger_builder_passes_only_intelligence_trigger_evidence_to_packet(app_config) -> None:
    config = _shadow_config(app_config).model_copy(
        update={
            "assessment": app_config.assessment.model_copy(update={"enabled": True}),
            "codex_runtime": app_config.codex_runtime.model_copy(update={"enabled": True}),
        }
    )
    plan = build_initial_trigger_plan(
        symbol="BTCUSDT",
        pipeline_id=config.pipeline.version,
        manifest_id="manifest-v1",
        updated_at=NOW,
        heartbeat_seconds=900,
    )
    intelligence = build_trigger_event(
        trigger_type=AnalysisTriggerType.INTELLIGENCE_INSERTED,
        symbol="BTCUSDT",
        pipeline_id=config.pipeline.version,
        occurred_at=NOW,
        observed_at=NOW,
        priority=90,
        dedup_key="intel-1",
        evidence_ids=("intel-evidence-1",),
    )
    market = build_trigger_event(
        trigger_type=AnalysisTriggerType.MARKET_SHOCK,
        symbol="BTCUSDT",
        pipeline_id=config.pipeline.version,
        occurred_at=NOW,
        observed_at=NOW,
        priority=80,
        dedup_key="market-1",
        evidence_ids=("market-evidence-1",),
    )
    preparation = RecordingPacketPreparation()

    TriggerDispatchBuilder(
        config=config,
        packet_preparation=preparation,
        assessment_history=EmptyAssessmentHistory(),
    ).build(
        build_trigger_batch(
            plan=plan,
            triggers=(market, intelligence),
            created_at=NOW,
            deadline=NOW + timedelta(minutes=5),
        )
    )

    assert preparation.intelligence_evidence_ids == ("intel-evidence-1",)
    assert preparation.market_shock_symbols == ("BTCUSDT",)


def test_non_owner_symbol_cannot_dispatch_portfolio_assessment(app_config) -> None:
    config = _shadow_config(app_config).model_copy(
        update={
            "assessment": app_config.assessment.model_copy(update={"enabled": True}),
            "codex_runtime": app_config.codex_runtime.model_copy(update={"enabled": True}),
        }
    )
    plan = build_initial_trigger_plan(
        symbol="ETHUSDT",
        pipeline_id=config.pipeline.version,
        manifest_id="manifest-v1",
        updated_at=NOW,
        heartbeat_seconds=900,
    )
    trigger = build_trigger_event(
        trigger_type=AnalysisTriggerType.INTELLIGENCE_INSERTED,
        symbol=plan.symbol,
        pipeline_id=plan.pipeline_id,
        occurred_at=NOW,
        observed_at=NOW,
        priority=90,
        dedup_key="portfolio-event-on-non-owner",
        evidence_ids=("portfolio-evidence",),
        affected_symbols=("ETHUSDT",),
    )
    preparation = RecordingPacketPreparation()

    dispatches = TriggerDispatchBuilder(
        config=config,
        packet_preparation=preparation,
        assessment_history=EmptyAssessmentHistory(),
    ).build(
        build_trigger_batch(
            plan=plan,
            triggers=(trigger,),
            created_at=NOW,
            deadline=NOW + timedelta(minutes=5),
        )
    )

    assert dispatches == ()
    assert preparation.intelligence_evidence_ids is None


def test_owner_routed_market_shock_preserves_affected_symbol(app_config) -> None:
    config = _shadow_config(app_config).model_copy(
        update={
            "assessment": app_config.assessment.model_copy(update={"enabled": True}),
            "codex_runtime": app_config.codex_runtime.model_copy(update={"enabled": True}),
        }
    )
    plan = build_initial_trigger_plan(
        symbol=config.assessment.review_trigger_symbol,
        pipeline_id=config.pipeline.version,
        manifest_id="manifest-v1",
        updated_at=NOW,
        heartbeat_seconds=900,
    )
    trigger = build_trigger_event(
        trigger_type=AnalysisTriggerType.MARKET_SHOCK,
        symbol=plan.symbol,
        pipeline_id=plan.pipeline_id,
        occurred_at=NOW,
        observed_at=NOW,
        priority=90,
        dedup_key="eth-shock-on-portfolio-owner",
        affected_symbols=("ETHUSDT",),
    )
    preparation = RecordingPacketPreparation()

    TriggerDispatchBuilder(
        config=config,
        packet_preparation=preparation,
        assessment_history=EmptyAssessmentHistory(),
    ).build(
        build_trigger_batch(
            plan=plan,
            triggers=(trigger,),
            created_at=NOW,
            deadline=NOW + timedelta(minutes=5),
        )
    )

    assert preparation.market_shock_symbols == ("ETHUSDT",)


def test_trigger_builder_preserves_agent_review_reason(app_config) -> None:
    config = _shadow_config(app_config).model_copy(
        update={
            "assessment": app_config.assessment.model_copy(update={"enabled": True}),
            "codex_runtime": app_config.codex_runtime.model_copy(update={"enabled": True}),
        }
    )
    plan = build_initial_trigger_plan(
        symbol="BTCUSDT",
        pipeline_id=config.pipeline.version,
        manifest_id="manifest-v1",
        updated_at=NOW,
        heartbeat_seconds=900,
    )
    trigger = build_trigger_event(
        trigger_type=AnalysisTriggerType.AGENT_WAKEUP,
        symbol=plan.symbol,
        pipeline_id=plan.pipeline_id,
        occurred_at=NOW,
        observed_at=NOW,
        priority=100,
        dedup_key="agent-review-1",
        review_reason="立即检查监管事件对当前风险倾向的影响",
        evidence_ids=("official-event-1",),
    )
    preparation = RecordingPacketPreparation()

    TriggerDispatchBuilder(
        config=config,
        packet_preparation=preparation,
        assessment_history=EmptyAssessmentHistory(),
    ).build(
        build_trigger_batch(
            plan=plan,
            triggers=(trigger,),
            created_at=NOW,
            deadline=NOW + timedelta(minutes=5),
        )
    )

    assert len(preparation.review_requests) == 1
    review = preparation.review_requests[0]
    assert review.reason == trigger.review_reason
    assert review.requested_at == trigger.occurred_at
    assert review.evidence_ids == trigger.evidence_ids


def test_sql_shadow_account_is_projected_from_latest_business_fact(
    app_config, replay_input
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    cycle = AnalysisCycle.with_adapters(
        app_config,
        ledger=SqlFactLedger(engine),
        exchange=MockExchange(app_config.execution),
        risk_budget=SqlRiskBudgetStore(engine),
    )
    result = cycle.run(replay_input)
    assert result.account_after is not None
    reader = SqlShadowStateReader(engine)
    next_as_of = replay_input.market.as_of + timedelta(minutes=1)
    account = reader.account_for_cycle(
        cycle_id="next-cycle",
        as_of=next_as_of,
        initial_quote_balance=app_config.shadow.initial_quote_balance,
    )
    assert account.cycle_id == "next-cycle"
    assert account.quote_balance == result.account_after.quote_balance
    assert reader.entry_orders_today(as_of=next_as_of) == 1
    assert reader.last_cycle_at(symbol="BTCUSDT", as_of=next_as_of) == replay_input.market.as_of

    next_day = reader.account_for_cycle(
        cycle_id="next-day-cycle",
        as_of=replay_input.market.as_of + timedelta(days=1),
        initial_quote_balance=app_config.shadow.initial_quote_balance,
    )
    assert next_day.daily_pnl == Decimal("0")
    assert isinstance(next_day.daily_pnl, Decimal)
