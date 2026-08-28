from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from temporalio.client import Client

from investment_manager.decision_cycle.capital import (
    CapitalCycleService,
    CapitalTriggerConsumer,
    assemble_capital_cycle,
)
from investment_manager.decision_cycle.trigger import (
    TriggerCoordinatorActivities,
    TriggerDispatchBuilder,
)
from investment_manager.execution.cash.repository import SqlCashYieldObservationStore
from investment_manager.execution.cash.service import CashYieldEvidenceService
from investment_manager.execution.cash.source import (
    BinanceReadCredentials,
    BinanceSimpleEarnReadSource,
)
from investment_manager.execution.venue.runtime import assemble_product_execution_runtime
from investment_manager.forecast.context.repository import SqlContextAssessmentStore
from investment_manager.governance.models import ReleaseManifest
from investment_manager.governance.repository import SqlGovernanceRepository
from investment_manager.platform.database import build_engine, require_current_schema
from investment_manager.scheduling.application import ensure_trigger_plans
from investment_manager.scheduling.repository import (
    PostgresOutboxListener,
    PostgresTriggerLeadership,
    SqlTriggerRepository,
)
from investment_manager.scheduling.runtime import (
    TemporalTriggerDispatcher,
    TriggerOutboxDispatcherService,
    TriggerTemporalWorker,
    terminate_inactive_trigger_coordinators,
)
from investment_manager.settings import AppConfig
from investment_manager.state.decision.service import assemble_decision_packet_preparation


@dataclass(frozen=True, slots=True)
class TriggerServiceAssembly:
    """Trigger graph constructed before leadership or durable activation."""

    repository: SqlTriggerRepository
    activities: TriggerCoordinatorActivities
    context_forecast_activation_at: datetime | None


def assemble_trigger_service(
    *,
    config: AppConfig,
    manifest: ReleaseManifest,
    engine,
    repository: SqlTriggerRepository | None = None,
) -> TriggerServiceAssembly:
    """Assemble the production graph without acquiring leadership or creating plans."""

    repository = repository or SqlTriggerRepository(engine, config.trigger)
    capital_consumer = (
        _assemble_capital_consumer(config=config, engine=engine)
        if config.capital.enabled
        else None
    )
    activities = TriggerCoordinatorActivities(
        TriggerDispatchBuilder(
            config=config,
            packet_preparation=(
                assemble_decision_packet_preparation(config, engine)
                if config.assessment.enabled
                else None
            ),
            assessment_history=(
                SqlContextAssessmentStore(engine) if config.assessment.enabled else None
            ),
            batch_recorder=repository,
            program_forecast_producers=(),
            program_batch_consumers=(
                CapitalTriggerConsumer(
                    capital_consumer,
                    context_cadence_minutes=(
                        config.capital.context_forecast.cadence_minutes
                        if config.assessment.enabled
                        and config.capital.context_forecast is not None
                        and config.capital.context_forecast.enabled
                        else None
                    ),
                    context_completion_deadline_seconds=(
                        config.capital.context_forecast.completion_deadline_seconds
                        if config.assessment.enabled
                        and config.capital.context_forecast is not None
                        and config.capital.context_forecast.enabled
                        else None
                    ),
                    material_event_slots_enabled=(
                        config.capital.context_forecast.material_event_slots_enabled
                        if config.assessment.enabled
                        and config.capital.context_forecast is not None
                        and config.capital.context_forecast.enabled
                        else False
                    ),
                    material_event_slot_policy_version=(
                        config.capital.context_forecast.material_event_slot_policy_version
                        if config.assessment.enabled
                        and config.capital.context_forecast is not None
                        and config.capital.context_forecast.enabled
                        else None
                    ),
                    owner_symbol=config.assessment.review_trigger_symbol,
                    context_activation_at=capital_consumer.context_activation_at,
                )
                if config.assessment.enabled
                else capital_consumer,
            )
            if capital_consumer is not None
            else (),
        )
    )
    return TriggerServiceAssembly(
        repository=repository,
        activities=activities,
        context_forecast_activation_at=(
            capital_consumer.context_activation_at
            if capital_consumer is not None
            else None
        ),
    )


def run_trigger_service(
    *,
    config: AppConfig,
    manifest: ReleaseManifest,
    database_url: str,
    on_superseded: Callable[[tuple[str, ...]], None] | None = None,
) -> None:
    """Run trigger admission and dispatch for every enabled decision consumer."""

    engine = build_engine(database_url)
    require_current_schema(engine)
    repository = SqlTriggerRepository(engine, config.trigger)

    async def run(wakeup: PostgresOutboxListener) -> None:
        client = await Client.connect(
            config.temporal.address,
            namespace=config.temporal.namespace,
        )
        terminated = await terminate_inactive_trigger_coordinators(
            client=client,
            active_symbols=config.analysis_symbols,
            active_pipeline_id=config.pipeline.version,
        )
        if terminated and on_superseded is not None:
            on_superseded(terminated)
        assembly = assemble_trigger_service(
            config=config,
            manifest=manifest,
            engine=engine,
            repository=repository,
        )
        dispatcher = TriggerOutboxDispatcherService(
            repository=repository,
            dispatcher=TemporalTriggerDispatcher(
                client,
                config,
                repository,
                context_forecast_activation_at=assembly.context_forecast_activation_at,
            ),
            poll_seconds=config.trigger.outbox_fallback_poll_seconds,
            wakeup=wakeup,
        )
        async with TriggerTemporalWorker(client, config.temporal, assembly.activities):
            await dispatcher.run(asyncio.Event())

    leadership = PostgresTriggerLeadership(
        engine,
        config.trigger.dispatcher_advisory_lock_key,
    )
    with leadership:
        SqlGovernanceRepository(engine).record_release(manifest)
        now = datetime.now(UTC)
        ensure_trigger_plans(
            repository=repository,
            symbols=config.analysis_symbols,
            pipeline_id=config.pipeline.version,
            manifest_id=manifest.manifest_id,
            heartbeat_seconds=config.trigger.heartbeat_minutes * 60,
            minimum_intelligence_review_priority=(
                config.trigger.minimum_intelligence_review_priority
            ),
            debounce_seconds=config.trigger.debounce_seconds,
            now=now,
        )
        with PostgresOutboxListener(engine) as wakeup:
            asyncio.run(run(wakeup))


def _assemble_capital_consumer(
    *,
    config: AppConfig,
    engine,
) -> CapitalCycleService:
    execution = assemble_product_execution_runtime(config, engine)
    cash_yield_observer = None
    if config.cash_yield_evidence.enabled:
        cash_yield_observer = CashYieldEvidenceService(
            policy=config.cash_yield_evidence,
            source=BinanceSimpleEarnReadSource(
                config.cash_yield_evidence,
                BinanceReadCredentials.from_environment(config.cash_yield_evidence),
            ),
            store=SqlCashYieldObservationStore(engine),
        )
    return assemble_capital_cycle(
        config,
        engine,
        venue=execution.venue,
        initial_cash=execution.initial_cash,
        cash_yield_observer=cash_yield_observer,
    )
