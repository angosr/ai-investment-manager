from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime

from investment_manager.forecast.context.targets import (
    assemble_context_capital_targets,
    configured_context_capital_targets,
)
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
from investment_manager.governance.evaluation.logical_account import (
    SqlProducerPanelReader,
)
from investment_manager.governance.evaluation.producer_capital import (
    ProducerProductProjectionRecorder,
)
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
    research_product_projection_count: int = 0
    research_product_projection_unavailable: int = 0
    last_target_forecast_error_class: str | None = None
    last_product_payoff_error_class: str | None = None
    last_research_product_projection_error_class: str | None = None


@dataclass(slots=True)
class OutcomeEvaluationSupervisor:
    config: AppConfig
    target_forecast_settler: ForecastOutcomeSettler
    product_payoff_settler: ProductPayoffOutcomeSettler | None = None
    research_product_projection_recorder: ProducerProductProjectionRecorder | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    health: OutcomeEvaluationSupervisorHealth = field(
        default_factory=OutcomeEvaluationSupervisorHealth
    )
    async def run(self, stop: asyncio.Event) -> None:
        workers = [self._run_settlement_loop(stop)]
        if self.research_product_projection_recorder is not None:
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
        """Record research product projections from the active Quant producer."""

        policy = self.config.outcome_evaluation
        while not stop.is_set():
            now = require_utc(self.clock())
            recorder = self.research_product_projection_recorder
            if recorder is not None:
                try:
                    report = await asyncio.to_thread(recorder.reconcile, as_of=now)
                    self.health.research_product_projection_count += (
                        report.projection_count
                    )
                    self.health.research_product_projection_unavailable = (
                        report.unavailable_forecast_count
                    )
                    self.health.last_research_product_projection_error_class = None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if (
                        self.health.last_research_product_projection_error_class
                        != type(exc).__name__
                    ):
                        logger.exception("research product projection recording failed")
                    self.health.last_research_product_projection_error_class = type(exc).__name__

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
    quant_behavior_id = _resolve_quant_behavior_id(
        config=config,
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
    product_projection_recorder = None
    if quant_behavior_id is not None:
        market = SqlMarketDataStore(engine)
        product_store = SqlProductPayoffProjectionStore(engine)
        definitions = assemble_context_capital_targets(
            capital=config.capital,
            feature=config.feature,
            market_policy=config.market_data,
            market=market,
            product_store=product_store,
        )
        projectors = {
            item.contract.outcome_family_id: item.product_payoffs
            for item in definitions
            if item.product_payoffs is not None
        }
        if projectors:
            product_projection_recorder = ProducerProductProjectionRecorder(
                producer_behavior_ids=(quant_behavior_id,),
                panels=SqlProducerPanelReader(engine),
                product_payoffs_by_family=projectors,
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
        research_product_projection_recorder=product_projection_recorder,
    )


def _resolve_quant_behavior_id(
    *,
    config: AppConfig,
    release: ReleaseManifest | None,
) -> str | None:
    quant = config.outcome_evaluation.quant_baseline
    if quant is None or not quant.enabled:
        return None
    if release is None:
        raise ValueError("Quant 研究运行必须绑定 Release")
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
    return quant_behavior_id
