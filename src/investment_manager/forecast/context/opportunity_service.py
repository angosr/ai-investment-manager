"""Long-running bridge from natural Program opportunities to Codex research review."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.engine import Engine

from investment_manager.forecast.codex.repository import (
    SqlAccountLeaseStore,
    SqlCodexAuditStore,
)
from investment_manager.forecast.context.opportunity_analyst import (
    OpportunityReviewExecutor,
    assemble_codex_opportunity_analyst,
)
from investment_manager.forecast.context.repository import (
    SqlContextAssessmentStore,
    SqlOpportunityAssessmentStore,
)
from investment_manager.forecast.context.review import OpportunityReviewInput
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.governance.models import ReleaseManifest
from investment_manager.kernel.time import require_utc
from investment_manager.portfolio.repository import SqlPortfolioStore
from investment_manager.settings import AppConfig


@dataclass(frozen=True, slots=True)
class OpportunityReviewPass:
    observed_at: datetime
    active_opportunity_count: int
    reviewed_count: int
    skipped_without_world_model: int
    skipped_without_account: int
    already_attempted_count: int


class OpportunityReviewService:
    """Review each still-actionable BaseForecast once, without blocking Program Base."""

    def __init__(
        self,
        *,
        context_config: AppConfig,
        capital_config: AppConfig,
        context_engine: Engine,
        capital_engine: Engine,
        executor: OpportunityReviewExecutor,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        program = capital_config.capital.cash_carry_program
        if program is None or not program.enabled:
            raise ValueError("机会复核服务需要启用的自然 Program producer")
        if not context_config.assessment.enabled or not context_config.codex_runtime.enabled:
            raise ValueError("机会复核服务需要启用的 WorldModel 与 Codex runtime")
        self._context_config = context_config
        self._capital_config = capital_config
        self._context = SqlContextAssessmentStore(context_engine)
        self._review_store = SqlOpportunityAssessmentStore(context_engine)
        self._forecasts = SqlForecastStore(capital_engine)
        self._portfolio = SqlPortfolioStore(capital_engine)
        self._executor = executor
        self._program = program
        self._clock = clock

    def run_once(self) -> OpportunityReviewPass:
        now = require_utc(self._clock())
        forecasts = self._forecasts.active_base_forecasts(
            producer_id=self._program.producer_id,
            producer_version=self._program.producer_version,
            forecast_family=self._program.forecast_family,
            as_of=now,
        )
        reviewed = 0
        without_world = 0
        without_account = 0
        attempted = 0
        for forecast in forecasts:
            # The counterfactual can only use cognition available when Program
            # froze the opportunity, never a later model that saw the outcome path.
            world_model = self._context.latest_before(
                analysis_scope=(
                    self._context_config.assessment.mandate.analysis_scope
                ),
                as_of=forecast.available_at,
            )
            if world_model is None:
                without_world += 1
                continue
            account = self._portfolio.latest_account(
                portfolio_id=self._capital_config.capital.decision.portfolio_id,
                as_of=forecast.available_at,
            )
            if account is None:
                without_account += 1
                continue
            review = OpportunityReviewInput.create(
                forecast=forecast,
                world_model=world_model,
                estimated_variable_cost_bps=(
                    self._program.estimated_variable_cost_bps
                ),
                baseline_net_edge_bps=(
                    forecast.raw_score
                    - self._program.estimated_variable_cost_bps
                ),
                portfolio_id=account.portfolio_id,
                account_snapshot_id=account.snapshot_id,
                account_equity=account.equity,
                created_at=forecast.available_at,
            )
            behavior_hash = self._executor.behavior_hash(review)
            if self._review_store.assessment_for(
                review_id=review.review_id,
                analysis_behavior_hash=behavior_hash,
            ) is not None or self._review_store.attempted(review.review_id):
                attempted += 1
                continue
            result = self._executor.execute(review)
            if result.success:
                reviewed += 1
            else:
                attempted += 1
        return OpportunityReviewPass(
            observed_at=now,
            active_opportunity_count=len(forecasts),
            reviewed_count=reviewed,
            skipped_without_world_model=without_world,
            skipped_without_account=without_account,
            already_attempted_count=attempted,
        )

    async def run(self, stop: asyncio.Event) -> None:
        poll_seconds = (
            self._context_config.assessment.opportunity_review_poll_seconds
        )
        while not stop.is_set():
            await asyncio.to_thread(self.run_once)
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)


def assemble_opportunity_review_service(
    *,
    context_config: AppConfig,
    capital_config: AppConfig,
    context_manifest: ReleaseManifest,
    context_engine: Engine,
    capital_engine: Engine,
) -> OpportunityReviewService:
    store = SqlOpportunityAssessmentStore(context_engine)
    analyst = assemble_codex_opportunity_analyst(
        context_config,
        bundle_root=(
            context_config.codex_runtime.bundle_root / "opportunity-reviews"
        ),
        code_version=context_manifest.code_version,
        leases=SqlAccountLeaseStore(context_engine),
        audit=SqlCodexAuditStore(context_engine),
    )
    return OpportunityReviewService(
        context_config=context_config,
        capital_config=capital_config,
        context_engine=context_engine,
        capital_engine=capital_engine,
        executor=OpportunityReviewExecutor(store, analyst),
    )
