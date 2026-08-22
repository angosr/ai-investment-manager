from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

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
from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.platform.database import build_engine, require_current_schema
from investment_manager.scheduling.application import ensure_trigger_plans
from investment_manager.scheduling.models import ScheduledWakeup
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


def capital_candidate_wakeups(
    config: AppConfig,
    *,
    now: datetime,
) -> dict[str, tuple[ScheduledWakeup, ...]]:
    """Materialize future natural signal windows for the authorized Mock candidate."""

    permissions = config.capital.mock_candidate_authorizations
    if not permissions:
        return {}
    permission = permissions[0]
    policy = config.carry_forecast
    if not policy.enabled or (
        permission.producer_id,
        permission.producer_version,
        permission.forecast_family,
    ) != (policy.producer_id, policy.version, policy.forecast_family):
        raise ValueError("Capital Mock authorization 没有匹配的已启用 Forecast producer")

    now = require_utc(now)
    cursor = datetime(permission.valid_from.year, permission.valid_from.month, 1, tzinfo=UTC)
    if cursor < permission.valid_from:
        cursor = _next_month(cursor)
    wakeups: list[ScheduledWakeup] = []
    while cursor < permission.valid_until:
        expires_at = min(
            cursor + timedelta(minutes=policy.maximum_monthly_entry_delay_minutes),
            permission.valid_until,
        )
        if cursor > now and expires_at > cursor:
            wakeups.append(
                ScheduledWakeup(
                    wakeup_id=stable_id(
                        "capital_candidate_wakeup",
                        permission.producer_id,
                        permission.producer_version,
                        cursor,
                    ),
                    wake_at=cursor,
                    expires_at=expires_at,
                    reason=(
                        f"{permission.producer_id} 已授权 Mock 候选的自然月首信号窗口"
                    ),
                    hypothesis=(
                        "仅按冻结 Producer 评估费用后机会；没有合格净优势时保持现金。"
                    ),
                    required_freshness_seconds=config.risk.maximum_market_age_seconds,
                )
            )
        cursor = _next_month(cursor)
    return {policy.symbol: tuple(wakeups)} if wakeups else {}


def _next_month(value: datetime) -> datetime:
    if value.month == 12:
        return value.replace(year=value.year + 1, month=1)
    return value.replace(month=value.month + 1)


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
        if config.capital.mock_candidate_authorizations:
            validate_capital_shadow_evaluation_plan(
                config=config,
                manifest=manifest,
                plans=governance.plans_for_manifest(manifest.manifest_id),
                started_at=datetime.now(UTC),
            )
        now = datetime.now(UTC)
        ensure_trigger_plans(
            repository=repository,
            symbols=config.market_data.symbols,
            pipeline_id=config.pipeline.version,
            manifest_id=manifest.manifest_id,
            heartbeat_seconds=config.trigger.heartbeat_minutes * 60,
            high_impact_threshold=config.trigger.high_impact_threshold,
            debounce_seconds=config.trigger.debounce_seconds,
            now=now,
            scheduled_wakeups_by_symbol=capital_candidate_wakeups(config, now=now),
        )
        with PostgresOutboxListener(engine) as wakeup:
            asyncio.run(run(wakeup))
