from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

from investment_manager.governance.models import ReleaseManifest
from investment_manager.governance.repository import SqlGovernanceRepository
from investment_manager.information.repository import SqlEventStore
from investment_manager.legacy.runtime import TemporalAnalysisCoordinator
from investment_manager.legacy.shadow import SqlShadowStateReader
from investment_manager.legacy.trigger_adapter import (
    TriggerAnalysisRequestBuilder,
    TriggerCoordinatorActivities,
)
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.platform.database import build_engine, require_current_schema
from investment_manager.risk.protection import SqlPortfolioProtectionStore
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


def run_trigger_service(
    *,
    config: AppConfig,
    manifest: ReleaseManifest,
    database_url: str,
    on_superseded: Callable[[tuple[str, ...]], None] | None = None,
) -> None:
    """Run trigger coordination with the retiring AnalysisCycle adapter."""

    engine = build_engine(database_url)
    require_current_schema(engine)
    repository = SqlTriggerRepository(engine, config.trigger)

    async def run(wakeup: PostgresOutboxListener) -> None:
        temporal = await TemporalAnalysisCoordinator.connect(config.temporal)
        terminated = await terminate_superseded_trigger_coordinators(
            client=temporal.client,
            plans=repository.current_plans_for_symbols(config.market_data.symbols),
            active_pipeline_id=config.pipeline.version,
        )
        if terminated and on_superseded is not None:
            on_superseded(terminated)
        activities = TriggerCoordinatorActivities(
            TriggerAnalysisRequestBuilder(
                config=config,
                market_store=SqlMarketDataStore(engine),
                event_store=SqlEventStore(
                    engine,
                    pipeline_id=config.pipeline.version,
                    trigger_expiry_seconds=config.trigger.trigger_expiry_seconds,
                    max_visible_events=config.information.read_limit,
                ),
                state=SqlShadowStateReader(
                    engine,
                    maximum_reconciliation_age_seconds=(
                        config.reconciliation.maximum_report_age_seconds
                    ),
                ),
                protection=SqlPortfolioProtectionStore(
                    engine,
                    policy=config.risk,
                    initial_equity=config.shadow.initial_quote_balance,
                ),
                batch_recorder=repository,
            )
        )
        dispatcher = TriggerOutboxDispatcherService(
            repository=repository,
            dispatcher=TemporalTriggerDispatcher(temporal.client, config, repository),
            poll_seconds=config.trigger.outbox_fallback_poll_seconds,
            wakeup=wakeup,
        )
        async with TriggerTemporalWorker(temporal.client, config.temporal, activities):
            await dispatcher.run(asyncio.Event())

    leadership = PostgresTriggerLeadership(
        engine,
        config.trigger.dispatcher_advisory_lock_key,
    )
    with leadership:
        SqlGovernanceRepository(engine).record_release(manifest)
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
