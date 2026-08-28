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
from investment_manager.forecast.context.stability import (
    ContextForecastStabilityRunner,
    assemble_context_forecast_stability_runner,
)
from investment_manager.forecast.context.targets import configured_context_capital_targets
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
from investment_manager.governance.models import (
    ReleaseManifest,
    resolve_manifest_artifact,
)
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
    forecast_stability_assignments: int = 0
    forecast_stability_complete_samples: int = 0
    forecast_stability_failed_replicas: int = 0
    quant_posterior_assignments: int = 0
    quant_posterior_forecasts: int = 0
    quant_posterior_no_estimates: int = 0
    quant_posterior_pending: int = 0
    last_target_forecast_error_class: str | None = None
    last_product_payoff_error_class: str | None = None
    last_forecast_stability_error_class: str | None = None
    last_quant_posterior_error_class: str | None = None


@dataclass(slots=True)
class OutcomeEvaluationSupervisor:
    config: AppConfig
    target_forecast_settler: ForecastOutcomeSettler
    product_payoff_settler: ProductPayoffOutcomeSettler | None = None
    forecast_stability_runners: tuple[ContextForecastStabilityRunner, ...] = ()
    quant_posterior_runner: QuantContextPosteriorRunner | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    health: OutcomeEvaluationSupervisorHealth = field(
        default_factory=OutcomeEvaluationSupervisorHealth
    )
    async def run(self, stop: asyncio.Event) -> None:
        workers = [self._run_settlement_loop(stop)]
        if self.quant_posterior_runner is not None or self.forecast_stability_runners:
            workers.append(self._run_research_loop(stop))
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

    async def _run_research_loop(self, stop: asyncio.Event) -> None:
        """Run the capital-relevant posterior before diagnostic replicas."""

        policy = self.config.outcome_evaluation
        while not stop.is_set():
            now = require_utc(self.clock())
            runner = self.quant_posterior_runner
            if runner is not None:
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

            outcomes = []
            stability_as_of = require_utc(self.clock())
            for stability_runner in self.forecast_stability_runners:
                try:
                    outcomes.append(
                        await asyncio.to_thread(
                            stability_runner.reconcile,
                            as_of=stability_as_of,
                        )
                    )
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    outcomes.append(exc)
            reports = tuple(item for item in outcomes if not isinstance(item, BaseException))
            errors = tuple(item for item in outcomes if isinstance(item, BaseException))
            self.health.forecast_stability_assignments = sum(
                item.assignment_count for item in reports
            )
            self.health.forecast_stability_complete_samples = sum(
                item.complete_sample_count for item in reports
            )
            self.health.forecast_stability_failed_replicas = sum(
                item.failed_replica_count for item in reports
            )
            if not errors:
                self.health.last_forecast_stability_error_class = None
            else:
                exc = errors[0]
                if self.health.last_forecast_stability_error_class != type(exc).__name__:
                    logger.error(
                        "forecast stability evaluation failed",
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )
                self.health.last_forecast_stability_error_class = type(exc).__name__
            await _wait_for_next_poll(
                stop,
                now=require_utc(self.clock()),
                poll_seconds=policy.research_poll_seconds,
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
    posterior_runner = _assemble_quant_context_posterior(
        config=config,
        engine=engine,
        release=release,
    )
    stability_behaviors = (
        ()
        if posterior_runner is None
        else (posterior_runner.producer_behavior_id,)
    )
    stability_runners = tuple(
        runner
        for behavior_id in stability_behaviors
        if behavior_id is not None
        and (
            runner := assemble_context_forecast_stability_runner(
                config,
                engine=engine,
                producer_behavior_id=behavior_id,
            )
        )
        is not None
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
        forecast_stability_runners=stability_runners,
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
    targets = configured_context_capital_targets(
        capital=config.capital,
        feature=config.feature,
        market_policy=config.market_data,
    )
    contracts = tuple(item.contract for item in targets)
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
        target_state_behaviors=tuple(
            (item.contract.outcome_family_id, item.state_behavior)
            for item in targets
        ),
        quant_producer_behavior_id=quant_behavior_id,
    )
