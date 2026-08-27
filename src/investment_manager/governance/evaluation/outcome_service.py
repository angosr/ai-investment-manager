from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.engine import Engine

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
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.forecast.settlement import ForecastOutcomeSettler
from investment_manager.governance.evaluation.world_model_ablation import (
    SqlWorldModelAblationRepository,
    WorldModelAblationRunner,
    assemble_world_model_ablation_analyst,
    ensure_world_model_ablation_plan,
)
from investment_manager.governance.models import EvaluationPlan, ReleaseManifest
from investment_manager.governance.repository import SqlGovernanceRepository
from investment_manager.kernel.time import require_utc
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.platform.database import build_engine
from investment_manager.portfolio.stability import (
    PortfolioForecastStabilityEvaluator,
)
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
    forecast_stability_replayable_capital_cases: int = 0
    forecast_stability_capital_target_changes: int = 0
    forecast_stability_capital_cash_flips: int = 0
    last_target_forecast_error_class: str | None = None
    last_product_payoff_error_class: str | None = None
    last_world_model_ablation_error_class: str | None = None
    last_forecast_stability_error_class: str | None = None


@dataclass(slots=True)
class OutcomeEvaluationSupervisor:
    config: AppConfig
    target_forecast_settler: ForecastOutcomeSettler
    product_payoff_settler: ProductPayoffOutcomeSettler | None = None
    world_model_ablation_runner: WorldModelAblationRunner | None = None
    forecast_stability_runner: ContextForecastStabilityRunner | None = None
    portfolio_forecast_stability: PortfolioForecastStabilityEvaluator | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    health: OutcomeEvaluationSupervisorHealth = field(
        default_factory=OutcomeEvaluationSupervisorHealth
    )

    async def run(self, stop: asyncio.Event) -> None:
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
            if self.world_model_ablation_runner is not None:
                try:
                    report = await asyncio.to_thread(
                        self.world_model_ablation_runner.reconcile,
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
            if self.forecast_stability_runner is not None:
                try:
                    report = await asyncio.to_thread(
                        self.forecast_stability_runner.reconcile,
                        as_of=now,
                    )
                    self.health.forecast_stability_assignments = report.assignment_count
                    self.health.forecast_stability_complete_samples = (
                        report.complete_sample_count
                    )
                    self.health.forecast_stability_failed_replicas = (
                        report.failed_replica_count
                    )
                    if self.portfolio_forecast_stability is not None:
                        assignments = self.forecast_stability_runner.repository.assignments(
                            policy_version=self.forecast_stability_runner.policy.version,
                            formal_producer_behavior_id=(
                                self.forecast_stability_runner.formal_producer_behavior_id
                            ),
                        )
                        capital_report = self.portfolio_forecast_stability.evaluate(
                            assignments=assignments,
                            results=self.forecast_stability_runner.repository.results(
                                tuple(item.assignment_id for item in assignments)
                            ),
                        )
                        self.health.forecast_stability_replayable_capital_cases = (
                            capital_report.replayable_case_count
                        )
                        self.health.forecast_stability_capital_target_changes = (
                            capital_report.target_change_count
                        )
                        self.health.forecast_stability_capital_cash_flips = (
                            capital_report.cash_flip_count
                        )
                    self.health.last_forecast_stability_error_class = None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if self.health.last_forecast_stability_error_class != type(exc).__name__:
                        logger.exception("context forecast stability evaluation failed")
                    self.health.last_forecast_stability_error_class = type(exc).__name__
            delay = _seconds_until_next_poll(
                require_utc(self.clock()),
                poll_seconds=policy.poll_seconds,
            )
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
        portfolio_forecast_stability=(
            PortfolioForecastStabilityEvaluator(
                engine=engine,
                capital_policy=config.capital,
            )
            if stability_runner is not None and config.capital.enabled
            else None
        ),
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
