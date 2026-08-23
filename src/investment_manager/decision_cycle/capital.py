"""Low-frequency product capital application assembled from the authoritative domains."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
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
from investment_manager.forecast.codex.repository import (
    SqlAccountLeaseStore,
    SqlCodexAuditStore,
)
from investment_manager.forecast.context.estimate import (
    assemble_codex_context_forecast_analyst,
)
from investment_manager.forecast.context.producer import (
    ContextForecastProducer,
    MarketContextTargetStateProvider,
    context_spot_forecast_contract,
)
from investment_manager.forecast.context.repository import SqlContextAssessmentStore
from investment_manager.forecast.contract_repository import SqlForecastContractStore
from investment_manager.forecast.contracts import (
    ForecastContract,
    ForecastNoEstimate,
    ForecastOrientation,
    ForecastPermission,
    ForecastProducerBinding,
    ForecastProducerKind,
)
from investment_manager.forecast.programs import (
    CashCarryForecastProducer,
    ForecastProductionResult,
    cash_carry_target,
)
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.forecast.results import Forecast
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
from investment_manager.scheduling.models import AnalysisTriggerType, TriggerBatch
from investment_manager.settings import AppConfig

logger = logging.getLogger(__name__)


class CapitalForecastProducer(Protocol):
    def produce(self, *, as_of: datetime) -> ForecastProductionResult: ...


@dataclass(frozen=True, slots=True)
class CapitalForecastSource:
    """One contract-bound producer and its risk/permission envelope."""

    contract: ForecastContract
    binding: ForecastProducerBinding
    producer: CapitalForecastProducer
    risk_template: SleeveRiskTemplate
    mock_authorization: MockCandidateAuthorization | None = None

    def __post_init__(self) -> None:
        if self.binding.contract_id != self.contract.contract_id:
            raise ValueError("Capital Forecast source 的 Contract/Binding 不一致")
        permission = self.mock_authorization
        if permission is not None and (
            permission.producer_id != self.binding.producer_id
            or permission.producer_behavior_id != self.binding.producer_behavior_id
            or permission.outcome_family_id != self.contract.outcome_family_id
            or self.binding.permission != ForecastPermission.MOCK
        ):
            raise ValueError("Capital Forecast source 与 Mock authorization 不一致")
        if permission is None and self.binding.permission != ForecastPermission.CAPITAL:
            raise ValueError("无 Mock authorization 的 Capital source 必须具有 CAPITAL 权限")


@dataclass(frozen=True, slots=True)
class CapitalTriggerConsumer:
    """Own capital on one coordinator and create idempotent Context slots."""

    capital: CapitalCycleService
    context_cadence_minutes: int | None = None
    owner_symbol: str | None = None

    def consume(
        self,
        batch: TriggerBatch,
    ) -> PortfolioPipelineResult | TradePlanExecutionResult | None:
        # Trigger plans are per market symbol, while the account is portfolio
        # scoped. Exactly one coordinator may append account/risk/execution
        # facts; the assessment path remains free to observe every asset.
        if self.owner_symbol is not None and batch.symbol != self.owner_symbol:
            return None
        if any(
            item.trigger_type == AnalysisTriggerType.WORLD_MODEL_UPDATED
            for item in batch.triggers
        ):
            slot_at = max(
                item.occurred_at
                for item in batch.triggers
                if item.trigger_type == AnalysisTriggerType.WORLD_MODEL_UPDATED
            )
            return self.capital.produce(
                as_of=slot_at,
                cause_id=batch.batch_id,
                trigger_batch_id=batch.batch_id,
                symbol=batch.symbol,
                trigger_types=tuple(item.trigger_type.value for item in batch.triggers),
            )
        if self.context_cadence_minutes is not None and any(
            item.trigger_type == AnalysisTriggerType.HEARTBEAT for item in batch.triggers
        ):
            cadence_seconds = self.context_cadence_minutes * 60
            slot_at = datetime.fromtimestamp(
                int(batch.created_at.timestamp()) // cadence_seconds * cadence_seconds,
                tz=UTC,
            )
            return self.capital.produce(
                as_of=slot_at,
                cause_id=stable_id(
                    "context_forecast_cadence",
                    self.capital.portfolio_id,
                    cadence_seconds,
                    slot_at.isoformat(),
                ),
                symbol=batch.symbol,
                trigger_types=("FORECAST_CADENCE",),
            )
        return self.capital.review(batch)


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
        families = tuple(item.contract.outcome_family_id for item in forecast_sources)
        if tuple(sorted(set(families))) != tuple(sorted(families)):
            raise ValueError("Capital Forecast source family 必须唯一")
        self._config = config
        self._market = market
        self._forecasts = forecasts
        self._forecast_sources = forecast_sources
        self._source_by_family = {
            item.contract.outcome_family_id: item for item in forecast_sources
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

    @property
    def portfolio_id(self) -> str:
        return self._config.capital.decision.portfolio_id

    def consume(self, batch: TriggerBatch) -> PortfolioPipelineResult | TradePlanExecutionResult:
        """Run capital once for an immutable trigger cause."""

        return self.produce(
            as_of=batch.created_at,
            cause_id=batch.batch_id,
            trigger_batch_id=batch.batch_id,
            symbol=batch.symbol,
            trigger_types=tuple(item.trigger_type.value for item in batch.triggers),
        )

    def review(self, batch: TriggerBatch) -> PortfolioPipelineResult | TradePlanExecutionResult:
        """Protect/reconcile current holdings without waiting for a new Context call."""

        requested_at = require_utc(batch.created_at)
        cause_id = batch.batch_id
        prior = self._cycle_records.get(
            stable_id(
                "capital_cycle_record",
                self._config.capital.decision.portfolio_id,
                self._config.pipeline.version,
                cause_id,
            )
        )
        if prior is not None:
            return self._recorded_result(prior)
        result = self._observe(as_of=requested_at)
        account = self._portfolio.latest_account(
            portfolio_id=self._config.capital.decision.portfolio_id,
            as_of=requested_at,
        )
        if account is None:
            raise ValueError("Capital risk review 缺少账户快照")
        if not account.sleeves:
            # A trigger remains durably visible in the event ledger.  Recording a
            # second capital "action" for an all-cash no-op only creates dashboard
            # noise and has no risk or investment content.
            return result
        return self._finish(
            result=result,
            requested_at=requested_at,
            triggered_at=requested_at,
            generated_forecasts=(),
            cause_id=cause_id,
            trigger_batch_id=batch.batch_id,
            symbol=batch.symbol,
            trigger_types=tuple(item.trigger_type.value for item in batch.triggers),
            forecast_already_decided=False,
            no_estimates=(),
            observation_reason_codes=("PROGRAMMATIC_RISK_REVIEW",),
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
        production_results = tuple(
            source.producer.produce(as_of=requested_at) for source in self._forecast_sources
        )
        generated_forecasts = tuple(
            item for item in production_results if not isinstance(item, ForecastNoEstimate)
        )
        no_estimates = tuple(
            item for item in production_results if isinstance(item, ForecastNoEstimate)
        )
        if not generated_forecasts:
            completed_at = max(
                (requested_at, *(item.completed_at for item in no_estimates))
            )
            return self._finish(
                result=self._observe(as_of=completed_at),
                requested_at=completed_at,
                triggered_at=requested_at,
                generated_forecasts=(),
                cause_id=evaluation_cause_id,
                trigger_batch_id=trigger_batch_id,
                symbol=symbol,
                trigger_types=trigger_types,
                forecast_already_decided=False,
                no_estimates=no_estimates,
            )
        decision_at = max(
            requested_at,
            *(item.available_at for item in generated_forecasts),
        )
        if any(item.valid_until <= decision_at for item in generated_forecasts):
            raise ValueError("Capital Forecast 在其入场有效期后才可用于决策")
        cycle_id = self._forecast_cycle_id(generated_forecasts)
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
                forecast_already_decided=True,
                no_estimates=no_estimates,
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
        decision_quotes = self._quotes_for_sleeves(sleeves=sleeves, quotes=quotes)
        risk_profiles = tuple(
            self._risk_profile(
                item.sleeve_id,
                self._source_by_family[item.forecast.outcome_family_id],
            )
            for item in sleeves
        )
        decision = self._decisions.run(
            cycle_id=cycle_id,
            as_of=decision_at,
            sleeves=sleeves,
            account=account,
            quotes=decision_quotes,
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
                forecast_already_decided=False,
                no_estimates=no_estimates,
            )
        result = self._execution.run(
            plan_id=plan.plan_id,
            as_of=decision_at,
            quotes=decision_quotes,
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
            forecast_already_decided=False,
            no_estimates=no_estimates,
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
        forecast_already_decided: bool,
        no_estimates: tuple[ForecastNoEstimate, ...],
        observation_reason_codes: tuple[str, ...] = (),
    ) -> PortfolioPipelineResult | TradePlanExecutionResult:
        target = result.target if isinstance(result, PortfolioPipelineResult) else None
        if isinstance(result, TradePlanExecutionResult):
            plan = self._plans.plan(result.plan_id)
            if plan is None:
                raise ValueError("Capital execution result 缺少权威 TradePlan")
            target = self._portfolio.target_for_cycle(plan.cycle_id)
        expected_forecast_cycle = (
            self._forecast_cycle_id(generated_forecasts) if generated_forecasts else None
        )
        if target is None and expected_forecast_cycle is not None:
            target = self._portfolio.target_for_cycle(expected_forecast_cycle)
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
                if expected_forecast_cycle is None
                or target.cycle_id != expected_forecast_cycle
                else CapitalCycleOutcome.FORECAST_ALREADY_DECIDED
                if forecast_already_decided
                else CapitalCycleOutcome.TARGET_DECIDED
            )
            reason_codes = target.reason_codes
            decision_cycle_id = target.cycle_id
            forecast_ids = tuple(sorted({*generated_ids, *target.considered_forecast_ids}))
            target_id = target.target_id
        else:
            outcome = (
                CapitalCycleOutcome.HOLD if account.sleeves else CapitalCycleOutcome.CASH
            )
            reason_codes = tuple(
                sorted(
                    {
                        *(("NO_REGISTERED_FORECAST_SOURCE",) if not self._forecast_sources else ()),
                        *(f"FORECAST_NO_ESTIMATE:{item.reason.value}" for item in no_estimates),
                        *observation_reason_codes,
                        *(("HOLDING_RISK_REVIEWED",) if account.sleeves else ()),
                    }
                )
            )
            if not reason_codes:
                raise ValueError("Capital 无 Target 时必须保存精确的未决原因")
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

    def _forecast_cycle_id(
        self,
        forecasts: tuple[Forecast, ...],
    ) -> str:
        return stable_id(
            "capital_forecast_cycle",
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
            source = self._source_by_family.get(forecast.outcome_family_id)
            if source is None:
                raise ValueError("Capital Forecast family 未绑定合格 source")
            sleeve_id = SleeveTarget.identity_for(
                portfolio_id=self._config.capital.decision.portfolio_id,
                forecast_family=forecast.outcome_family_id,
                forecast_target_id=forecast.target.target_id,
            )
            candidate = PortfolioSleeveInput(
                sleeve_id=sleeve_id,
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
                forecast=forecast,
                mock_authorization=source.mock_authorization,
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
            derivative_initial_margin_fraction=(template.derivative_initial_margin_fraction),
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
                    forecast=forecast,
                    mock_authorization=source.mock_authorization,
                )
            )
            profiles.append(self._risk_profile(position.sleeve_id, source))
        decision_quotes = self._quotes_for_sleeves(
            sleeves=tuple(sleeves),
            quotes=quotes,
        )
        protected = self._decisions.protect(
            cycle_id=cycle_id,
            as_of=as_of,
            sleeves=tuple(sleeves),
            account=account,
            quotes=decision_quotes,
            risk_profiles=tuple(profiles),
            execution_specs=self._config.capital.execution_specs,
        )
        plan = protected.trade_plan
        if plan is None:
            protected = self._decisions.run(
                cycle_id=cycle_id,
                as_of=as_of,
                sleeves=tuple(sleeves),
                account=account,
                quotes=decision_quotes,
                risk_profiles=tuple(profiles),
                execution_specs=self._config.capital.execution_specs,
            )
            plan = protected.trade_plan
        if plan is None or not plan.groups:
            return protected
        result = self._execution.run(
            plan_id=plan.plan_id,
            as_of=as_of,
            quotes=decision_quotes,
        )
        self._performance.record(result.account)
        logger.info(
            "capital holding review executed a cash-converging plan",
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
                outcome_family_id=source.contract.outcome_family_id,
                as_of=as_of,
                include_expired=True,
            )
        return self._forecasts.latest_calibrated_for_target(
            target_id=target_id,
            outcome_family_id=source.contract.outcome_family_id,
            as_of=as_of,
            include_expired=True,
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
        instruments = tuple(item.instrument for item in self._config.capital.execution_specs)
        spot = next(item for item in instruments if item.product == InstrumentProduct.SPOT)
        perpetual = next(item for item in instruments if item.product != InstrumentProduct.SPOT)
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

    @staticmethod
    def _quotes_for_sleeves(
        *,
        sleeves: tuple[PortfolioSleeveInput, ...],
        quotes: tuple[ExecutableQuote, ...],
    ) -> tuple[ExecutableQuote, ...]:
        required = {
            leg.instrument.key
            for sleeve in sleeves
            for leg in sleeve.forecast.target.legs
        }
        return tuple(item for item in quotes if item.instrument.key in required)


def assemble_capital_cycle(
    config: AppConfig,
    engine,
    *,
    forecast_sources: tuple[CapitalForecastSource, ...] | None = None,
    code_version: str | None = None,
) -> CapitalCycleService:
    if not config.capital.enabled or config.deployment.stage != DeploymentStage.SHADOW:
        raise ValueError("Capital cycle 只装配显式启用的 SHADOW")
    market = SqlMarketDataStore(engine)
    forecasts = SqlForecastStore(engine)
    contracts = SqlForecastContractStore(engine)
    if forecast_sources is None:
        configured_sources: list[CapitalForecastSource] = []
        program = config.capital.cash_carry_program
        if program is not None and program.enabled:
            authorization = next(
                item
                for item in config.capital.mock_candidate_authorizations
                if (
                    item.producer_id,
                    item.producer_behavior_id,
                    item.outcome_family_id,
                )
                == (
                    program.producer_id,
                    program.producer_behavior_id,
                    program.outcome_family_id,
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
            contract = ForecastContract.create(
                contract_version=program.contract_version,
                outcome_family_id=program.outcome_family_id,
                target=cash_carry_target(spot=spot, perpetual=perpetual),
                allowed_orientations=(ForecastOrientation.CANONICAL,),
                outcome_buckets=program.outcome_buckets,
                horizon_minutes=program.horizon_minutes,
                decision_slot_rule="each-admitted-capital-trigger-v1",
                evaluation_trigger="scheduled-or-material-market-trigger-v1",
                information_cutoff_rule="slot-as-of-point-in-time-v1",
                completion_deadline_seconds=program.completion_deadline_seconds,
                minimum_remaining_horizon_minutes=(program.minimum_remaining_horizon_minutes),
                entry_anchor_rule="first-executable-quote-after-completion-v1",
                cost_semantics_version=config.capital.decision.cost_model_version,
                validity_minutes=program.validity_minutes,
                validity_conditions=("EXECUTABLE_QUOTES_REMAIN_VALID",),
                settlement_rule="cutoff-to-horizon-executable-with-funding-v1",
                forecast_benchmark=program.forecast_benchmark,
                decision_benchmark="cash-v1",
            )
            binding = ForecastProducerBinding(
                binding_id=stable_id(
                    "forecast_producer_binding",
                    contract.contract_id,
                    ForecastProducerKind.PROGRAM.value,
                    program.producer_id,
                    program.producer_behavior_id,
                    ForecastPermission.MOCK.value,
                    (),
                    None,
                ),
                contract_id=contract.contract_id,
                producer_kind=ForecastProducerKind.PROGRAM,
                producer_id=program.producer_id,
                producer_behavior_id=program.producer_behavior_id,
                permission=ForecastPermission.MOCK,
            )
            configured_sources.append(
                CapitalForecastSource(
                    contract=contract,
                    binding=binding,
                    producer=CashCarryForecastProducer(
                        policy=program,
                        contract=contract,
                        binding=binding,
                        market=market,
                        contracts=contracts,
                        forecasts=forecasts,
                        spot=spot,
                        perpetual=perpetual,
                    ),
                    risk_template=config.capital.sleeve_risk,
                    mock_authorization=authorization,
                )
            )
        context = config.capital.context_forecast
        if context is not None and context.enabled:
            if code_version is None:
                raise ValueError("装配 Context Forecast 必须冻结 code_version")
            instrument = next(
                (
                    item.instrument
                    for item in config.capital.execution_specs
                    if item.instrument.key == context.target_instrument_key
                ),
                None,
            )
            if instrument is None:
                raise ValueError("Context Forecast target 不在 Capital execution_specs")
            perpetual = next(
                (
                    item.instrument
                    for item in config.capital.execution_specs
                    if item.instrument.product != InstrumentProduct.SPOT
                    and item.instrument.base_asset == instrument.base_asset
                    and item.instrument.quote_asset == instrument.quote_asset
                ),
                None,
            )
            contract = context_spot_forecast_contract(
                policy=context,
                instrument=instrument,
                cost_semantics_version=config.capital.decision.cost_model_version,
            )
            binding = ForecastProducerBinding(
                binding_id=stable_id(
                    "forecast_producer_binding",
                    contract.contract_id,
                    ForecastProducerKind.CONTEXT.value,
                    context.producer_id,
                    context.producer_behavior_id,
                    ForecastPermission.MOCK.value,
                    context.required_feature_keys,
                    context.maximum_world_model_age_seconds,
                ),
                contract_id=contract.contract_id,
                producer_kind=ForecastProducerKind.CONTEXT,
                producer_id=context.producer_id,
                producer_behavior_id=context.producer_behavior_id,
                permission=ForecastPermission.MOCK,
                required_feature_keys=context.required_feature_keys,
                maximum_world_model_age_seconds=(context.maximum_world_model_age_seconds),
            )
            authorization = next(
                item
                for item in config.capital.mock_candidate_authorizations
                if (
                    item.producer_id,
                    item.producer_behavior_id,
                    item.outcome_family_id,
                )
                == (
                    context.producer_id,
                    context.producer_behavior_id,
                    context.outcome_family_id,
                )
            )
            configured_sources.append(
                CapitalForecastSource(
                    contract=contract,
                    binding=binding,
                    producer=ContextForecastProducer(
                        policy=context,
                        contract=contract,
                        binding=binding,
                        market=market,
                        contexts=SqlContextAssessmentStore(engine),
                        contracts=contracts,
                        forecasts=forecasts,
                        instrument=instrument,
                        target_states=MarketContextTargetStateProvider(
                            market=market,
                            feature_policy=config.feature,
                            spot=instrument,
                            perpetual=perpetual,
                            interval=config.market_data.interval,
                            bar_window=config.market_data.bar_window,
                            funding_lookback_hours=(
                                config.market_data.funding_history_lookback_hours
                            ),
                            maximum_quote_skew_seconds=(
                                config.market_data.maximum_cross_market_quote_skew_seconds
                            ),
                        ),
                        analysis_scope=config.assessment.mandate.analysis_scope,
                        analyst=assemble_codex_context_forecast_analyst(
                            config,
                            policy=context,
                            contract=contract,
                            code_version=code_version,
                            leases=SqlAccountLeaseStore(engine),
                            audit=SqlCodexAuditStore(engine),
                        ),
                    ),
                    risk_template=config.capital.sleeve_risk,
                    mock_authorization=authorization,
                )
            )
        forecast_sources = tuple(configured_sources)
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
