"""Low-frequency product capital application assembled from the authoritative domains."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

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
from investment_manager.forecast.programs import CashCarryForecastProducer
from investment_manager.forecast.repository import Forecast, SqlForecastStore
from investment_manager.governance.policy import DeploymentStage
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.market.models import ExecutableQuote, InstrumentProduct
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.portfolio.decision import (
    PortfolioDecisionEngine,
    PortfolioSleeveInput,
)
from investment_manager.portfolio.models import (
    CapitalCycleOutcome,
    CapitalCycleRecord,
    MockCandidateAuthorization,
    PortfolioAccountSnapshot,
    SleeveTarget,
)
from investment_manager.portfolio.policy import SleeveRiskTemplate
from investment_manager.portfolio.repository import (
    SqlCapitalCycleStore,
    SqlPortfolioPerformanceStore,
    SqlPortfolioStore,
)
from investment_manager.risk.models import RiskOutcome
from investment_manager.risk.portfolio import (
    PortfolioRiskEngine,
    SleeveRiskProfile,
)
from investment_manager.risk.repository import SqlPortfolioRiskStore
from investment_manager.scheduling.models import TriggerBatch
from investment_manager.settings import AppConfig

logger = logging.getLogger(__name__)


class CapitalForecastProducer(Protocol):
    def produce(self, *, as_of: datetime) -> Forecast | None: ...


@dataclass(frozen=True, slots=True)
class CapitalForecastSource:
    """One qualified producer with source-specific economics and risk."""

    forecast_family: str
    producer: CapitalForecastProducer
    estimated_variable_cost_bps: Decimal
    risk_template: SleeveRiskTemplate
    mock_authorization: MockCandidateAuthorization | None = None

    def __post_init__(self) -> None:
        permission = self.mock_authorization
        if permission is not None and permission.forecast_family != self.forecast_family:
            raise ValueError("Capital Forecast source 与 Mock authorization family 不一致")


class CapitalCycleService:
    """Run one idempotent point-in-time decision and immediate persistent Mock execution."""

    def __init__(
        self,
        *,
        config: AppConfig,
        market: SqlMarketDataStore,
        forecasts: SqlForecastStore,
        forecast_sources: tuple[CapitalForecastSource, ...],
        portfolio: SqlPortfolioStore,
        performance: SqlPortfolioPerformanceStore,
        risks: SqlPortfolioRiskStore,
        plans: SqlTradePlanStore,
        groups: SqlExecutionGroupStore,
        accounts: ProductAccountProjectionService,
        decisions: PortfolioDecisionPipeline,
        execution: TradePlanExecutionPipeline,
        cycle_records: SqlCapitalCycleStore,
    ) -> None:
        families = tuple(item.forecast_family for item in forecast_sources)
        if tuple(sorted(set(families))) != tuple(sorted(families)):
            raise ValueError("Capital Forecast source family 必须唯一")
        self._config = config
        self._market = market
        self._forecasts = forecasts
        self._forecast_sources = forecast_sources
        self._source_by_family = {
            item.forecast_family: item for item in forecast_sources
        }
        self._portfolio = portfolio
        self._performance = performance
        self._risks = risks
        self._plans = plans
        self._groups = groups
        self._accounts = accounts
        self._decisions = decisions
        self._execution = execution
        self._cycle_records = cycle_records

    def consume(self, batch: TriggerBatch) -> PortfolioPipelineResult | TradePlanExecutionResult:
        """Run capital once for an immutable trigger cause."""

        return self.produce(
            as_of=batch.created_at,
            cause_id=batch.batch_id,
            trigger_batch_id=batch.batch_id,
            symbol=batch.symbol,
            trigger_types=tuple(item.trigger_type.value for item in batch.triggers),
        )

    def produce(
        self,
        *,
        as_of: datetime,
        cause_id: str | None = None,
        trigger_batch_id: str | None = None,
        symbol: str = "SYSTEM",
        trigger_types: tuple[str, ...] = (),
    ) -> PortfolioPipelineResult | TradePlanExecutionResult:
        requested_at = require_utc(as_of)
        evaluation_cause_id = cause_id or stable_id(
            "capital_manual_evaluation",
            self._config.capital.decision.portfolio_id,
            self._config.pipeline.version,
            requested_at.isoformat(),
        )
        prior_record = self._cycle_records.get(
            stable_id(
                "capital_cycle_record",
                self._config.capital.decision.portfolio_id,
                self._config.pipeline.version,
                evaluation_cause_id,
            )
        )
        if prior_record is not None:
            if (
                prior_record.triggered_at != requested_at
                or prior_record.trigger_batch_id != trigger_batch_id
                or prior_record.symbol != symbol
                or prior_record.trigger_types != tuple(sorted(set(trigger_types)))
            ):
                raise ValueError("Capital evaluation cause 已绑定不同触发事实")
            return self._recorded_result(prior_record)
        generated = tuple(
            (source, forecast)
            for source in self._forecast_sources
            if (forecast := source.producer.produce(as_of=requested_at)) is not None
        )
        if not generated:
            return self._finish(
                result=self._observe(as_of=requested_at),
                requested_at=requested_at,
                triggered_at=requested_at,
                generated_forecasts=(),
                cause_id=evaluation_cause_id,
                trigger_batch_id=trigger_batch_id,
                symbol=symbol,
                trigger_types=trigger_types,
                opportunity_already_decided=False,
            )
        generated_forecasts = tuple(item[1] for item in generated)
        decision_at = max(
            requested_at,
            *(item.available_at for item in generated_forecasts),
        )
        if any(item.valid_until <= decision_at for item in generated_forecasts):
            raise ValueError("Capital Forecast 在其入场有效期后才可用于决策")
        cycle_id = self._opportunity_cycle_id(generated_forecasts)
        completed = self._completed_decision(
            cycle_id=cycle_id,
            requested_at=decision_at,
        )
        if completed is not None:
            return self._finish(
                result=completed,
                requested_at=decision_at,
                triggered_at=requested_at,
                generated_forecasts=generated_forecasts,
                cause_id=evaluation_cause_id,
                trigger_batch_id=trigger_batch_id,
                symbol=symbol,
                trigger_types=trigger_types,
                opportunity_already_decided=True,
            )

        self._recover(as_of=decision_at, cycle_id=cycle_id)
        quotes = self._quotes(as_of=decision_at)
        account = self._account(
            cycle_id=cycle_id,
            as_of=decision_at,
            quotes=quotes,
        )
        sleeves = self._decision_sleeves(
            forecasts=generated_forecasts,
            account=account,
            as_of=decision_at,
        )
        risk_profiles = tuple(
            self._risk_profile(
                item.sleeve_id,
                self._source_by_family[item.forecast.forecast_family],
            )
            for item in sleeves
        )
        decision = self._decisions.run(
            cycle_id=cycle_id,
            as_of=decision_at,
            sleeves=sleeves,
            account=account,
            quotes=quotes,
            risk_profiles=risk_profiles,
            execution_specs=self._config.capital.execution_specs,
        )
        plan = decision.trade_plan
        if plan is None or not plan.groups:
            logger.info(
                "capital cycle completed without executable group",
                extra={"cycle_id": cycle_id, "outcome": decision.outcome.value},
            )
            return self._finish(
                result=decision,
                requested_at=decision_at,
                triggered_at=requested_at,
                generated_forecasts=generated_forecasts,
                cause_id=evaluation_cause_id,
                trigger_batch_id=trigger_batch_id,
                symbol=symbol,
                trigger_types=trigger_types,
                opportunity_already_decided=False,
            )
        result = self._execution.run(
            plan_id=plan.plan_id,
            as_of=decision_at,
            quotes=quotes,
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
        return self._finish(
            result=result,
            requested_at=decision_at,
            triggered_at=requested_at,
            generated_forecasts=generated_forecasts,
            cause_id=evaluation_cause_id,
            trigger_batch_id=trigger_batch_id,
            symbol=symbol,
            trigger_types=trigger_types,
            opportunity_already_decided=False,
        )

    def _finish(
        self,
        *,
        result: PortfolioPipelineResult | TradePlanExecutionResult,
        requested_at: datetime,
        triggered_at: datetime,
        generated_forecasts: tuple[Forecast, ...],
        cause_id: str,
        trigger_batch_id: str | None,
        symbol: str,
        trigger_types: tuple[str, ...],
        opportunity_already_decided: bool,
    ) -> PortfolioPipelineResult | TradePlanExecutionResult:
        target = (
            result.target if isinstance(result, PortfolioPipelineResult) else None
        )
        if isinstance(result, TradePlanExecutionResult):
            plan = self._plans.plan(result.plan_id)
            if plan is None:
                raise ValueError("Capital execution result 缺少权威 TradePlan")
            target = self._portfolio.target_for_cycle(plan.cycle_id)
        expected_opportunity_cycle = (
            self._opportunity_cycle_id(generated_forecasts)
            if generated_forecasts
            else None
        )
        if target is None and expected_opportunity_cycle is not None:
            target = self._portfolio.target_for_cycle(expected_opportunity_cycle)
        account = self._portfolio.latest_account(
            portfolio_id=self._config.capital.decision.portfolio_id,
            as_of=requested_at,
        )
        if account is None:
            raise ValueError("Capital cycle 缺少最终账户快照")
        generated_ids = tuple(item.forecast_id for item in generated_forecasts)
        if target is not None:
            outcome = (
                CapitalCycleOutcome.RISK_EXIT
                if expected_opportunity_cycle is None
                or target.cycle_id != expected_opportunity_cycle
                else CapitalCycleOutcome.OPPORTUNITY_ALREADY_DECIDED
                if opportunity_already_decided
                else CapitalCycleOutcome.TARGET_DECIDED
            )
            reason_codes = target.reason_codes
            decision_cycle_id = target.cycle_id
            forecast_ids = tuple(
                sorted({*generated_ids, *target.considered_forecast_ids})
            )
            target_id = target.target_id
        else:
            outcome = (
                CapitalCycleOutcome.HOLD
                if account.sleeves
                else CapitalCycleOutcome.NO_OPPORTUNITY
            )
            reason_codes = (
                ("NO_NEW_OPPORTUNITY_HOLDING_REVIEWED",)
                if account.sleeves
                else ("NO_ACTIVE_CAPITAL_OPPORTUNITY",)
            )
            decision_cycle_id = account.cycle_id
            forecast_ids = tuple(sorted(set(generated_ids)))
            target_id = None
        self._cycle_records.record(
            CapitalCycleRecord.create(
                portfolio_id=self._config.capital.decision.portfolio_id,
                pipeline_id=self._config.pipeline.version,
                cause_id=cause_id,
                trigger_batch_id=trigger_batch_id,
                symbol=symbol,
                trigger_types=trigger_types,
                triggered_at=triggered_at,
                evaluated_at=requested_at,
                decision_cycle_id=decision_cycle_id,
                account_snapshot_id=account.snapshot_id,
                forecast_ids=forecast_ids,
                target_id=target_id,
                outcome=outcome,
                reason_codes=reason_codes,
            )
        )
        return result

    def _recorded_result(
        self,
        record: CapitalCycleRecord,
    ) -> PortfolioPipelineResult | TradePlanExecutionResult:
        """Reconstruct a completed cause without re-running producers or execution."""

        if record.target_id is None:
            return PortfolioPipelineResult(
                cycle_id=record.decision_cycle_id,
                outcome=PortfolioPipelineOutcome.NO_CHANGE,
            )
        target = self._portfolio.target(record.target_id)
        if target is None or target.cycle_id != record.decision_cycle_id:
            raise ValueError("CapitalCycleRecord 缺少权威 PortfolioTarget")
        completed = self._completed_decision(
            cycle_id=target.cycle_id,
            requested_at=target.as_of,
        )
        if completed is None:
            raise ValueError("CapitalCycleRecord 引用的决策链不完整")
        return completed

    def _opportunity_cycle_id(
        self,
        forecasts: tuple[Forecast, ...],
    ) -> str:
        return stable_id(
            "capital_opportunity_cycle",
            self._config.capital.decision.portfolio_id,
            self._config.capital.decision.version,
            tuple(sorted(item.forecast_id for item in forecasts)),
        )

    def _completed_decision(
        self,
        *,
        cycle_id: str,
        requested_at: datetime,
    ) -> PortfolioPipelineResult | TradePlanExecutionResult | None:
        target = self._portfolio.target_for_cycle(cycle_id)
        if target is None:
            return None
        risk = self._risks.for_target(target.target_id)
        if risk is None:
            return None
        if risk.outcome != RiskOutcome.APPROVED:
            result = PortfolioPipelineResult(
                cycle_id=cycle_id,
                outcome=PortfolioPipelineOutcome.RISK_REJECTED,
                target=target,
                risk_decision=risk,
            )
            if requested_at <= target.as_of:
                return result
            return self._observe(as_of=requested_at)
        plan = self._plans.for_cycle(cycle_id)
        if plan is None:
            return None
        groups = self._groups.for_plan(plan.plan_id)
        if len(groups) != len(plan.groups):
            return None
        if requested_at <= target.as_of:
            if not plan.groups:
                return PortfolioPipelineResult(
                    cycle_id=cycle_id,
                    outcome=PortfolioPipelineOutcome.PLANNED,
                    target=target,
                    risk_decision=risk,
                    trade_plan=plan,
                )
            result = self._execution_result(plan, groups=groups, as_of=target.as_of)
            if result is not None:
                return result
        return self._observe(as_of=requested_at)

    def _decision_sleeves(
        self,
        *,
        forecasts: tuple[Forecast, ...],
        account: PortfolioAccountSnapshot,
        as_of: datetime,
    ) -> tuple[PortfolioSleeveInput, ...]:
        by_sleeve: dict[str, PortfolioSleeveInput] = {}
        for forecast in forecasts:
            source = self._source_by_family.get(forecast.forecast_family)
            if source is None:
                raise ValueError("Capital Forecast family 未绑定合格 source")
            sleeve_id = SleeveTarget.identity_for(
                portfolio_id=self._config.capital.decision.portfolio_id,
                forecast_family=forecast.forecast_family,
                forecast_target_id=forecast.target.target_id,
            )
            candidate = PortfolioSleeveInput(
                sleeve_id=sleeve_id,
                estimated_variable_cost_bps=source.estimated_variable_cost_bps,
                forecast=forecast,
                mock_authorization=source.mock_authorization,
            )
            existing = by_sleeve.get(sleeve_id)
            if existing is not None and existing != candidate:
                raise ValueError("同一 Capital Sleeve 收到多个不同 Forecast")
            by_sleeve[sleeve_id] = candidate
        for position in account.sleeves:
            if position.sleeve_id in by_sleeve:
                continue
            source = self._source_by_family.get(position.forecast_family)
            if source is None:
                raise ValueError("当前 Capital Sleeve 缺少合格 Forecast source")
            forecast = self._latest_forecast(
                source=source,
                target_id=position.target.target_id,
                as_of=as_of,
            )
            if forecast is None:
                raise ValueError("当前 Capital Sleeve 缺少权威来源 Forecast")
            by_sleeve[position.sleeve_id] = PortfolioSleeveInput(
                sleeve_id=position.sleeve_id,
                estimated_variable_cost_bps=source.estimated_variable_cost_bps,
                forecast=forecast,
                mock_authorization=source.mock_authorization,
                refresh_target=False,
            )
        return tuple(by_sleeve[item] for item in sorted(by_sleeve))

    @staticmethod
    def _risk_profile(
        sleeve_id: str,
        source: CapitalForecastSource,
    ) -> SleeveRiskProfile:
        template = source.risk_template
        return SleeveRiskProfile(
            sleeve_id=sleeve_id,
            version=template.version,
            basis_stress_bps=template.basis_stress_bps,
            funding_stress_bps=template.funding_stress_bps,
            execution_stress_bps=template.execution_stress_bps,
            derivative_initial_margin_fraction=(
                template.derivative_initial_margin_fraction
            ),
        )

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
        for position in account.sleeves:
            source = self._source_by_family.get(position.forecast_family)
            if source is None:
                raise ValueError("当前 Capital Sleeve 缺少合格 Forecast source")
            forecast = self._latest_forecast(
                source=source,
                target_id=position.target.target_id,
                as_of=as_of,
            )
            if forecast is None:
                raise ValueError("当前 Capital Sleeve 缺少权威来源 Forecast")
            sleeves.append(
                PortfolioSleeveInput(
                    sleeve_id=position.sleeve_id,
                    estimated_variable_cost_bps=(
                        source.estimated_variable_cost_bps
                    ),
                    forecast=forecast,
                    mock_authorization=source.mock_authorization,
                )
            )
            profiles.append(self._risk_profile(position.sleeve_id, source))
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

    def _latest_forecast(
        self,
        *,
        source: CapitalForecastSource,
        target_id: str,
        as_of: datetime,
    ) -> Forecast | None:
        if source.mock_authorization is not None:
            return self._forecasts.latest_base_for_target(
                target_id=target_id,
                forecast_family=source.forecast_family,
                as_of=as_of,
            )
        return self._forecasts.latest_calibrated_for_target(
            target_id=target_id,
            forecast_family=source.forecast_family,
            as_of=as_of,
        )

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
        instruments = tuple(
            item.instrument for item in self._config.capital.execution_specs
        )
        spot = next(
            item for item in instruments if item.product == InstrumentProduct.SPOT
        )
        perpetual = next(
            item for item in instruments if item.product != InstrumentProduct.SPOT
        )
        perpetual_quote = self._market.latest_perpetual_quote(
            instrument=perpetual,
            evaluation_at=as_of,
            visible_at=as_of,
        )
        if perpetual_quote is None:
            raise ValueError("Capital 缺少 Perpetual 可成交报价")
        spot_quote = self._market.latest_spot_quote(
            instrument=spot,
            evaluation_at=perpetual_quote.observed_at,
            visible_at=as_of,
        )
        if spot_quote is None:
            raise ValueError("Capital 缺少与 Perpetual 点时对齐的 Spot 可成交报价")
        if (
            perpetual_quote.observed_at - spot_quote.observed_at
        ).total_seconds() > self._config.capital.risk.maximum_quote_skew_seconds:
            raise ValueError("Capital Spot/Perpetual 可成交报价时间偏差过大")
        values = [
            ExecutableQuote(
                source_quote_id=spot_quote.quote_id,
                instrument=spot,
                as_of=as_of,
                observed_at=spot_quote.observed_at,
                bid=spot_quote.bid,
                bid_quantity=spot_quote.bid_quantity,
                ask=spot_quote.ask,
                ask_quantity=spot_quote.ask_quantity,
                source=spot_quote.source,
            ),
            ExecutableQuote(
                source_quote_id=perpetual_quote.quote_id,
                instrument=perpetual,
                as_of=as_of,
                observed_at=perpetual_quote.observed_at,
                bid=perpetual_quote.bid,
                bid_quantity=perpetual_quote.bid_quantity,
                ask=perpetual_quote.ask,
                ask_quantity=perpetual_quote.ask_quantity,
                source=perpetual_quote.source,
            ),
        ]
        return tuple(sorted(values, key=lambda item: item.instrument.key))


def assemble_capital_cycle(
    config: AppConfig,
    engine,
    *,
    forecast_sources: tuple[CapitalForecastSource, ...] | None = None,
) -> CapitalCycleService:
    if not config.capital.enabled or config.deployment.stage != DeploymentStage.SHADOW:
        raise ValueError("Capital cycle 只装配显式启用的 SHADOW")
    market = SqlMarketDataStore(engine)
    forecasts = SqlForecastStore(engine)
    if forecast_sources is None:
        program = config.capital.cash_carry_program
        if program is None or not program.enabled:
            forecast_sources = ()
        else:
            authorization = next(
                item
                for item in config.capital.mock_candidate_authorizations
                if (
                    item.producer_id,
                    item.producer_version,
                    item.forecast_family,
                )
                == (
                    program.producer_id,
                    program.producer_version,
                    program.forecast_family,
                )
            )
            spot = next(
                item.instrument
                for item in config.capital.execution_specs
                if item.instrument.product == InstrumentProduct.SPOT
            )
            perpetual = next(
                item.instrument
                for item in config.capital.execution_specs
                if item.instrument.product != InstrumentProduct.SPOT
            )
            forecast_sources = (
                CapitalForecastSource(
                    forecast_family=program.forecast_family,
                    producer=CashCarryForecastProducer(
                        policy=program,
                        market=market,
                        forecasts=forecasts,
                        spot=spot,
                        perpetual=perpetual,
                        minimum_entry_net_bps=authorization.minimum_entry_net_bps,
                    ),
                    estimated_variable_cost_bps=program.estimated_variable_cost_bps,
                    risk_template=config.capital.sleeve_risk,
                    mock_authorization=authorization,
                ),
            )
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
    group_engine = ExecutionGroupEngine(
        store=groups,
        venue=venue,
        observations=observations,
    )
    return CapitalCycleService(
        config=config,
        market=market,
        forecasts=forecasts,
        forecast_sources=forecast_sources,
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
        cycle_records=SqlCapitalCycleStore(engine),
    )
