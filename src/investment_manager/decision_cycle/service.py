from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from temporalio.client import Client

from investment_manager.decision_cycle.capital import (
    CapitalCycleService,
    CapitalForecastSource,
    CapitalTriggerConsumer,
    assemble_capital_cycle,
)
from investment_manager.decision_cycle.trigger import (
    TriggerCoordinatorActivities,
    TriggerDispatchBuilder,
)
from investment_manager.execution.venue.runtime import assemble_product_execution_runtime
from investment_manager.forecast.context.analyst import configured_assess_behavior_hash
from investment_manager.forecast.context.posterior_preparation import (
    ContextPosteriorPreparation,
)
from investment_manager.forecast.context.repository import SqlContextAssessmentStore
from investment_manager.forecast.contract_repository import SqlForecastContractStore
from investment_manager.forecast.product.projector import (
    RecordedProductPayoffProjector,
    build_point_in_time_product_payoff_projector,
)
from investment_manager.forecast.product.repository import (
    SqlProductPayoffProjectionStore,
)
from investment_manager.forecast.program.admission import (
    ActiveForecastAdmissions,
    resolve_active_forecast_admissions,
)
from investment_manager.forecast.program.baseline import load_forecast_baseline
from investment_manager.forecast.program.prior import (
    PriorRuntimeTarget,
    RollingPriorForecastProducer,
    build_prior_targets,
)
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.governance.models import (
    ReleaseManifest,
    resolve_manifest_artifact,
)
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
    terminate_inactive_trigger_coordinators,
)
from investment_manager.settings import AppConfig
from investment_manager.state.decision.service import assemble_decision_packet_preparation


@dataclass(frozen=True, slots=True)
class TriggerServiceAssembly:
    """Trigger graph constructed before leadership or durable activation."""

    repository: SqlTriggerRepository
    activities: TriggerCoordinatorActivities


@dataclass(frozen=True, slots=True)
class ForecastRuntimeAssembly:
    prior: RollingPriorForecastProducer
    posterior: ContextPosteriorPreparation | None
    capital_sources: tuple[CapitalForecastSource, ...]


def assemble_trigger_service(
    *,
    config: AppConfig,
    manifest: ReleaseManifest,
    engine,
    repository: SqlTriggerRepository | None = None,
) -> TriggerServiceAssembly:
    """Assemble the production graph without acquiring leadership or creating plans."""

    repository = repository or SqlTriggerRepository(engine, config.trigger)
    forecast_runtime = (
        _assemble_forecast_runtime(config=config, manifest=manifest, engine=engine)
        if config.outcome_evaluation.forecast_prior.enabled
        else None
    )
    if forecast_runtime is None and config.capital.candidate_capital_authorizations:
        raise ValueError("Capital authorization 缺少现役 Forecast runtime")
    capital_consumer = (
        _assemble_capital_consumer(
            config=config,
            engine=engine,
            forecast_sources=(() if forecast_runtime is None else forecast_runtime.capital_sources),
        )
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
            program_forecast_producers=(
                () if forecast_runtime is None else (forecast_runtime.prior,)
            ),
            posterior_preparation=(
                None if forecast_runtime is None else forecast_runtime.posterior
            ),
            program_batch_consumers=(
                CapitalTriggerConsumer(
                    capital_consumer,
                    owner_symbol=config.assessment.review_trigger_symbol,
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
            dispatcher=TemporalTriggerDispatcher(client, config, repository),
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
    forecast_sources: tuple[CapitalForecastSource, ...],
) -> CapitalCycleService:
    execution = assemble_product_execution_runtime(config, engine)
    return assemble_capital_cycle(
        config,
        engine,
        venue=execution.venue,
        initial_cash=execution.initial_cash,
        forecast_sources=forecast_sources,
    )


def _assemble_forecast_runtime(
    *,
    config: AppConfig,
    manifest: ReleaseManifest,
    engine,
) -> ForecastRuntimeAssembly:
    policy = config.outcome_evaluation.forecast_prior
    if policy.artifact_id is None:
        raise ValueError("启用 Forecast prior 时缺少 Release 制品 ID")
    artifact = load_forecast_baseline(resolve_manifest_artifact(manifest, policy.artifact_id))
    world_model_behavior_id = configured_assess_behavior_hash(config)
    authorization_identities = tuple(
        sorted(
            (
                item.producer_id,
                item.producer_behavior_id,
                item.outcome_family_id,
            )
            for item in config.capital.candidate_capital_authorizations
        )
    )
    admissions = resolve_active_forecast_admissions(
        artifact=artifact,
        runtime=config.codex_runtime,
        world_model_behavior_id=world_model_behavior_id,
        authorization_identities=authorization_identities,
    )
    targets = tuple(
        sorted(
            build_prior_targets(
                artifact,
                capital_outcome_families=admissions.prior_outcome_families,
            ),
            key=lambda item: item.contract.contract_id,
        )
    )
    prior = RollingPriorForecastProducer(
        artifact=artifact,
        market=SqlMarketDataStore(engine),
        contracts=SqlForecastContractStore(engine),
        forecasts=SqlForecastStore(engine),
        outcome_evaluation_version=config.outcome_evaluation.target_forecast_version,
        activated_at=manifest.created_at,
        maximum_quote_age_seconds=config.capital.risk.maximum_quote_age_seconds,
        capital_outcome_families=admissions.prior_outcome_families,
    )
    contracts = tuple(item.contract for item in targets)
    prior_bindings = tuple(item.binding for item in targets)
    posterior = (
        ContextPosteriorPreparation(
            contracts=contracts,
            prior_bindings=prior_bindings,
            runtime=config.codex_runtime,
            world_model_behavior_id=world_model_behavior_id,
            activated_at=manifest.created_at,
            contract_store=SqlForecastContractStore(engine),
            forecast_store=SqlForecastStore(engine),
            capital_outcome_families=admissions.posterior_outcome_families,
        )
        if config.assessment.enabled
        else None
    )
    if admissions.posterior_outcome_families and posterior is None:
        raise ValueError("Posterior capital authorization 缺少启用的 Assessment runtime")
    sources = (
        _assemble_capital_sources(
            config=config,
            engine=engine,
            targets=targets,
            posterior=posterior,
            admissions=admissions,
        )
        if config.capital.enabled
        else ()
    )
    return ForecastRuntimeAssembly(prior=prior, posterior=posterior, capital_sources=sources)


def _assemble_capital_sources(
    *,
    config: AppConfig,
    engine,
    targets: tuple[PriorRuntimeTarget, ...],
    posterior: ContextPosteriorPreparation | None,
    admissions: ActiveForecastAdmissions,
) -> tuple[CapitalForecastSource, ...]:
    authorizations = {
        (item.producer_id, item.producer_behavior_id, item.outcome_family_id): item
        for item in config.capital.candidate_capital_authorizations
    }
    market = SqlMarketDataStore(engine)
    projection_store = SqlProductPayoffProjectionStore(engine)
    sources = []
    for target in targets:
        contract = target.contract
        if admissions.prior_outcome_families:
            binding = target.binding
        elif posterior is not None:
            binding = posterior.binding(contract)
        else:
            binding = target.binding
        authorization = authorizations.get(
            (
                binding.producer_id,
                binding.producer_behavior_id,
                contract.outcome_family_id,
            )
        )
        builder = build_point_in_time_product_payoff_projector(
            capital_policy=config.capital,
            contract=contract,
            market=market,
            funding_lookback_hours=config.market_data.funding_history_lookback_hours,
        )
        sources.append(
            CapitalForecastSource(
                contract=contract,
                binding=binding,
                risk_template=config.capital.sleeve_risk,
                capital_authorization=authorization,
                product_payoffs=RecordedProductPayoffProjector(
                    builder=builder,
                    store=projection_store,
                ),
            )
        )
    if authorizations and sum(item.capital_authorization is not None for item in sources) != len(
        authorizations
    ):
        raise ValueError("Capital authorization 未精确装配到现役 Forecast source")
    return tuple(sorted(sources, key=lambda item: item.contract.outcome_family_id))
