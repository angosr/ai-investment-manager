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
from investment_manager.execution.venue.runtime import assemble_product_execution_runtime
from investment_manager.forecast.context.posterior import (
    assemble_quant_context_posterior_preallocator,
)
from investment_manager.forecast.context.producer import CompositeContextForecastPreflight
from investment_manager.forecast.context.repository import SqlContextAssessmentStore
from investment_manager.forecast.context.stability import (
    assemble_context_forecast_stability_preallocator,
)
from investment_manager.forecast.quant.runtime import (
    load_quant_forecast_artifact,
    quant_forecast_behavior_id,
)
from investment_manager.governance.evaluation.world_model_ablation import (
    assemble_world_model_ablation_preallocator,
)
from investment_manager.governance.models import ReleaseManifest, resolve_manifest_artifact
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
        _assemble_capital_consumer(config=config, engine=engine, manifest=manifest)
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
    manifest: ReleaseManifest,
) -> CapitalCycleService:
    execution = assemble_product_execution_runtime(config, engine)
    ablation_policy = config.outcome_evaluation.world_model_ablation
    stability_policy = config.outcome_evaluation.context_forecast_stability
    quant_policy = config.outcome_evaluation.quant_baseline
    posterior_policy = config.outcome_evaluation.quant_context_posterior
    quant_artifact_paths = (
        {
            item.artifact_id: resolve_manifest_artifact(manifest, item.artifact_id)
            for item in quant_policy.artifacts
        }
        if quant_policy is not None and quant_policy.enabled
        else None
    )

    def evaluation_preflight(contracts):
        preflights = []
        if ablation_policy is not None and ablation_policy.enabled:
            ablation = assemble_world_model_ablation_preallocator(
                config,
                engine=engine,
                release=manifest,
                contracts=contracts,
                clock=lambda: datetime.now(UTC),
            )
            if ablation is None:
                raise ValueError("WorldModel 配对 preflight 未启用")
            preflights.append(ablation)
        if stability_policy is not None and stability_policy.enabled:
            stability = assemble_context_forecast_stability_preallocator(
                config,
                engine=engine,
                clock=lambda: datetime.now(UTC),
            )
            if stability is None:
                raise ValueError("Context Forecast stability preflight 未启用")
            preflights.append(stability)
        if posterior_policy is not None and posterior_policy.enabled:
            assert quant_policy is not None and quant_policy.enabled
            assert quant_artifact_paths is not None
            policy_by_family = {item.outcome_family_id: item for item in quant_policy.artifacts}
            artifacts_by_family = {
                family: load_quant_forecast_artifact(
                    quant_artifact_paths[item.artifact_id],
                    expected_artifact_id=item.artifact_id,
                )
                for family, item in policy_by_family.items()
            }
            quant_behavior_id = quant_forecast_behavior_id(
                policy_version=quant_policy.version,
                producer_id=quant_policy.producer_id,
                targets=tuple(
                    (
                        contract,
                        artifacts_by_family.get(contract.outcome_family_id),
                    )
                    for contract in contracts
                ),
            )
            posterior = assemble_quant_context_posterior_preallocator(
                config,
                engine=engine,
                contracts=contracts,
                quant_producer_behavior_id=quant_behavior_id,
                producer_activation_at=manifest.created_at,
                clock=lambda: datetime.now(UTC),
            )
            if posterior is None:
                raise ValueError("Quant Context posterior preflight 未启用")
            preflights.append(posterior)
        return CompositeContextForecastPreflight(tuple(preflights))

    return assemble_capital_cycle(
        config,
        engine,
        venue=execution.venue,
        initial_cash=execution.initial_cash,
        code_version=manifest.code_version,
        producer_activation_at=manifest.created_at,
        quant_artifact_paths=quant_artifact_paths,
        context_forecast_preflight_factory=(
            evaluation_preflight
            if (
                (ablation_policy is not None and ablation_policy.enabled)
                or (stability_policy is not None and stability_policy.enabled)
                or (posterior_policy is not None and posterior_policy.enabled)
            )
            else None
        ),
    )
