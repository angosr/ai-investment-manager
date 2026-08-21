"""Low-frequency product capital application assembled from the authoritative domains."""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from investment_manager.decision_cycle.portfolio import (
    PortfolioDecisionPipeline,
    PortfolioPipelineResult,
    TradePlanExecutionPipeline,
    TradePlanExecutionResult,
)
from investment_manager.execution.group.accounting import (
    ProductAccountProjectionService,
    ProductAccountProjector,
)
from investment_manager.execution.group.engine import ExecutionGroupEngine
from investment_manager.execution.group.repository import SqlExecutionGroupStore
from investment_manager.execution.planning.planner import TradePlanner
from investment_manager.execution.planning.repository import SqlTradePlanStore
from investment_manager.execution.venue.observation import SqlProductOrderObservationStore
from investment_manager.execution.venue.product_mock import SqlMockProductVenue
from investment_manager.forecast.carry import (
    CarryForecastProducer,
    ReleasedCarryForecastProducer,
)
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.governance.policy import DeploymentStage
from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.market.models import ExecutableQuote, InstrumentProduct
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.portfolio.decision import (
    PortfolioDecisionEngine,
    PortfolioSleeveInput,
)
from investment_manager.portfolio.models import SleeveTarget
from investment_manager.portfolio.repository import SqlPortfolioStore
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
        forecast = self._producer.produce(as_of=requested_at)
        if forecast is None:
            forecast = self._forecasts.latest_calibrated(
                producer_id=self._config.carry_forecast.producer_id,
                forecast_family=self._config.carry_forecast.forecast_family,
                as_of=requested_at,
            )
        as_of = max(
            requested_at,
            forecast.available_at if forecast is not None else requested_at,
        )
        cycle_id = stable_id(
            "capital_cycle",
            policy.version,
            policy.decision.portfolio_id,
            as_of.isoformat(),
        )
        quotes = self._quotes(as_of=as_of)
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
        account = self._portfolio.account_for_cycle(
            cycle_id=cycle_id,
            portfolio_id=policy.decision.portfolio_id,
        )
        if account is None:
            account = self._accounts.project(
                cycle_id=cycle_id,
                as_of=as_of,
                quotes=quotes,
            )
            self._portfolio.record_account(account)

        if forecast is None:
            logger.info(
                "capital cycle selected cash: monthly entry window unavailable",
                extra={"cycle_id": cycle_id, "as_of": as_of.isoformat()},
            )
            return self._decisions.run(
                cycle_id=cycle_id,
                as_of=as_of,
                sleeves=(),
                account=account,
                quotes=(),
                risk_profiles=(),
                execution_specs=policy.execution_specs,
            )

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
            derivative_initial_margin_fraction=(
                template.derivative_initial_margin_fraction
            ),
        )
        decision = self._decisions.run(
            cycle_id=cycle_id,
            as_of=as_of,
            sleeves=(sleeve,),
            account=account,
            quotes=quotes,
            risk_profiles=(risk_profile,),
            execution_specs=policy.execution_specs,
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
            as_of=as_of,
            quotes=quotes,
        )
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

    def _quotes(self, *, as_of: datetime) -> tuple[ExecutableQuote, ...]:
        values = []
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
    risks = SqlPortfolioRiskStore(engine)
    plans = SqlTradePlanStore(engine)
    groups = SqlExecutionGroupStore(engine)
    observations = SqlProductOrderObservationStore(engine)
    venue = SqlMockProductVenue(
        engine,
        fee_bps_by_instrument={
            item.instrument.key: item.fee_bps
            for item in config.capital.execution_specs
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
