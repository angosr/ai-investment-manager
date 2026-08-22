from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

from temporalio.client import Client

from investment_manager.decision_cycle.capital import assemble_capital_cycle
from investment_manager.decision_cycle.trigger import (
    TriggerCoordinatorActivities,
    TriggerDispatchBuilder,
)
from investment_manager.forecast.carry import CarryForecastProducer
from investment_manager.forecast.context.repository import SqlContextAssessmentStore
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.governance.evaluation.capital import (
    validate_capital_shadow_evaluation_plan,
)
from investment_manager.governance.models import ReleaseManifest
from investment_manager.governance.repository import SqlGovernanceRepository
from investment_manager.market.repository import SqlMarketDataStore
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
    terminate_superseded_trigger_coordinators,
)
from investment_manager.settings import AppConfig
from investment_manager.state.decision.service import assemble_decision_packet_preparation


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
        terminated = await terminate_superseded_trigger_coordinators(
            client=client,
            plans=repository.current_plans_for_symbols(config.market_data.symbols),
            active_pipeline_id=config.pipeline.version,
        )
        if terminated and on_superseded is not None:
            on_superseded(terminated)
        capital_consumer = (
            assemble_capital_cycle(config, engine) if config.capital.enabled else None
        )
        standalone_carry = (
            CarryForecastProducer(
                policy=config.carry_forecast,
                market=SqlMarketDataStore(engine),
                store=SqlForecastStore(engine),
                maximum_spot_age_seconds=config.risk.maximum_market_age_seconds,
                maximum_perpetual_age_seconds=(
                    config.market_data.perpetual_poll_seconds * 3
                ),
                maximum_quote_skew_seconds=(
                    config.market_data.maximum_cross_market_quote_skew_seconds
                ),
            )
            if config.carry_forecast.enabled and capital_consumer is None
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
                    SqlContextAssessmentStore(engine)
                    if config.assessment.enabled
                    else None
                ),
                batch_recorder=repository,
                program_forecast_producers=(standalone_carry,)
                if standalone_carry is not None
                else (),
                program_batch_consumers=(capital_consumer,)
                if capital_consumer is not None
                else (),
            )
        )
        dispatcher = TriggerOutboxDispatcherService(
            repository=repository,
            dispatcher=TemporalTriggerDispatcher(client, config, repository),
            poll_seconds=config.trigger.outbox_fallback_poll_seconds,
            wakeup=wakeup,
        )
        async with TriggerTemporalWorker(client, config.temporal, activities):
            await dispatcher.run(asyncio.Event())

    leadership = PostgresTriggerLeadership(
        engine,
        config.trigger.dispatcher_advisory_lock_key,
    )
    with leadership:
        governance = SqlGovernanceRepository(engine)
        governance.record_release(manifest)
        if config.capital.enabled:
            validate_capital_shadow_evaluation_plan(
                config=config,
                manifest=manifest,
                plans=governance.plans_for_manifest(manifest.manifest_id),
                started_at=datetime.now(UTC),
            )
        ensure_trigger_plans(
            repository=repository,
            symbols=config.market_data.symbols,
            pipeline_id=config.pipeline.version,
            manifest_id=manifest.manifest_id,
            heartbeat_seconds=config.trigger.heartbeat_minutes * 60,
            high_impact_threshold=config.trigger.high_impact_threshold,
            debounce_seconds=config.trigger.debounce_seconds,
            now=datetime.now(UTC),
        )
        with PostgresOutboxListener(engine) as wakeup:
            asyncio.run(run(wakeup))
