from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.engine import Engine

from investment_manager.forecast.context.posterior import (
    QuantContextPosteriorRunner,
    assemble_quant_context_posterior_runner,
)
from investment_manager.forecast.context.producer import context_forecast_contract
from investment_manager.forecast.context.stability import (
    ContextForecastStabilityRunner,
    assemble_context_forecast_stability_runner,
)
from investment_manager.forecast.contracts import ForecastContract
from investment_manager.forecast.product.repository import (
    SqlProductPayoffProjectionStore,
)
from investment_manager.forecast.product.settlement import ProductPayoffOutcomeSettler
from investment_manager.forecast.quant.runtime import (
    load_quant_forecast_artifact,
    quant_forecast_behavior_id,
)
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.forecast.settlement import ForecastOutcomeSettler
from investment_manager.governance.evaluation.world_model_ablation import (
    SqlWorldModelAblationRepository,
    WorldModelAblationRunner,
    assemble_world_model_ablation_analyst,
    ensure_world_model_ablation_plan,
)
from investment_manager.governance.models import (
    EvaluationPlan,
    ReleaseManifest,
    resolve_manifest_artifact,
)
from investment_manager.governance.repository import SqlGovernanceRepository
from investment_manager.kernel.time import require_utc
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.platform.database import build_engine
from investment_manager.settings import AppConfig

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class OutcomeEvaluationSupervisorHealth:
    target_forecast_settled: int = 0
    target_forecast_outcome_unavailable: int = 0
    target_forecast_pending: int = 0
    product_payoff_settled: int = 0
    product_payoff_outcome_unavailable: int = 0
    product_payoff_pending: int = 0
    world_model_ablation_assignments: int = 0
    world_model_ablation_settled_pairs: int = 0
    world_model_ablation_failed_controls: int = 0
    forecast_stability_assignments: int = 0
    forecast_stability_complete_samples: int = 0
    forecast_stability_failed_replicas: int = 0
    quant_posterior_assignments: int = 0
    quant_posterior_forecasts: int = 0
    quant_posterior_no_estimates: int = 0
    quant_posterior_pending: int = 0
    last_target_forecast_error_class: str | None = None
    last_product_payoff_error_class: str | None = None
    last_world_model_ablation_error_class: str | None = None
    last_forecast_stability_error_class: str | None = None
    last_quant_posterior_error_class: str | None = None


@dataclass(slots=True)
class OutcomeEvaluationSupervisor:
    config: AppConfig
    target_forecast_settler: ForecastOutcomeSettler
    product_payoff_settler: ProductPayoffOutcomeSettler | None = None
    world_model_ablation_runner: WorldModelAblationRunner | None = None
    forecast_stability_runner: ContextForecastStabilityRunner | None = None
    quant_posterior_runner: QuantContextPosteriorRunner | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    health: OutcomeEvaluationSupervisorHealth = field(
        default_factory=OutcomeEvaluationSupervisorHealth
    )

    async def run(self, stop: asyncio.Event) -> None:
        workers = [self._run_settlement_loop(stop)]
        if self.world_model_ablation_runner is not None:
            workers.append(self._run_world_model_ablation_loop(stop))
        if self.forecast_stability_runner is not None:
            workers.append(self._run_forecast_stability_loop(stop))
        if self.quant_posterior_runner is not None:
            workers.append(self._run_quant_posterior_loop(stop))
        await asyncio.gather(*workers)

    async def _run_settlement_loop(self, stop: asyncio.Event) -> None:
        policy = self.config.outcome_evaluation
        while not stop.is_set():
            now = require_utc(self.clock())
            try:
                target_result = await asyncio.to_thread(
                    self.target_forecast_settler.settle,
                    as_of=now,
                )
                self.health.target_forecast_settled += target_result.settled
                self.health.target_forecast_outcome_unavailable += (
                    target_result.outcome_unavailable
                )
                self.health.target_forecast_pending += target_result.pending
                self.health.last_target_forecast_error_class = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.health.last_target_forecast_error_class != type(exc).__name__:
                    logger.exception("target forecast settlement failed")
                self.health.last_target_forecast_error_class = type(exc).__name__
            if self.product_payoff_settler is not None:
                try:
                    product_result = await asyncio.to_thread(
                        self.product_payoff_settler.settle,
                        as_of=now,
                    )
                    self.health.product_payoff_settled += product_result.settled
                    self.health.product_payoff_outcome_unavailable += (
                        product_result.outcome_unavailable
                    )
                    self.health.product_payoff_pending += product_result.pending
                    self.health.last_product_payoff_error_class = None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if self.health.last_product_payoff_error_class != type(exc).__name__:
                        logger.exception("product payoff settlement failed")
                    self.health.last_product_payoff_error_class = type(exc).__name__
            await _wait_for_next_poll(
                stop,
                now=require_utc(self.clock()),
                poll_seconds=policy.poll_seconds,
            )

    async def _run_world_model_ablation_loop(self, stop: asyncio.Event) -> None:
        policy = self.config.outcome_evaluation
        runner = self.world_model_ablation_runner
        assert runner is not None
        while not stop.is_set():
            now = require_utc(self.clock())
            try:
                report = await asyncio.to_thread(
                    runner.reconcile,
                    as_of=now,
                )
                self.health.world_model_ablation_assignments = report.assignments
                self.health.world_model_ablation_settled_pairs = report.settled_pairs
                self.health.world_model_ablation_failed_controls = report.failed_controls
                self.health.last_world_model_ablation_error_class = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.health.last_world_model_ablation_error_class != type(exc).__name__:
                    logger.exception("world model ablation evaluation failed")
                self.health.last_world_model_ablation_error_class = type(exc).__name__
            await _wait_for_next_poll(
                stop,
                now=require_utc(self.clock()),
                poll_seconds=policy.poll_seconds,
            )

    async def _run_forecast_stability_loop(self, stop: asyncio.Event) -> None:
        policy = self.config.outcome_evaluation
        runner = self.forecast_stability_runner
        assert runner is not None
        while not stop.is_set():
            now = require_utc(self.clock())
            try:
                report = await asyncio.to_thread(
                    runner.reconcile,
                    as_of=now,
                )
                self.health.forecast_stability_assignments = report.assignment_count
                self.health.forecast_stability_complete_samples = (
                    report.complete_sample_count
                )
                self.health.forecast_stability_failed_replicas = (
                    report.failed_replica_count
                )
                self.health.last_forecast_stability_error_class = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.health.last_forecast_stability_error_class != type(exc).__name__:
                    logger.exception("context forecast stability evaluation failed")
                self.health.last_forecast_stability_error_class = type(exc).__name__
            await _wait_for_next_poll(
                stop,
                now=require_utc(self.clock()),
                poll_seconds=policy.poll_seconds,
            )

    async def _run_quant_posterior_loop(self, stop: asyncio.Event) -> None:
        runner = self.quant_posterior_runner
        assert runner is not None
        while not stop.is_set():
            now = require_utc(self.clock())
            try:
                report = await asyncio.to_thread(runner.reconcile, as_of=now)
                self.health.quant_posterior_assignments = report.assignment_count
                self.health.quant_posterior_forecasts = report.forecast_count
                self.health.quant_posterior_no_estimates = report.no_estimate_count
                self.health.quant_posterior_pending = report.pending_count
                self.health.last_quant_posterior_error_class = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.health.last_quant_posterior_error_class != type(exc).__name__:
                    logger.exception("quant context posterior evaluation failed")
                self.health.last_quant_posterior_error_class = type(exc).__name__
            await _wait_for_next_poll(
                stop,
                now=require_utc(self.clock()),
                poll_seconds=runner.policy.assignment_poll_seconds,
            )


async def _wait_for_next_poll(
    stop: asyncio.Event,
    *,
    now: datetime,
    poll_seconds: int,
) -> None:
    delay = _seconds_until_next_poll(now, poll_seconds=poll_seconds)
    with suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=delay)


def _seconds_until_next_poll(now: datetime, *, poll_seconds: int) -> float:
    """按绝对 UTC 时间桶调度，避免候选结算耗时累积为评价漂移。"""

    elapsed = now.timestamp()
    next_boundary = (int(elapsed) // poll_seconds + 1) * poll_seconds
    return max(0.001, next_boundary - elapsed)


def assemble_outcome_evaluation(
    config: AppConfig,
    database_url: str,
    *,
    release: ReleaseManifest | None = None,
) -> OutcomeEvaluationSupervisor:
    engine = build_engine(database_url)
    ablation_runner = _assemble_world_model_ablation(
        config=config,
        engine=engine,
        release=release,
    )
    stability_runner = assemble_context_forecast_stability_runner(
        config,
        engine=engine,
    )
    posterior_runner = _assemble_quant_context_posterior(
        config=config,
        engine=engine,
        release=release,
    )
    # Outcome owns already-recorded obligations across Release changes.  Whether the
    # current Capital policy can create new projections must not orphan old ones.
    product_payoff_settler = ProductPayoffOutcomeSettler(
        market=SqlMarketDataStore(engine),
        store=SqlProductPayoffProjectionStore(engine),
        evaluation_version=config.outcome_evaluation.product_payoff_version,
        maximum_spot_age_seconds=config.capital.risk.maximum_quote_age_seconds,
        maximum_perpetual_age_seconds=(config.market_data.perpetual_poll_seconds * 3),
        maximum_funding_gap_hours=(config.outcome_evaluation.maximum_funding_gap_hours),
        settlement_grace_minutes=(config.outcome_evaluation.settlement_grace_minutes),
    )
    return OutcomeEvaluationSupervisor(
        config=config,
        target_forecast_settler=ForecastOutcomeSettler(
            market=SqlMarketDataStore(engine),
            store=SqlForecastStore(engine),
            evaluation_version=(config.outcome_evaluation.target_forecast_version),
            maximum_spot_age_seconds=config.capital.risk.maximum_quote_age_seconds,
            maximum_perpetual_age_seconds=(config.market_data.perpetual_poll_seconds * 3),
            maximum_funding_gap_hours=(config.outcome_evaluation.maximum_funding_gap_hours),
            settlement_grace_minutes=(config.outcome_evaluation.settlement_grace_minutes),
        ),
        product_payoff_settler=product_payoff_settler,
        world_model_ablation_runner=ablation_runner,
        forecast_stability_runner=stability_runner,
        quant_posterior_runner=posterior_runner,
    )


def _assemble_quant_context_posterior(
    *,
    config: AppConfig,
    engine: Engine,
    release: ReleaseManifest | None,
) -> QuantContextPosteriorRunner | None:
    posterior = config.outcome_evaluation.quant_context_posterior
    quant = config.outcome_evaluation.quant_baseline
    if posterior is None or not posterior.enabled:
        return None
    if release is None or quant is None or not quant.enabled:
        raise ValueError("Quant Context posterior 运行必须绑定 Release 与 Quant baseline")
    contracts = configured_world_model_ablation_contracts(config)
    policy_by_family = {item.outcome_family_id: item for item in quant.artifacts}
    artifacts = {
        family: load_quant_forecast_artifact(
            resolve_manifest_artifact(release, item.artifact_id),
            expected_artifact_id=item.artifact_id,
        )
        for family, item in policy_by_family.items()
    }
    quant_behavior_id = quant_forecast_behavior_id(
        policy_version=quant.version,
        producer_id=quant.producer_id,
        targets=tuple(
            (contract, artifacts.get(contract.outcome_family_id)) for contract in contracts
        ),
    )
    return assemble_quant_context_posterior_runner(
        config,
        engine=engine,
        contracts=contracts,
        quant_producer_behavior_id=quant_behavior_id,
    )


def _assemble_world_model_ablation(
    *,
    config: AppConfig,
    engine,
    release: ReleaseManifest | None,
) -> WorldModelAblationRunner | None:
    policy = config.outcome_evaluation.world_model_ablation
    if policy is None or not policy.enabled:
        return None
    if release is None:
        raise ValueError("启用 WorldModel control 必须绑定 ReleaseManifest")
    contracts = configured_world_model_ablation_contracts(config)
    context = config.capital.context_forecast
    assert context is not None
    plan = ensure_world_model_ablation_plan(
        governance=SqlGovernanceRepository(engine),
        config=config,
        contracts=contracts,
        release=release,
        registered_at=datetime.now(UTC),
    )
    return WorldModelAblationRunner(
        policy=policy,
        plan=plan,
        formal_producer_behavior_id=context.producer_behavior_id,
        evaluation_version=config.outcome_evaluation.target_forecast_version,
        repository=SqlWorldModelAblationRepository(engine),
        analyst=assemble_world_model_ablation_analyst(config, engine=engine),
    )


def configured_world_model_ablation_contracts(
    config: AppConfig,
) -> tuple[ForecastContract, ...]:
    """Build the exact joint contract set seen by the formal forecast call."""

    context = config.capital.context_forecast
    if context is None or not context.enabled:
        raise ValueError("启用 WorldModel control 必须绑定 Context Forecast")
    instruments = {
        item.instrument.key: item.instrument for item in config.capital.execution_specs
    }
    instruments.update(
        {
            item.key: item
            for item in config.capital.forecast_reference_instruments
        }
    )
    try:
        return tuple(
            context_forecast_contract(
                policy=context,
                target_policy=target_policy,
                instrument=instruments[target_policy.reference_instrument_key],
                cost_semantics_version=config.capital.decision.cost_model_version,
            )
            for target_policy in context.targets
        )
    except KeyError as exc:
        raise ValueError(
            "WorldModel control 参考合同品种不在 Forecast 参考范围"
        ) from exc


def preregister_world_model_ablation_plan(
    *,
    config: AppConfig,
    engine: Engine,
    release: ReleaseManifest,
    registered_at: datetime,
) -> EvaluationPlan:
    """Persist the candidate plan before release preflight or either model call."""

    return ensure_world_model_ablation_plan(
        governance=SqlGovernanceRepository(engine),
        config=config,
        contracts=configured_world_model_ablation_contracts(config),
        release=release,
        registered_at=registered_at,
    )
