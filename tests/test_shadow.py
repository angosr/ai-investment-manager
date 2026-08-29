from __future__ import annotations

from datetime import UTC, datetime, timedelta

from investment_manager.decision_cycle import service as decision_service
from investment_manager.decision_cycle.capital import CapitalTriggerConsumer
from investment_manager.decision_cycle.trigger import TriggerDispatchBuilder
from investment_manager.scheduling.models import (
    AnalysisTriggerType,
    build_initial_trigger_plan,
    build_trigger_batch,
    build_trigger_event,
)
from investment_manager.settings import AppConfig
from investment_manager.state.decision.application import (
    DecisionPacketPreparationResult,
    PacketPreparationStatus,
)

NOW = datetime(2026, 8, 18, 12, 10, 30, tzinfo=UTC)


def _shadow_config(app_config) -> AppConfig:
    raw = {
        name: getattr(app_config, name).model_dump(mode="python") for name in AppConfig.model_fields
    }
    raw["pipeline"] = {"version": app_config.pipeline.version}
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
    raw["market_data"]["reference_price_symbols"] = ()
    raw["market_data"]["cross_venue_spot"]["products"] = (
        raw["market_data"]["cross_venue_spot"]["products"][0],
    )
    raw["assessment"]["mandate"]["observation_assets"] = (
        raw["assessment"]["mandate"]["observation_assets"][0],
    )
    raw["assessment"]["mandate"]["observation_references"] = ()
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
    def __init__(self, results=()) -> None:
        self.as_of = None
        self.calls = []
        self.results = results

    def produce(self, *, as_of, cause=None):
        self.as_of = as_of
        self.calls.append((as_of, cause))
        return self.results


class ForecastResultStub:
    def __init__(self, information_cutoff_at: datetime) -> None:
        self.information_cutoff_at = information_cutoff_at


class RecordingPosteriorPreparation:
    def __init__(self) -> None:
        self.calls = []
        self.activation_count = 0

    def activate(self):
        self.activation_count += 1
        return ()

    def reserve(self, prior_results, *, as_of):
        self.calls.append((prior_results, as_of))
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


class RecordingBatchRecorder:
    def __init__(self) -> None:
        self.batch = None

    def record_batch(self, batch, *, analysis_submitted_at):
        self.batch = batch
        return True

    def admit_analysis_call(self, batch, *, requested_at):
        raise AssertionError("无 AI dispatch 时不应请求调用准入")


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
        symbol=config.assessment.review_trigger_symbol,
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

    assert "strategy" not in type(config).model_fields
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
        symbol=plan.symbol,
        pipeline_id=config.pipeline.version,
        occurred_at=NOW,
        observed_at=NOW,
        priority=1,
        dedup_key="forecast-slot-1",
    )
    producer = RecordingForecastProducer()
    posterior = RecordingPosteriorPreparation()
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
        posterior_preparation=posterior,
        program_batch_consumers=(consumer,),
    ).build(batch)

    assert dispatches == ()
    assert producer.as_of == NOW
    assert posterior.activation_count == 1
    assert posterior.calls == []
    assert consumer.batch == batch


def test_trigger_builder_groups_recovered_cadence_results_by_cutoff(app_config) -> None:
    config = _shadow_config(app_config)
    plan = build_initial_trigger_plan(
        symbol=config.assessment.review_trigger_symbol,
        pipeline_id=config.pipeline.version,
        manifest_id="manifest-v1",
        updated_at=NOW,
        heartbeat_seconds=900,
    )
    trigger = build_trigger_event(
        trigger_type=AnalysisTriggerType.FORECAST_SLOT_DUE,
        symbol=plan.symbol,
        pipeline_id=plan.pipeline_id,
        occurred_at=NOW,
        observed_at=NOW,
        priority=1,
        dedup_key="recovered-forecast-slots",
    )
    earlier = NOW - timedelta(days=3)
    producer = RecordingForecastProducer(
        (
            ForecastResultStub(earlier),
            ForecastResultStub(earlier),
            ForecastResultStub(NOW),
            ForecastResultStub(NOW),
        )
    )
    posterior = RecordingPosteriorPreparation()
    batch = build_trigger_batch(
        plan=plan,
        triggers=(trigger,),
        created_at=NOW,
        deadline=NOW + timedelta(minutes=5),
    )

    TriggerDispatchBuilder(
        config=config,
        packet_preparation=RecordingPacketPreparation(),
        assessment_history=EmptyAssessmentHistory(),
        program_forecast_producers=(producer,),
        posterior_preparation=posterior,
    ).build(batch)

    assert tuple(
        tuple(item.information_cutoff_at for item in results) for results, _as_of in posterior.calls
    ) == ((earlier, earlier), (NOW, NOW))
    assert posterior.activation_count == 1


def test_non_owner_trigger_does_not_race_joint_cadence_producer(app_config) -> None:
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
    producer = RecordingForecastProducer()
    posterior = RecordingPosteriorPreparation()

    TriggerDispatchBuilder(
        config=config,
        program_forecast_producers=(producer,),
        posterior_preparation=posterior,
    ).build(
        build_trigger_batch(
            plan=plan,
            triggers=(trigger,),
            created_at=NOW,
            deadline=NOW + timedelta(minutes=5),
        )
    )

    assert producer.calls == []
    assert posterior.activation_count == 0


def test_qualified_information_event_creates_separate_material_forecast_panel(
    app_config,
) -> None:
    config = _shadow_config(app_config)
    plan = build_initial_trigger_plan(
        symbol=config.assessment.review_trigger_symbol,
        pipeline_id=config.pipeline.version,
        manifest_id="manifest-v1",
        updated_at=NOW,
        heartbeat_seconds=900,
    )
    evidence_id = "e" * 64
    trigger = build_trigger_event(
        trigger_type=AnalysisTriggerType.INTELLIGENCE_INSERTED,
        symbol=plan.symbol,
        pipeline_id=plan.pipeline_id,
        occurred_at=NOW,
        observed_at=NOW,
        priority=85,
        dedup_key="qualified-official-event",
        evidence_ids=(evidence_id,),
    )
    producer = RecordingForecastProducer()
    batch = build_trigger_batch(
        plan=plan,
        triggers=(trigger,),
        created_at=NOW,
        deadline=NOW + timedelta(minutes=5),
    )

    TriggerDispatchBuilder(
        config=config,
        program_forecast_producers=(producer,),
    ).build(batch)

    assert len(producer.calls) == 2
    assert producer.calls[0] == (NOW, None)
    material_cause = producer.calls[1][1]
    assert material_cause is not None
    assert material_cause.origin.value == "MATERIAL_STATE"
    assert material_cause.trigger_refs == (evidence_id,)


def test_evidence_free_agent_review_does_not_create_material_forecast_panel(
    app_config,
) -> None:
    config = _shadow_config(app_config)
    plan = build_initial_trigger_plan(
        symbol=config.assessment.review_trigger_symbol,
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
        priority=90,
        dedup_key="world-model-review",
        review_reason="到期复核既有世界机制",
    )
    producer = RecordingForecastProducer()
    batch = build_trigger_batch(
        plan=plan,
        triggers=(trigger,),
        created_at=NOW,
        deadline=NOW + timedelta(minutes=5),
    )

    TriggerDispatchBuilder(
        config=config,
        program_forecast_producers=(producer,),
    ).build(batch)

    assert producer.calls == [(NOW, None)]


def test_trigger_service_assembly_passes_enabled_forecast_producer_as_tuple(
    app_config,
    monkeypatch,
) -> None:
    config = _shadow_config(app_config).model_copy(
        update={
            "capital": app_config.capital.model_copy(update={"enabled": False}),
            "assessment": app_config.assessment.model_copy(update={"enabled": False}),
            "outcome_evaluation": app_config.outcome_evaluation.model_copy(
                update={
                    "forecast_prior": (
                        app_config.outcome_evaluation.forecast_prior.model_copy(
                            update={"enabled": True}
                        )
                    )
                }
            ),
        }
    )
    producer = RecordingForecastProducer()
    recorder = RecordingBatchRecorder()
    monkeypatch.setattr(
        decision_service,
        "_assemble_forecast_prior",
        lambda **_kwargs: producer,
    )
    assembly = decision_service.assemble_trigger_service(
        config=config,
        manifest=object(),
        engine=object(),
        repository=recorder,
    )
    plan = build_initial_trigger_plan(
        symbol="BTCUSDT",
        pipeline_id=config.pipeline.version,
        manifest_id="manifest-v1",
        updated_at=NOW,
        heartbeat_seconds=900,
    )
    batch = build_trigger_batch(
        plan=plan,
        triggers=(
            build_trigger_event(
                trigger_type=AnalysisTriggerType.FORECAST_SLOT_DUE,
                symbol=plan.symbol,
                pipeline_id=plan.pipeline_id,
                occurred_at=NOW,
                observed_at=NOW,
                priority=1,
                dedup_key="assembled-forecast-slot-1",
            ),
        ),
        created_at=NOW,
        deadline=NOW + timedelta(minutes=5),
    )

    assert assembly.activities.builder.build(batch) == ()
    assert producer.as_of == NOW
    assert recorder.batch == batch


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
        program_batch_consumers=(CapitalTriggerConsumer(capital),),
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
