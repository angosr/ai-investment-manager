"""Low-frequency product capital application assembled from the authoritative domains."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from investment_manager.execution.venue.product import ProductOrderVenue
from investment_manager.forecast.codex.repository import (
    SqlAccountLeaseStore,
    SqlCodexAuditStore,
)
from investment_manager.forecast.context.estimate import (
    ContextForecastTargetStateBehavior,
    assemble_codex_context_forecast_analyst,
)
from investment_manager.forecast.context.producer import (
    ContextForecastPreflight,
    ContextForecastRuntimeTarget,
    ForecastProductionResult,
    MarketContextTargetStateProvider,
    PortfolioContextForecastProducer,
    context_forecast_contract,
)
from investment_manager.forecast.context.repository import SqlContextAssessmentStore
from investment_manager.forecast.contract_repository import SqlForecastContractStore
from investment_manager.forecast.contracts import (
    ForecastContract,
    ForecastNoEstimate,
    ForecastPermission,
    ForecastProducerBinding,
    ForecastProducerKind,
    ForecastSlotCause,
)
from investment_manager.forecast.product.context import ContextProductPayoffProjector
from investment_manager.forecast.product.models import ProductPayoffProjection
from investment_manager.forecast.product.repository import SqlProductPayoffProjectionStore
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.forecast.results import BaseForecast, Forecast
from investment_manager.kernel.errors import PointInTimeInputUnavailable
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.market.features import freeze_quote_views
from investment_manager.market.models import (
    ExecutableQuote,
    InstrumentId,
    InstrumentProduct,
    SpotVenue,
    ValuationQuote,
)
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.portfolio.decision import (
    PortfolioDecisionEngine,
    PortfolioSleeveInput,
)
from investment_manager.portfolio.models import (
    CandidateCapitalAuthorization,
    CapitalCycleOutcome,
    CapitalCycleRecord,
    PortfolioAccountSnapshot,
    SleevePosition,
    SleeveTarget,
)
from investment_manager.portfolio.policy import CapitalPolicy, SleeveRiskTemplate
from investment_manager.portfolio.repository import (
    SqlCapitalCycleStore,
    SqlPortfolioPerformanceStore,
    SqlPortfolioStore,
)
from investment_manager.risk.models import RiskOutcome
from investment_manager.risk.portfolio import (
    PortfolioRiskEngine,
    RiskReductionAuthorization,
    SleeveRiskProfile,
)
from investment_manager.risk.repository import SqlPortfolioRiskStore
from investment_manager.scheduling.models import AnalysisTriggerType, TriggerBatch
from investment_manager.settings import AppConfig

logger = logging.getLogger(__name__)


class CapitalForecastProducer(Protocol):
    def existing_result(
        self,
        *,
        as_of: datetime,
        cause: ForecastSlotCause | None = None,
    ) -> ForecastProductionResult | None: ...

    def produce(
        self,
        *,
        as_of: datetime,
        cause: ForecastSlotCause | None = None,
    ) -> ForecastProductionResult: ...

    def record_deadline_missed(
        self,
        *,
        as_of: datetime,
        completed_at: datetime,
        cause: ForecastSlotCause | None = None,
    ) -> ForecastProductionResult: ...

    def recover_deadline_missed(
        self,
        *,
        before_as_of: datetime,
        completed_at: datetime,
    ) -> tuple[ForecastNoEstimate, ...]: ...


class CapitalProductPayoffProjector(Protocol):
    """Deterministic product expressions owned by the Forecast domain."""

    @property
    def candidate_instruments(self) -> tuple[InstrumentId, ...]: ...

    def project(
        self,
        forecast: BaseForecast,
        *,
        as_of: datetime,
    ) -> tuple[ProductPayoffProjection, ...]: ...

    def for_source(self, source_forecast_id: str) -> tuple[ProductPayoffProjection, ...]: ...


@dataclass(frozen=True, slots=True)
class CapitalForecastSource:
    """One contract-bound producer and its risk/permission envelope."""

    contract: ForecastContract
    binding: ForecastProducerBinding
    producer: CapitalForecastProducer
    risk_template: SleeveRiskTemplate
    capital_authorization: CandidateCapitalAuthorization
    product_payoffs: CapitalProductPayoffProjector | None = None

    def __post_init__(self) -> None:
        if self.binding.contract_id != self.contract.contract_id:
            raise ValueError("Capital Forecast source 的 Contract/Binding 不一致")
        permission = self.capital_authorization
        if (
            permission.producer_id != self.binding.producer_id
            or permission.producer_behavior_id != self.binding.producer_behavior_id
            or permission.outcome_family_id != self.contract.outcome_family_id
            or self.binding.permission != ForecastPermission.CAPITAL_CANDIDATE
        ):
            raise ValueError("Capital Forecast source 与资本授权不一致")


@dataclass(frozen=True, slots=True)
class CapitalTriggerConsumer:
    """Own capital on one coordinator and create idempotent Context slots."""

    capital: CapitalCycleService
    context_cadence_minutes: int | None = None
    context_completion_deadline_seconds: int | None = None
    material_event_slots_enabled: bool = False
    material_event_slot_policy_version: str | None = None
    owner_symbol: str | None = None
    context_activation_at: datetime | None = None

    def __post_init__(self) -> None:
        if (self.context_cadence_minutes is None) != (
            self.context_completion_deadline_seconds is None
        ):
            raise ValueError("Context cadence 与完成截止秒数必须同时配置")
        if self.material_event_slots_enabled != (
            self.material_event_slot_policy_version is not None
        ):
            raise ValueError("材料事件 Forecast 槽启用状态与政策版本必须同时配置")
        if self.material_event_slots_enabled and self.context_cadence_minutes is None:
            raise ValueError("材料事件 Forecast 槽必须复用 Context Forecast 合同")
        if self.context_activation_at is not None:
            require_utc(self.context_activation_at)

    def consume(
        self,
        batch: TriggerBatch,
    ) -> PortfolioPipelineResult | TradePlanExecutionResult | None:
        # Trigger plans are per market symbol, while the account is portfolio
        # scoped. Exactly one coordinator may append account/risk/execution
        # facts; the assessment path remains free to observe every asset.
        if self.owner_symbol is not None and batch.symbol != self.owner_symbol:
            return None
        material_triggers = tuple(
            item
            for item in batch.triggers
            if item.trigger_type == AnalysisTriggerType.FORECAST_EVENT_DUE
        )
        cadence_due = self.context_cadence_minutes is not None and any(
            item.trigger_type
            in {
                AnalysisTriggerType.HEARTBEAT,
                AnalysisTriggerType.FORECAST_SLOT_DUE,
            }
            for item in batch.triggers
        )
        if material_triggers:
            if not self.material_event_slots_enabled:
                return self._consume_cadence(batch) if cadence_due else self.capital.review(batch)
            assert self.material_event_slot_policy_version is not None
            assert self.context_completion_deadline_seconds is not None
            slot_at = max(item.occurred_at for item in material_triggers)
            if self.context_activation_at is not None and slot_at < self.context_activation_at:
                return self._consume_cadence(batch) if cadence_due else self.capital.review(batch)
            trigger_refs = tuple(
                sorted(
                    {
                        *(item.trigger_id for item in material_triggers),
                        *(
                            evidence_id
                            for item in material_triggers
                            for evidence_id in item.evidence_ids
                        ),
                    }
                )
            )
            cause = ForecastSlotCause.material_state(
                policy_version=self.material_event_slot_policy_version,
                trigger_refs=trigger_refs,
            )
            event_cause_id = stable_id(
                "context_forecast_material_event",
                self.capital.portfolio_id,
                cause.policy_version,
                *cause.trigger_refs,
            )
            if self.capital.cause_completed(event_cause_id):
                return self._consume_cadence(batch) if cadence_due else self.capital.review(batch)
            outputs_complete = self.capital.forecast_outputs_complete(
                as_of=slot_at,
                cause=cause,
            )
            if not outputs_complete and batch.created_at > slot_at + timedelta(
                seconds=self.context_completion_deadline_seconds
            ):
                self.capital.record_missed_forecast(
                    slot_at=slot_at,
                    completed_at=batch.created_at,
                    cause=cause,
                )
                return self._consume_cadence(batch) if cadence_due else self.capital.review(batch)
            cadence_first = cadence_due and self._cadence_slot_at(batch.created_at) <= slot_at
            if cadence_first:
                self._consume_cadence(batch)
            result = self.capital.produce(
                as_of=slot_at,
                decision_at=batch.created_at if outputs_complete else None,
                cause_id=event_cause_id,
                trigger_batch_id=batch.batch_id,
                symbol=batch.symbol,
                trigger_types=(AnalysisTriggerType.FORECAST_EVENT_DUE.value,),
                cause=cause,
            )
            if cadence_due and not cadence_first:
                self._consume_cadence(batch)
            return result
        if cadence_due:
            return self._consume_cadence(batch)
        return self.capital.review(batch)

    def _cadence_slot_at(self, at: datetime) -> datetime:
        assert self.context_cadence_minutes is not None
        cadence_seconds = self.context_cadence_minutes * 60
        return datetime.fromtimestamp(
            int(at.timestamp()) // cadence_seconds * cadence_seconds,
            tz=UTC,
        )

    def _cadence_cause_id(self, slot_at: datetime) -> str:
        assert self.context_cadence_minutes is not None
        return stable_id(
            "context_forecast_cadence",
            self.capital.portfolio_id,
            self.context_cadence_minutes * 60,
            slot_at.isoformat(),
        )

    def _consume_cadence(
        self,
        batch: TriggerBatch,
    ) -> PortfolioPipelineResult | TradePlanExecutionResult | None:
        assert self.context_cadence_minutes is not None
        assert self.context_completion_deadline_seconds is not None
        slot_at = self._cadence_slot_at(batch.created_at)
        if self.context_activation_at is not None and slot_at < self.context_activation_at:
            return self.capital.review(batch)
        cadence_cause_id = self._cadence_cause_id(slot_at)
        self.capital.recover_missed_forecasts(
            before_slot_at=slot_at,
            completed_at=batch.created_at,
        )
        if self.capital.cause_completed(cadence_cause_id):
            return self.capital.review(batch)
        if self.capital.forecast_outputs_complete(as_of=slot_at):
            return self.capital.produce(
                as_of=slot_at,
                decision_at=batch.created_at,
                cause_id=cadence_cause_id,
                trigger_batch_id=batch.batch_id,
                symbol=batch.symbol,
                trigger_types=("FORECAST_CADENCE",),
            )
        if batch.created_at > slot_at + timedelta(seconds=self.context_completion_deadline_seconds):
            self.capital.record_missed_forecast(
                slot_at=slot_at,
                completed_at=batch.created_at,
            )
            return self.capital.review(batch)
        return self.capital.produce(
            as_of=slot_at,
            cause_id=cadence_cause_id,
            trigger_batch_id=batch.batch_id,
            symbol=batch.symbol,
            trigger_types=("FORECAST_CADENCE",),
        )


class CapitalCycleService:
    """Run one idempotent point-in-time capital decision and persistent execution."""

    def __init__(
        self,
        *,
        capital_policy: CapitalPolicy,
        capital_behavior_id: str,
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
        context_activation_at: datetime | None = None,
    ) -> None:
        families = tuple(item.contract.outcome_family_id for item in forecast_sources)
        if tuple(sorted(set(families))) != tuple(sorted(families)):
            raise ValueError("Capital Forecast source family 必须唯一")
        self._policy = capital_policy
        self._capital_behavior_id = capital_behavior_id
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
        self.context_activation_at = (
            require_utc(context_activation_at) if context_activation_at is not None else None
        )

    @property
    def portfolio_id(self) -> str:
        return self._policy.decision.portfolio_id

    def cause_completed(self, cause_id: str) -> bool:
        """Whether this pipeline already completed one durable capital cause."""

        return (
            self._cycle_records.get(
                stable_id(
                    "capital_cycle_record",
                    self._policy.decision.portfolio_id,
                    self._capital_behavior_id,
                    cause_id,
                )
            )
            is not None
        )

    def forecast_outputs_complete(
        self,
        *,
        as_of: datetime,
        cause: ForecastSlotCause | None = None,
    ) -> bool:
        """Whether every configured producer already wrote a terminal slot output."""

        return bool(self._forecast_sources) and all(
            source.producer.existing_result(as_of=as_of, cause=cause) is not None
            for source in self._forecast_sources
        )

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
                self._policy.decision.portfolio_id,
                self._capital_behavior_id,
                cause_id,
            )
        )
        if prior is not None:
            return self._recorded_result(prior)
        result = self._observe(as_of=requested_at)
        account = self._portfolio.latest_account(
            portfolio_id=self._policy.decision.portfolio_id,
            as_of=requested_at,
        )
        if account is None:
            raise PointInTimeInputUnavailable("Capital risk review 缺少账户快照")
        if (
            not account.sleeves
            and isinstance(result, PortfolioPipelineResult)
            and result.outcome == PortfolioPipelineOutcome.NO_CHANGE
        ):
            # A trigger remains durably visible in the event ledger.  Recording a
            # second capital "action" for an all-cash no-op only creates dashboard
            # noise and has no risk or investment content.  A review that just
            # executed an exit is materially different even though its final
            # account is now all cash, and must continue into `_finish`.
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
        decision_at: datetime | None = None,
        cause_id: str | None = None,
        trigger_batch_id: str | None = None,
        symbol: str = "SYSTEM",
        trigger_types: tuple[str, ...] = (),
        cause: ForecastSlotCause | None = None,
    ) -> PortfolioPipelineResult | TradePlanExecutionResult:
        requested_at = require_utc(as_of)
        resume_at = require_utc(decision_at) if decision_at is not None else requested_at
        if resume_at < requested_at:
            raise ValueError("Capital decision_at 不能早于 Forecast slot")
        evaluation_cause_id = cause_id or stable_id(
            "capital_manual_evaluation",
            self._policy.decision.portfolio_id,
            self._capital_behavior_id,
            requested_at.isoformat(),
        )
        prior_record = self._cycle_records.get(
            stable_id(
                "capital_cycle_record",
                self._policy.decision.portfolio_id,
                self._capital_behavior_id,
                evaluation_cause_id,
            )
        )
        if prior_record is not None:
            if (
                prior_record.triggered_at != requested_at
                or prior_record.symbol != symbol
                or prior_record.trigger_types != tuple(sorted(set(trigger_types)))
            ):
                raise ValueError("Capital evaluation cause 已绑定不同触发事实")
            return self._recorded_result(prior_record)
        production_results = tuple(
            source.producer.produce(as_of=requested_at)
            if cause is None
            else source.producer.produce(as_of=requested_at, cause=cause)
            for source in self._forecast_sources
        )
        generated_forecasts = tuple(
            item for item in production_results if not isinstance(item, ForecastNoEstimate)
        )
        no_estimates = tuple(
            item for item in production_results if isinstance(item, ForecastNoEstimate)
        )
        if not generated_forecasts:
            completed_at = max((requested_at, *(item.completed_at for item in no_estimates)))
            account_head = self._portfolio.head_account(
                portfolio_id=self._policy.decision.portfolio_id
            )
            # A recovered cadence slot may predate the account ledger head.  Its
            # no-estimate result already lives in the Forecast audit trail; it is
            # not a new capital decision and must not create an action receipt.
            if account_head is not None and completed_at < account_head.as_of:
                return self._observe(as_of=account_head.as_of)
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
            resume_at,
            *(item.available_at for item in generated_forecasts),
        )
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

        recovered_groups = self._recover(as_of=decision_at, cycle_id=cycle_id)
        valuation_instruments, execution_instruments = self._current_quote_requirements(
            as_of=decision_at,
            recovered_groups=recovered_groups,
        )
        execution_instruments = self._merge_instruments(
            execution_instruments,
            self._candidate_instruments(generated_forecasts),
        )
        valuation_quotes, executable_quotes = self._quote_views(
            as_of=decision_at,
            valuation_instruments=valuation_instruments,
            execution_instruments=execution_instruments,
        )
        account = self._account(
            as_of=decision_at,
            quotes=valuation_quotes,
        )
        sleeves = self._decision_sleeves(
            forecasts=generated_forecasts,
            account=account,
            as_of=decision_at,
        )
        decision_quotes = self._quotes_for_sleeves(
            sleeves=sleeves,
            quotes=executable_quotes,
        )
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
            execution_specs=self._policy.execution_specs,
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
            "capital cycle executed trade plan",
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

    def record_missed_forecast(
        self,
        *,
        slot_at: datetime,
        completed_at: datetime,
        cause: ForecastSlotCause | None = None,
    ) -> tuple[ForecastProductionResult, ...]:
        """Ensure a late slot has a terminal result without rewriting an existing one."""

        slot = require_utc(slot_at)
        completed = require_utc(completed_at)
        results = tuple(
            (
                source.producer.record_deadline_missed(
                    as_of=slot,
                    completed_at=completed,
                )
                if cause is None
                else source.producer.record_deadline_missed(
                    as_of=slot,
                    completed_at=completed,
                    cause=cause,
                )
            )
            for source in self._forecast_sources
        )
        # ``record_deadline_missed`` is idempotent at the producer boundary.  A
        # Forecast returned here is necessarily the immutable result already
        # recorded for this behavior/slot, not hindsight production.
        return results

    def recover_missed_forecasts(
        self,
        *,
        before_slot_at: datetime,
        completed_at: datetime,
    ) -> tuple[ForecastNoEstimate, ...]:
        recovered = tuple(
            result
            for source in self._forecast_sources
            for result in source.producer.recover_deadline_missed(
                before_as_of=before_slot_at,
                completed_at=completed_at,
            )
        )
        return recovered

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
        reduction_authorization = (
            result.holding_risk_review.reduction_authorization
            if isinstance(result, PortfolioPipelineResult)
            and result.holding_risk_review is not None
            else None
        )
        if isinstance(result, TradePlanExecutionResult):
            plan = self._plans.plan(result.plan_id)
            if plan is None:
                raise ValueError("Capital execution result 缺少权威 TradePlan")
            target = self._portfolio.target_for_cycle(plan.cycle_id)
            if target is None:
                loaded_authorization = self._risks.execution_authorization(plan.approved_target_id)
                if not isinstance(loaded_authorization, RiskReductionAuthorization):
                    raise ValueError("无 PortfolioTarget 的执行结果缺少只减险授权")
                reduction_authorization = loaded_authorization
        expected_forecast_cycle = (
            self._forecast_cycle_id(generated_forecasts) if generated_forecasts else None
        )
        if target is None and expected_forecast_cycle is not None:
            target = self._portfolio.target_for_cycle(expected_forecast_cycle)
        account = self._portfolio.latest_account(
            portfolio_id=self._policy.decision.portfolio_id,
            as_of=requested_at,
        )
        if account is None:
            raise ValueError("Capital cycle 缺少最终账户快照")
        generated_ids = tuple(item.forecast_id for item in generated_forecasts)
        if reduction_authorization is not None:
            outcome = CapitalCycleOutcome.RISK_EXIT
            reason_codes = reduction_authorization.reason_codes
            decision_cycle_id = reduction_authorization.cycle_id
            forecast_ids = tuple(sorted(set(generated_ids)))
            target_id = None
            execution_authorization_id = reduction_authorization.authorization_id
        elif target is not None:
            if expected_forecast_cycle is not None and target.cycle_id != expected_forecast_cycle:
                raise ValueError("Capital Target 与本轮 Forecast cycle 不一致")
            outcome = (
                CapitalCycleOutcome.FORECAST_ALREADY_DECIDED
                if forecast_already_decided
                else CapitalCycleOutcome.TARGET_DECIDED
            )
            reason_codes = target.reason_codes
            decision_cycle_id = target.cycle_id
            forecast_ids = tuple(sorted({*generated_ids, *target.considered_forecast_ids}))
            target_id = target.target_id
            execution_authorization_id = None
        else:
            outcome = CapitalCycleOutcome.HOLD if account.sleeves else CapitalCycleOutcome.CASH
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
            execution_authorization_id = None
        self._cycle_records.record(
            CapitalCycleRecord.create(
                portfolio_id=self._policy.decision.portfolio_id,
                # The legacy storage field is named ``pipeline_id``.  Its value is
                # deliberately the venue-neutral capital behavior identity, never
                # a system-stage or execution-venue identity.
                pipeline_id=self._capital_behavior_id,
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
                execution_authorization_id=execution_authorization_id,
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
            self._policy.decision.portfolio_id,
            self._policy.decision.version,
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
            try:
                candidates = self._forecast_sleeve_inputs(
                    source=source,
                    forecast=forecast,
                    as_of=as_of,
                )
            except PointInTimeInputUnavailable:
                if not any(
                    item.forecast_family == forecast.outcome_family_id for item in account.sleeves
                ):
                    raise
                candidates = ()
            for candidate in candidates:
                existing = by_sleeve.get(candidate.sleeve_id)
                if existing is not None and existing != candidate:
                    raise ValueError("同一 Capital Sleeve 收到多个不同 Forecast")
                by_sleeve[candidate.sleeve_id] = candidate
        for position in account.sleeves:
            if position.sleeve_id in by_sleeve:
                continue
            by_sleeve[position.sleeve_id] = self._position_sleeve_input(
                position=position,
                as_of=as_of,
            )
        return tuple(by_sleeve[item] for item in sorted(by_sleeve))

    def _forecast_sleeve_inputs(
        self,
        *,
        source: CapitalForecastSource,
        forecast: Forecast,
        as_of: datetime,
    ) -> tuple[PortfolioSleeveInput, ...]:
        projector = source.product_payoffs
        if projector is None:
            projections: tuple[ProductPayoffProjection | None, ...] = (None,)
        else:
            if not isinstance(forecast, BaseForecast):
                raise ValueError("Product payoff 当前只允许 BaseForecast")
            projected = projector.project(forecast, as_of=as_of)
            if not projected:
                raise PointInTimeInputUnavailable("Forecast 没有可执行产品收益投影")
            projections = projected
        return tuple(
            PortfolioSleeveInput(
                sleeve_id=SleeveTarget.identity_for(
                    portfolio_id=self._policy.decision.portfolio_id,
                    forecast_family=forecast.outcome_family_id,
                    forecast_target_id=(
                        forecast.target.target_id
                        if projection is None
                        else projection.target.target_id
                    ),
                ),
                forecast=forecast,
                payoff_projection=projection,
                capital_authorization=source.capital_authorization,
            )
            for projection in projections
        )

    def _position_sleeve_input(
        self,
        *,
        position: SleevePosition,
        as_of: datetime,
    ) -> PortfolioSleeveInput:
        source = self._source_by_family.get(position.forecast_family)
        if source is None:
            raise ValueError("当前 Capital Sleeve 缺少合格 Forecast source")
        target_id = (
            source.contract.target.target_id
            if source.product_payoffs is not None
            else position.target.target_id
        )
        forecast = self._latest_forecast(
            source=source,
            target_id=target_id,
            as_of=as_of,
        )
        if forecast is None:
            raise ValueError("当前 Capital Sleeve 缺少权威来源 Forecast")
        projection = None
        projection_current = True
        if source.product_payoffs is not None:
            if not isinstance(forecast, BaseForecast):
                raise ValueError("Product payoff 当前只允许 BaseForecast")
            if as_of < forecast.economic_horizon_end:
                try:
                    available = source.product_payoffs.project(forecast, as_of=as_of)
                except PointInTimeInputUnavailable:
                    available = ()
            else:
                available = ()
            projection = next(
                (
                    item
                    for item in reversed(available)
                    if item.target == position.target and item.projected_at <= as_of
                ),
                None,
            )
            if projection is None:
                projection = next(
                    (
                        item
                        for item in reversed(
                            source.product_payoffs.for_source(forecast.forecast_id)
                        )
                        if item.target == position.target and item.projected_at <= as_of
                    ),
                    None,
                )
                projection_current = False
            if projection is None:
                raise ValueError("当前 Capital Sleeve 缺少原始产品收益投影")
        return PortfolioSleeveInput(
            sleeve_id=position.sleeve_id,
            forecast=forecast,
            payoff_projection=projection,
            payoff_projection_current=projection_current,
            capital_authorization=source.capital_authorization,
            new_capital_allowed=False,
        )

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
            portfolio_id=self._policy.decision.portfolio_id,
        )
        if account is None:
            return None
        return TradePlanExecutionResult(
            plan_id=plan.plan_id,
            groups=groups,
            account=account,
        )

    def _recover(self, *, as_of: datetime, cycle_id: str) -> tuple[ExecutionGroup, ...]:
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
        return recovered

    def _observe(
        self,
        *,
        as_of: datetime,
    ) -> PortfolioPipelineResult | TradePlanExecutionResult:
        cycle_id = stable_id(
            "capital_observation",
            self._policy.version,
            self._policy.decision.portfolio_id,
            as_of.isoformat(),
        )
        recovered_groups = self._recover(as_of=as_of, cycle_id=cycle_id)
        valuation_instruments, execution_instruments = self._current_quote_requirements(
            as_of=as_of,
            recovered_groups=recovered_groups,
        )
        valuation_quotes, executable_quotes = self._quote_views(
            as_of=as_of,
            valuation_instruments=valuation_instruments,
            execution_instruments=execution_instruments,
        )
        account = self._account(
            as_of=as_of,
            quotes=valuation_quotes,
        )
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
            sleeves.append(self._position_sleeve_input(position=position, as_of=as_of))
            profiles.append(self._risk_profile(position.sleeve_id, source))
        decision_quotes = self._quotes_for_sleeves(
            sleeves=tuple(sleeves),
            quotes=executable_quotes,
        )
        protected = self._decisions.protect(
            cycle_id=cycle_id,
            as_of=as_of,
            account=account,
            quotes=decision_quotes,
            risk_profiles=tuple(profiles),
            execution_specs=self._policy.execution_specs,
        )
        plan = protected.trade_plan
        if (
            plan is None
            and protected.holding_risk_review is not None
            and protected.holding_risk_review.outcome.value == "HOLD"
        ):
            protected = self._decisions.run(
                cycle_id=cycle_id,
                as_of=as_of,
                sleeves=tuple(sleeves),
                account=account,
                quotes=decision_quotes,
                risk_profiles=tuple(profiles),
                execution_specs=self._policy.execution_specs,
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
        return self._forecasts.latest_base_for_target(
            target_id=target_id,
            outcome_family_id=source.contract.outcome_family_id,
            as_of=as_of,
            include_expired=True,
        )

    def _account(
        self,
        *,
        as_of: datetime,
        quotes: tuple[ValuationQuote, ...],
    ) -> PortfolioAccountSnapshot:
        portfolio_id = self._policy.decision.portfolio_id
        projection_cycle_id = stable_id(
            "portfolio_account_projection",
            portfolio_id,
            as_of.isoformat(),
        )
        with self._portfolio.account_projection_lock(portfolio_id=portfolio_id):
            head = self._portfolio.head_account(portfolio_id=portfolio_id)
            if head is not None and head.as_of > as_of:
                raise ValueError("Capital account 不允许倒序投影")
            account = head if head is not None and head.as_of == as_of else None
            if account is None:
                account = self._portfolio.account_for_cycle(
                    cycle_id=projection_cycle_id,
                    portfolio_id=portfolio_id,
                )
            if account is None:
                account = self._accounts.project(
                    cycle_id=projection_cycle_id,
                    as_of=as_of,
                    quotes=quotes,
                )
                self._portfolio.record_account(account)
        self._performance.record(account)
        return account

    def _quote_views(
        self,
        *,
        as_of: datetime,
        valuation_instruments: tuple[InstrumentId, ...],
        execution_instruments: tuple[InstrumentId, ...],
    ) -> tuple[tuple[ValuationQuote, ...], tuple[ExecutableQuote, ...]]:
        valuation_keys = {item.key for item in valuation_instruments}
        execution_keys = {item.key for item in execution_instruments}
        instruments = self._merge_instruments(
            valuation_instruments,
            execution_instruments,
        )
        schedule = (
            self._market.latest_trading_schedule(as_of=as_of)
            if any(item.product == InstrumentProduct.TRADFI_PERPETUAL for item in instruments)
            else None
        )
        valuations: list[ValuationQuote] = []
        executables: list[ExecutableQuote] = []
        for instrument in instruments:
            if instrument.product == InstrumentProduct.SPOT:
                observed = self._market.latest_spot_quote(
                    instrument=instrument,
                    evaluation_at=as_of,
                    visible_at=as_of,
                )
            else:
                observed = self._market.latest_perpetual_quote(
                    instrument=instrument,
                    evaluation_at=as_of,
                    visible_at=as_of,
                )
            if observed is None:
                requirement = (
                    "估值与可执行"
                    if instrument.key in valuation_keys and instrument.key in execution_keys
                    else "估值"
                    if instrument.key in valuation_keys
                    else "可执行"
                )
                raise PointInTimeInputUnavailable(
                    f"Capital 缺少 {instrument.key} {requirement}报价"
                )
            valuation, executable = freeze_quote_views(
                instrument=instrument,
                quote=observed,
                as_of=as_of,
                maximum_live_age_seconds=(self._policy.risk.maximum_quote_age_seconds),
                trading_schedule=(
                    schedule if instrument.product == InstrumentProduct.TRADFI_PERPETUAL else None
                ),
            )
            if instrument.key in valuation_keys:
                valuations.append(valuation)
            if instrument.key in execution_keys and executable is not None:
                executables.append(executable)
        observed_times = tuple(item.observed_at for item in executables)
        if (
            observed_times
            and (max(observed_times) - min(observed_times)).total_seconds()
            > self._policy.risk.maximum_quote_skew_seconds
        ):
            raise PointInTimeInputUnavailable("Capital 多产品可成交报价时间偏差过大")
        return (
            tuple(sorted(valuations, key=lambda item: item.instrument.key)),
            tuple(sorted(executables, key=lambda item: item.instrument.key)),
        )

    def _current_quote_requirements(
        self,
        *,
        as_of: datetime,
        recovered_groups: tuple[ExecutionGroup, ...],
    ) -> tuple[tuple[InstrumentId, ...], tuple[InstrumentId, ...]]:
        account = self._portfolio.latest_account(
            portfolio_id=self._policy.decision.portfolio_id,
            as_of=as_of,
        )
        valuation = tuple(
            item.instrument for item in (() if account is None else account.positions)
        )
        execution = tuple(
            leg.instrument
            for sleeve in (() if account is None else account.sleeves)
            for leg in sleeve.target.legs
        )
        recovered_instruments = tuple(
            leg.instrument
            for group in recovered_groups
            for leg in (*group.target_legs, *group.compensation_legs)
        )
        return (
            self._merge_instruments(valuation, recovered_instruments),
            self._merge_instruments(execution, recovered_instruments),
        )

    @staticmethod
    def _merge_instruments(
        *groups: tuple[InstrumentId, ...],
    ) -> tuple[InstrumentId, ...]:
        by_key: dict[str, InstrumentId] = {}
        for group in groups:
            for item in group:
                existing = by_key.get(item.key)
                if existing is not None and existing != item:
                    raise ValueError("相同 Instrument key 出现冲突的产品定义")
                by_key[item.key] = item
        return tuple(by_key[key] for key in sorted(by_key))

    def _candidate_instruments(
        self,
        forecasts: tuple[Forecast, ...],
    ) -> tuple[InstrumentId, ...]:
        candidates: list[InstrumentId] = []
        for forecast in forecasts:
            source = self._source_by_family.get(forecast.outcome_family_id)
            if source is None:
                raise ValueError("Capital Forecast family 未绑定合格 source")
            candidates.extend(
                source.product_payoffs.candidate_instruments
                if source.product_payoffs is not None
                else (leg.instrument for leg in forecast.target.legs)
            )
        return self._merge_instruments(tuple(candidates))

    @staticmethod
    def _quotes_for_sleeves(
        *,
        sleeves: tuple[PortfolioSleeveInput, ...],
        quotes: tuple[ExecutableQuote, ...],
    ) -> tuple[ExecutableQuote, ...]:
        required = {leg.instrument.key for sleeve in sleeves for leg in sleeve.target.legs}
        selected = tuple(item for item in quotes if item.instrument.key in required)
        missing = required - {item.instrument.key for item in selected}
        if missing:
            raise PointInTimeInputUnavailable(
                "Capital 候选当前不可执行: " + ", ".join(sorted(missing))
            )
        return selected


def assemble_capital_cycle(
    config: AppConfig,
    engine,
    *,
    venue: ProductOrderVenue,
    initial_cash: Decimal,
    forecast_sources: tuple[CapitalForecastSource, ...] | None = None,
    code_version: str | None = None,
    producer_activation_at: datetime | None = None,
    context_forecast_preflight_factory: (
        Callable[[tuple[ForecastContract, ...]], ContextForecastPreflight] | None
    ) = None,
) -> CapitalCycleService:
    if not config.capital.enabled:
        raise ValueError("Capital cycle 未启用")
    if initial_cash <= 0:
        raise ValueError("Capital 初始现金必须为正数")
    market = SqlMarketDataStore(engine)
    forecasts = SqlForecastStore(engine)
    world_models = SqlContextAssessmentStore(engine)
    contracts = SqlForecastContractStore(engine)
    context_activation_at: datetime | None = None
    if forecast_sources is None:
        configured_sources: list[CapitalForecastSource] = []
        context = config.capital.context_forecast
        if context is not None and context.enabled:
            if code_version is None:
                raise ValueError("装配 Context Forecast 必须冻结 code_version")
            if producer_activation_at is None:
                raise ValueError("装配 Context Forecast 必须冻结 producer activation")
            spec_by_key = {
                item.instrument.key: item for item in config.capital.execution_specs
            }
            reference_by_key = {
                item.key: item
                for item in config.capital.forecast_reference_instruments
            }
            forecast_instruments = {
                **{key: spec.instrument for key, spec in spec_by_key.items()},
                **reference_by_key,
            }
            perpetual_by_key = {
                item.key: item for item in config.market_data.perpetual_instruments
            }
            cross_venue_symbols = (
                {
                    item.symbol
                    for item in config.market_data.cross_venue_spot.products
                }
                if config.market_data.cross_venue_spot is not None
                else set()
            )
            runtimes: list[ContextForecastRuntimeTarget] = []
            behaviors: list[ContextForecastTargetStateBehavior] = []
            activation_times: list[datetime] = []
            for target_policy in context.targets:
                instrument = forecast_instruments[
                    target_policy.reference_instrument_key
                ]
                perpetual = (
                    perpetual_by_key.get(
                        target_policy.derivative_evidence_instrument_key
                    )
                    if target_policy.derivative_evidence_instrument_key is not None
                    else None
                )
                cross_venue_enabled = instrument.symbol in cross_venue_symbols
                behavior = ContextForecastTargetStateBehavior(
                    feature_policy=config.feature,
                    reference_instrument=instrument,
                    derivative_evidence_instrument=perpetual,
                    interval=config.market_data.interval,
                    bar_window=config.market_data.bar_window,
                    funding_lookback_hours=(
                        config.market_data.funding_history_lookback_hours
                    ),
                    maximum_quote_skew_seconds=(
                        config.market_data.maximum_cross_market_quote_skew_seconds
                    ),
                    cross_venue_spot_version=(
                        config.market_data.cross_venue_spot.version
                        if cross_venue_enabled
                        else None
                    ),
                    cross_venue_spot_venues=(
                        tuple(sorted(SpotVenue, key=lambda item: item.value))
                        if cross_venue_enabled
                        else ()
                    ),
                    maximum_cross_venue_spot_age_seconds=(
                        config.market_data.cross_venue_spot.maximum_age_seconds
                        if cross_venue_enabled
                        and config.market_data.cross_venue_spot is not None
                        else 30
                    ),
                )
                contract = context_forecast_contract(
                    policy=context,
                    target_policy=target_policy,
                    instrument=instrument,
                    cost_semantics_version=(
                        config.capital.decision.cost_model_version
                    ),
                )
                binding = ForecastProducerBinding.create(
                    contract_id=contract.contract_id,
                    producer_kind=ForecastProducerKind.CONTEXT,
                    producer_id=context.producer_id,
                    producer_behavior_id=context.producer_behavior_id,
                    permission=ForecastPermission.CAPITAL_CANDIDATE,
                    required_feature_keys=target_policy.required_feature_keys,
                )
                contracts.record_contract(contract)
                binding = contracts.resolve_binding(
                    binding,
                    activated_at=producer_activation_at,
                )
                activation_at = contracts.binding_activation_at(binding.binding_id)
                if activation_at is None:
                    raise ValueError("Context Forecast binding 缺少激活时点")
                activation_times.append(activation_at)
                target_states = MarketContextTargetStateProvider(
                    market=market,
                    feature_policy=behavior.feature_policy,
                    reference=behavior.reference_instrument,
                    perpetual=behavior.derivative_evidence_instrument,
                    interval=behavior.interval,
                    bar_window=behavior.bar_window,
                    funding_lookback_hours=behavior.funding_lookback_hours,
                    maximum_quote_skew_seconds=(
                        behavior.maximum_quote_skew_seconds
                    ),
                    cross_venue_spot_venues=behavior.cross_venue_spot_venues,
                    maximum_cross_venue_spot_age_seconds=(
                        behavior.maximum_cross_venue_spot_age_seconds
                    ),
                )
                runtimes.append(
                    ContextForecastRuntimeTarget(
                        policy=target_policy,
                        contract=contract,
                        binding=binding,
                        instrument=instrument,
                        target_states=target_states,
                    )
                )
                behaviors.append(behavior)
            context_activation_at = max(activation_times)
            frozen_runtimes = tuple(runtimes)
            frozen_behaviors = tuple(behaviors)
            frozen_contracts = tuple(item.contract for item in frozen_runtimes)
            program = PortfolioContextForecastProducer(
                policy=context,
                targets=frozen_runtimes,
                market=market,
                contexts=world_models,
                contracts=contracts,
                forecasts=forecasts,
                analysis_scope=config.assessment.mandate.analysis_scope,
                activated_at=context_activation_at,
                preflight=(
                    None
                    if context_forecast_preflight_factory is None
                    else context_forecast_preflight_factory(frozen_contracts)
                ),
                analyst=assemble_codex_context_forecast_analyst(
                    config,
                    policy=context,
                    contracts=frozen_contracts,
                    target_state_behaviors=frozen_behaviors,
                    code_version=code_version,
                    leases=SqlAccountLeaseStore(engine),
                    audit=SqlCodexAuditStore(engine),
                ),
            )
            authorization_by_family = {
                item.outcome_family_id: item
                for item in config.capital.candidate_capital_authorizations
            }
            for runtime in frozen_runtimes:
                payoff_policy = runtime.policy.product_payoffs
                product_payoffs = None
                if payoff_policy is not None:
                    payoff_specs = tuple(
                        spec_by_key[key] for key in payoff_policy.instrument_keys
                    )
                    product_payoffs = ContextProductPayoffProjector(
                        policy=payoff_policy,
                        contract=runtime.contract,
                        market=market,
                        target_states=runtime.target_states,
                        instruments=tuple(
                            item.instrument for item in payoff_specs
                        ),
                        execution_specs=payoff_specs,
                        risk=config.capital.sleeve_risk,
                        maximum_quote_age_seconds=(
                            context.maximum_quote_age_seconds
                        ),
                        store=SqlProductPayoffProjectionStore(engine),
                    )
                family = runtime.contract.outcome_family_id
                configured_sources.append(
                    CapitalForecastSource(
                        contract=runtime.contract,
                        binding=runtime.binding,
                        producer=program.view(family),
                        risk_template=config.capital.sleeve_risk,
                        capital_authorization=authorization_by_family[family],
                        product_payoffs=product_payoffs,
                    )
                )
        forecast_sources = tuple(configured_sources)
    portfolio = SqlPortfolioStore(engine)
    performance = SqlPortfolioPerformanceStore(engine)
    risks = SqlPortfolioRiskStore(engine)
    plans = SqlTradePlanStore(engine)
    groups = SqlExecutionGroupStore(engine)
    observations = SqlProductOrderObservationStore(engine)
    account_projection = ProductAccountProjectionService(
        projector=ProductAccountProjector(
            portfolio_id=config.capital.decision.portfolio_id,
            settlement_asset=config.capital.settlement_asset,
            initial_cash=initial_cash,
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
        capital_policy=config.capital,
        capital_behavior_id=config.capital.version,
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
        context_activation_at=context_activation_at,
    )
