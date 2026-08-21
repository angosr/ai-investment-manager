"""Low-frequency product capital application assembled from the authoritative domains."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal

from investment_manager.decision_cycle.portfolio import (
    PortfolioDecisionPipeline,
    PortfolioPipelineOutcome,
    PortfolioPipelineResult,
    TradePlanExecutionPipeline,
    TradePlanExecutionResult,
)
from investment_manager.execution.group.accounting import (
    ProductAccountProjectionService,
    ProductAccountProjector,
)
from investment_manager.execution.group.engine import ExecutionGroupEngine
from investment_manager.execution.group.models import ExecutionGroup
from investment_manager.execution.group.repository import SqlExecutionGroupStore
from investment_manager.execution.planning.planner import TradePlan, TradePlanner
from investment_manager.execution.planning.repository import SqlTradePlanStore
from investment_manager.execution.venue.observation import SqlProductOrderObservationStore
from investment_manager.execution.venue.product_mock import SqlMockProductVenue
from investment_manager.forecast.carry import (
    CarryForecastProducer,
    ReleasedCarryForecastProducer,
)
from investment_manager.forecast.models import CalibratedForecast
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.governance.policy import DeploymentStage
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.market.models import ExecutableQuote, InstrumentProduct
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.portfolio.decision import (
    PortfolioDecisionEngine,
    PortfolioSleeveInput,
)
from investment_manager.portfolio.models import PortfolioAccountSnapshot, SleeveTarget
from investment_manager.portfolio.rebalance import (
    PortfolioRebalancePeriod,
    RebalancePeriodMode,
)
from investment_manager.portfolio.repository import (
    SqlPortfolioPerformanceStore,
    SqlPortfolioStore,
)
from investment_manager.risk.models import RiskOutcome
from investment_manager.risk.portfolio import (
    PortfolioRiskEngine,
    SleeveRiskProfile,
)
from investment_manager.risk.repository import SqlPortfolioRiskStore
from investment_manager.settings import AppConfig

logger = logging.getLogger(__name__)


class CapitalCycleService:
    """Run one idempotent point-in-time decision and immediate persistent Mock execution."""

    def __init__(
        self,
        *,
        config: AppConfig,
        market: SqlMarketDataStore,
        forecasts: SqlForecastStore,
        producer: ReleasedCarryForecastProducer,
        portfolio: SqlPortfolioStore,
        performance: SqlPortfolioPerformanceStore,
        risks: SqlPortfolioRiskStore,
        plans: SqlTradePlanStore,
        groups: SqlExecutionGroupStore,
        accounts: ProductAccountProjectionService,
        decisions: PortfolioDecisionPipeline,
        execution: TradePlanExecutionPipeline,
        estimated_variable_cost_bps: Decimal,
    ) -> None:
        self._config = config
        self._market = market
        self._forecasts = forecasts
        self._producer = producer
        self._portfolio = portfolio
        self._performance = performance
        self._risks = risks
        self._plans = plans
        self._groups = groups
        self._accounts = accounts
        self._decisions = decisions
        self._execution = execution
        self._estimated_variable_cost_bps = estimated_variable_cost_bps

    def produce(
        self,
        *,
        as_of: datetime,
    ) -> PortfolioPipelineResult | TradePlanExecutionResult:
        requested_at = require_utc(as_of)
        policy = self._config.capital
        period = self._rebalance_period(requested_at)
        if period.mode == RebalancePeriodMode.NO_CHANGE:
            logger.info(
                "capital month frozen without late rebalance",
                extra={
                    "period_id": period.period_id,
                    "as_of": requested_at.isoformat(),
                    "reason_codes": period.reason_codes,
                },
            )
            return self._observe(as_of=requested_at)

        assert period.candidate_forecast_id is not None
        loaded = self._forecasts.forecast(period.candidate_forecast_id)
        if not isinstance(loaded, CalibratedForecast):
            raise ValueError("月度 Portfolio 周期缺少绑定的 CalibratedForecast")
        forecast = loaded
        as_of = period.decision_at
        cycle_id = period.cycle_id

        completed = self._completed_decision(period, requested_at=requested_at)
        if completed is not None:
            return completed

        self._recover(as_of=requested_at, cycle_id=cycle_id)
        quotes = self._quotes(as_of=as_of)
        account = self._account(cycle_id=cycle_id, as_of=as_of, quotes=quotes)

        sleeve_id = SleeveTarget.identity_for(
            portfolio_id=policy.decision.portfolio_id,
            forecast_family=forecast.forecast_family,
            forecast_target_id=forecast.target.target_id,
        )
        sleeve = PortfolioSleeveInput(
            sleeve_id=sleeve_id,
            estimated_variable_cost_bps=self._estimated_variable_cost_bps,
            forecast=forecast,
        )
        template = policy.sleeve_risk
        risk_profile = SleeveRiskProfile(
            sleeve_id=sleeve_id,
            version=template.version,
            basis_stress_bps=template.basis_stress_bps,
            funding_stress_bps=template.funding_stress_bps,
            execution_stress_bps=template.execution_stress_bps,
            derivative_initial_margin_fraction=(template.derivative_initial_margin_fraction),
        )
        decision = self._decisions.run(
            cycle_id=cycle_id,
            as_of=as_of,
            sleeves=(sleeve,),
            account=account,
            quotes=quotes,
            risk_profiles=(risk_profile,),
            execution_specs=policy.execution_specs,
            decision_valid_until=period.entry_window_end,
        )
        plan = decision.trade_plan
        if plan is None or not plan.groups:
            logger.info(
                "capital cycle completed without executable group",
                extra={"cycle_id": cycle_id, "outcome": decision.outcome.value},
            )
            return decision
        result = self._execution.run(
            plan_id=plan.plan_id,
            as_of=max(as_of, requested_at),
            quotes=(quotes if requested_at <= as_of else self._quotes(as_of=requested_at)),
        )
        self._performance.record(result.account)
        logger.info(
            "capital cycle executed mock trade plan",
            extra={
                "cycle_id": cycle_id,
                "plan_id": plan.plan_id,
                "groups": len(result.groups),
                "equity": str(result.account.equity),
            },
        )
        return result

    def _rebalance_period(self, requested_at: datetime) -> PortfolioRebalancePeriod:
        policy = self._config.capital
        period_start = requested_at.replace(
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        period_end = (
            period_start.replace(year=period_start.year + 1, month=1)
            if period_start.month == 12
            else period_start.replace(month=period_start.month + 1)
        )
        existing = self._portfolio.rebalance_period(
            portfolio_id=policy.decision.portfolio_id,
            policy_version=policy.rebalance.version,
            period_start=period_start,
        )
        if existing is not None:
            return existing
        forecast = self._producer.produce(as_of=requested_at)
        decision_at = max(
            requested_at,
            forecast.available_at if forecast is not None else requested_at,
        )
        entry_window_end = period_start + timedelta(
            minutes=policy.rebalance.maximum_entry_delay_minutes
        )
        if forecast is not None and decision_at >= entry_window_end:
            raise ValueError("Carry Forecast 在月度 Portfolio 窗口后才可用")
        proposed = PortfolioRebalancePeriod.create(
            portfolio_id=policy.decision.portfolio_id,
            policy_version=policy.rebalance.version,
            period_start=period_start,
            period_end=period_end,
            entry_window_end=entry_window_end,
            decision_at=decision_at,
            candidate_forecast_id=(forecast.forecast_id if forecast is not None else None),
        )
        return self._portfolio.claim_rebalance_period(proposed)

    def _completed_decision(
        self,
        period: PortfolioRebalancePeriod,
        *,
        requested_at: datetime,
    ) -> PortfolioPipelineResult | TradePlanExecutionResult | None:
        target = self._portfolio.target_for_cycle(period.cycle_id)
        if target is None:
            return None
        risk = self._risks.for_target(target.target_id)
        if risk is None:
            return None
        if risk.outcome != RiskOutcome.APPROVED:
            result = PortfolioPipelineResult(
                cycle_id=period.cycle_id,
                outcome=PortfolioPipelineOutcome.RISK_REJECTED,
                target=target,
                risk_decision=risk,
            )
            if requested_at <= period.decision_at:
                return result
            return self._observe(as_of=requested_at)
        plan = self._plans.for_cycle(period.cycle_id)
        if plan is None:
            return None
        groups = self._groups.for_plan(plan.plan_id)
        if len(groups) != len(plan.groups):
            return None
        if requested_at <= period.decision_at:
            if not plan.groups:
                return PortfolioPipelineResult(
                    cycle_id=period.cycle_id,
                    outcome=PortfolioPipelineOutcome.PLANNED,
                    target=target,
                    risk_decision=risk,
                    trade_plan=plan,
                )
            result = self._execution_result(plan, groups=groups, as_of=period.decision_at)
            if result is not None:
                return result
        return self._observe(as_of=requested_at)

    def _execution_result(
        self,
        plan: TradePlan,
        *,
        groups: tuple[ExecutionGroup, ...],
        as_of: datetime,
    ) -> TradePlanExecutionResult | None:
        projection_cycle_id = stable_id(
            "execution_account",
            plan.cycle_id,
            as_of.isoformat(),
            content_hash(groups),
        )
        account = self._portfolio.account_for_cycle(
            cycle_id=projection_cycle_id,
            portfolio_id=self._config.capital.decision.portfolio_id,
        )
        if account is None:
            return None
        return TradePlanExecutionResult(
            plan_id=plan.plan_id,
            groups=groups,
            account=account,
        )

    def _recover(self, *, as_of: datetime, cycle_id: str) -> None:
        recovered = self._execution.recover_pending(as_of=as_of)
        if recovered:
            logger.info(
                "capital cycle reconciled pending execution groups",
                extra={
                    "cycle_id": cycle_id,
                    "group_count": len(recovered),
                    "nonterminal_count": sum(not item.terminal for item in recovered),
                },
            )

    def _observe(
        self,
        *,
        as_of: datetime,
    ) -> PortfolioPipelineResult | TradePlanExecutionResult:
        cycle_id = stable_id(
            "capital_observation",
            self._config.capital.version,
            self._config.capital.decision.portfolio_id,
            as_of.isoformat(),
        )
        self._recover(as_of=as_of, cycle_id=cycle_id)
        quotes = self._quotes(as_of=as_of)
        account = self._account(cycle_id=cycle_id, as_of=as_of, quotes=quotes)
        if not account.sleeves:
            return PortfolioPipelineResult(
                cycle_id=cycle_id,
                outcome=PortfolioPipelineOutcome.NO_CHANGE,
            )
        sleeves = []
        profiles = []
        template = self._config.capital.sleeve_risk
        for position in account.sleeves:
            forecast = self._forecasts.latest_calibrated_for_target(
                target_id=position.target.target_id,
                forecast_family=position.forecast_family,
                as_of=as_of,
            )
            if forecast is None:
                raise ValueError("当前 Capital Sleeve 缺少权威来源 Forecast")
            sleeves.append(
                PortfolioSleeveInput(
                    sleeve_id=position.sleeve_id,
                    estimated_variable_cost_bps=self._estimated_variable_cost_bps,
                    forecast=forecast,
                )
            )
            profiles.append(
                SleeveRiskProfile(
                    sleeve_id=position.sleeve_id,
                    version=template.version,
                    basis_stress_bps=template.basis_stress_bps,
                    funding_stress_bps=template.funding_stress_bps,
                    execution_stress_bps=template.execution_stress_bps,
                    derivative_initial_margin_fraction=(
                        template.derivative_initial_margin_fraction
                    ),
                )
            )
        protected = self._decisions.protect(
            cycle_id=cycle_id,
            as_of=as_of,
            sleeves=tuple(sleeves),
            account=account,
            quotes=quotes,
            risk_profiles=tuple(profiles),
            execution_specs=self._config.capital.execution_specs,
        )
        plan = protected.trade_plan
        if plan is None or not plan.groups:
            return protected
        result = self._execution.run(
            plan_id=plan.plan_id,
            as_of=as_of,
            quotes=quotes,
        )
        self._performance.record(result.account)
        logger.warning(
            "capital holding risk triggered programmatic exit",
            extra={
                "cycle_id": cycle_id,
                "plan_id": plan.plan_id,
                "equity": str(result.account.equity),
            },
        )
        return result

    def _account(
        self,
        *,
        cycle_id: str,
        as_of: datetime,
        quotes: tuple[ExecutableQuote, ...],
    ) -> PortfolioAccountSnapshot:
        portfolio_id = self._config.capital.decision.portfolio_id
        with self._portfolio.account_projection_lock(portfolio_id=portfolio_id):
            account = self._portfolio.account_for_cycle(
                cycle_id=cycle_id,
                portfolio_id=portfolio_id,
            )
            if account is None:
                account = self._accounts.project(
                    cycle_id=cycle_id,
                    as_of=as_of,
                    quotes=quotes,
                )
                self._portfolio.record_account(account)
        self._performance.record(account)
        return account

    def _quotes(self, *, as_of: datetime) -> tuple[ExecutableQuote, ...]:
        values: list[ExecutableQuote] = []
        for spec in self._config.capital.execution_specs:
            instrument = spec.instrument
            if instrument.product == InstrumentProduct.SPOT:
                quote = self._market.latest_spot_quote(
                    instrument=instrument,
                    evaluation_at=as_of,
                    visible_at=as_of,
                )
                if quote is None:
                    raise ValueError("Capital 缺少 Spot 可成交报价")
                values.append(
                    ExecutableQuote(
                        source_quote_id=quote.quote_id,
                        instrument=instrument,
                        as_of=as_of,
                        observed_at=quote.observed_at,
                        bid=quote.bid,
                        bid_quantity=quote.bid_quantity,
                        ask=quote.ask,
                        ask_quantity=quote.ask_quantity,
                        source=quote.source,
                    )
                )
                continue
            quote = self._market.latest_perpetual_quote(
                instrument=instrument,
                evaluation_at=as_of,
                visible_at=as_of,
            )
            if quote is None:
                raise ValueError("Capital 缺少 Perpetual 可成交报价")
            values.append(
                ExecutableQuote(
                    source_quote_id=quote.quote_id,
                    instrument=instrument,
                    as_of=as_of,
                    observed_at=quote.exchange_time,
                    bid=quote.bid,
                    bid_quantity=quote.bid_quantity,
                    ask=quote.ask,
                    ask_quantity=quote.ask_quantity,
                    source=quote.source,
                )
            )
        return tuple(sorted(values, key=lambda item: item.instrument.key))


def assemble_capital_cycle(config: AppConfig, engine) -> CapitalCycleService:
    if not config.capital.enabled or config.deployment.stage != DeploymentStage.SHADOW:
        raise ValueError("Capital cycle 只装配显式启用的 SHADOW")
    evidence = config.carry_forecast.evidence
    if evidence is None:  # guarded by AppConfig; keep assembly fail-closed.
        raise ValueError("Capital cycle 缺少 Carry evidence")
    market = SqlMarketDataStore(engine)
    forecasts = SqlForecastStore(engine)
    portfolio = SqlPortfolioStore(engine)
    performance = SqlPortfolioPerformanceStore(engine)
    risks = SqlPortfolioRiskStore(engine)
    plans = SqlTradePlanStore(engine)
    groups = SqlExecutionGroupStore(engine)
    observations = SqlProductOrderObservationStore(engine)
    venue = SqlMockProductVenue(
        engine,
        fee_bps_by_instrument={
            item.instrument.key: item.fee_bps for item in config.capital.execution_specs
        },
    )
    account_projection = ProductAccountProjectionService(
        projector=ProductAccountProjector(
            portfolio_id=config.capital.decision.portfolio_id,
            settlement_asset=config.capital.settlement_asset,
            initial_cash=config.shadow.initial_quote_balance,
        ),
        groups=groups,
        observations=observations,
        funding=market,
        risks=risks,
        accounts=portfolio,
    )
    base = CarryForecastProducer(
        policy=config.carry_forecast,
        market=market,
        store=forecasts,
        maximum_spot_age_seconds=config.capital.risk.maximum_quote_age_seconds,
        maximum_perpetual_age_seconds=config.capital.risk.maximum_quote_age_seconds,
    )
    producer = ReleasedCarryForecastProducer(
        base=base,
        evidence=evidence,
        store=forecasts,
    )
    group_engine = ExecutionGroupEngine(
        store=groups,
        venue=venue,
        observations=observations,
    )
    return CapitalCycleService(
        config=config,
        market=market,
        forecasts=forecasts,
        producer=producer,
        portfolio=portfolio,
        performance=performance,
        risks=risks,
        plans=plans,
        groups=groups,
        accounts=account_projection,
        decisions=PortfolioDecisionPipeline(
            decision=PortfolioDecisionEngine(config.capital.decision),
            risk=PortfolioRiskEngine(config.capital.risk),
            planner=TradePlanner(config.capital.planner),
            portfolio_store=portfolio,
            risk_store=risks,
            plan_store=plans,
        ),
        execution=TradePlanExecutionPipeline(
            plans=plans,
            groups=groups,
            engine=group_engine,
            accounts=account_projection,
            portfolio_store=portfolio,
        ),
        estimated_variable_cost_bps=evidence.round_trip_cost_bps,
    )
