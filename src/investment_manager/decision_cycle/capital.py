"""Low-frequency product capital application assembled from the authoritative domains."""

from __future__ import annotations

import logging
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
    assemble_codex_context_forecast_analyst,
)
from investment_manager.forecast.context.producer import (
    ContextForecastProducer,
    ForecastProductionResult,
    MarketContextTargetStateProvider,
    context_spot_forecast_contract,
)
from investment_manager.forecast.context.repository import SqlContextAssessmentStore
from investment_manager.forecast.contract_repository import SqlForecastContractStore
from investment_manager.forecast.contracts import (
    ForecastContract,
    ForecastNoEstimate,
    ForecastPermission,
    ForecastProducerBinding,
    ForecastProducerKind,
)
from investment_manager.forecast.models import ContextAssessment
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.forecast.results import Forecast
from investment_manager.kernel.errors import PointInTimeInputUnavailable
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.market.models import ExecutableQuote, InstrumentProduct
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.portfolio.decision import (
    ForecastExternalValidity,
    PortfolioDecisionEngine,
    PortfolioSleeveInput,
)
from investment_manager.portfolio.models import (
    CandidateCapitalAuthorization,
    CapitalCycleOutcome,
    CapitalCycleRecord,
    PortfolioAccountSnapshot,
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
from investment_manager.state.decision.packet import DecisionPacket

logger = logging.getLogger(__name__)


class CapitalForecastProducer(Protocol):
    def produce(self, *, as_of: datetime) -> ForecastProductionResult: ...

    def record_deadline_missed(
        self,
        *,
        as_of: datetime,
        completed_at: datetime,
    ) -> ForecastProductionResult: ...

    def recover_deadline_missed(
        self,
        *,
        before_as_of: datetime,
        completed_at: datetime,
    ) -> tuple[ForecastNoEstimate, ...]: ...


class WorldModelHistory(Protocol):
    def latest_before(
        self,
        *,
        analysis_scope: str,
        as_of: datetime,
    ) -> ContextAssessment | None: ...

    def assessment(self, assessment_id: str) -> ContextAssessment | None: ...

    def packet_for_assessment(self, assessment_id: str) -> DecisionPacket | None: ...


@dataclass(frozen=True, slots=True)
class CapitalForecastSource:
    """One contract-bound producer and its risk/permission envelope."""

    contract: ForecastContract
    binding: ForecastProducerBinding
    producer: CapitalForecastProducer
    risk_template: SleeveRiskTemplate
    capital_authorization: CandidateCapitalAuthorization
    world_model_scope: str | None = None

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
        requires_current_world_model = (
            "WORLD_MODEL_CURRENT" in self.contract.validity_conditions
        )
        if requires_current_world_model != (self.world_model_scope is not None):
            raise ValueError("Capital Forecast source 的 WorldModel 有效性检查未完整装配")


def forecast_external_validity(
    *,
    world_models: WorldModelHistory,
    world_model_scope: str | None,
    forecast_world_model_id: str | None,
    as_of: datetime,
) -> ForecastExternalValidity | None:
    """Resolve whether the Forecast's causal WorldModel remains current.

    Snapshot identity is deliberately not the criterion.  A later assessment may
    refresh every causal mechanism with new evidence while preserving the exact
    structure the Forecast consumed.  The Forecast becomes stale only when that
    structure changes, or when its immutable lineage cannot be proven.
    """

    if world_model_scope is None:
        return None
    if forecast_world_model_id is None:
        raise ValueError("要求 WorldModel 当前有效的 Forecast 缺少认知身份")
    latest = world_models.latest_before(
        analysis_scope=world_model_scope,
        as_of=as_of,
    )
    evidence_refs = tuple(
        sorted(
            {
                forecast_world_model_id,
                *((latest.assessment_id,) if latest is not None else ()),
            }
        )
    )

    def invalid(reason_code: str) -> ForecastExternalValidity:
        return ForecastExternalValidity(
            checked_at=as_of,
            current=False,
            reason_codes=(reason_code,),
            evidence_refs=evidence_refs,
        )

    if latest is None:
        return invalid("FORECAST_WORLD_MODEL_UNAVAILABLE")
    if latest.assessment_id == forecast_world_model_id:
        return ForecastExternalValidity(
            checked_at=as_of,
            current=True,
            evidence_refs=evidence_refs,
        )
    source = world_models.assessment(forecast_world_model_id)
    if (
        source is None
        or source.analysis_scope != world_model_scope
        or source.available_at > as_of
    ):
        return invalid("FORECAST_WORLD_MODEL_LINEAGE_UNAVAILABLE")

    # Walk the immutable packet lineage backwards, then apply each explicit
    # one-to-one mechanism disposition forwards.  New or retired mechanisms are
    # structural changes; refreshed claims/evidence under continuity are not.
    transitions: list[ContextAssessment] = []
    cursor = latest
    visited = {cursor.assessment_id}
    while cursor.assessment_id != source.assessment_id:
        packet = world_models.packet_for_assessment(cursor.assessment_id)
        previous_context = None if packet is None else packet.previous_context
        previous_id = (
            None if previous_context is None else previous_context.assessment_id
        )
        if previous_id is None or previous_id in visited:
            return invalid("FORECAST_WORLD_MODEL_LINEAGE_UNAVAILABLE")
        previous = world_models.assessment(previous_id)
        if (
            previous is None
            or previous.analysis_scope != world_model_scope
            or previous.available_at >= cursor.available_at
        ):
            return invalid("FORECAST_WORLD_MODEL_LINEAGE_UNAVAILABLE")
        transitions.append(cursor)
        visited.add(previous_id)
        cursor = previous

    active_ids = {item.mechanism_id for item in source.mechanisms}
    for assessment in reversed(transitions):
        continuation = {
            item.continuity_ref: item.mechanism_id
            for item in assessment.mechanisms
            if item.continuity_ref is not None
        }
        retired = {
            item.previous_mechanism_id for item in assessment.retired_mechanisms
        }
        continued_ids = active_ids.intersection(continuation)
        unresolved_ids = active_ids - continued_ids - retired
        descendant_ids = {continuation[item] for item in continued_ids}
        current_ids = {item.mechanism_id for item in assessment.mechanisms}
        if (
            unresolved_ids
            or active_ids.intersection(retired)
            or current_ids != descendant_ids
        ):
            return invalid("FORECAST_WORLD_MODEL_CAUSAL_STRUCTURE_CHANGED")
        active_ids = descendant_ids

    return ForecastExternalValidity(
        checked_at=as_of,
        current=True,
        evidence_refs=evidence_refs,
    )


@dataclass(frozen=True, slots=True)
class CapitalTriggerConsumer:
    """Own capital on one coordinator and create idempotent Context slots."""

    capital: CapitalCycleService
    context_cadence_minutes: int | None = None
    context_completion_deadline_seconds: int | None = None
    owner_symbol: str | None = None
    context_activation_at: datetime | None = None

    def __post_init__(self) -> None:
        if (self.context_cadence_minutes is None) != (
            self.context_completion_deadline_seconds is None
        ):
            raise ValueError("Context cadence 与完成截止秒数必须同时配置")
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
        if self.context_cadence_minutes is not None and any(
            item.trigger_type
            in {
                AnalysisTriggerType.HEARTBEAT,
                AnalysisTriggerType.FORECAST_SLOT_DUE,
            }
            for item in batch.triggers
        ):
            cadence_seconds = self.context_cadence_minutes * 60
            slot_at = datetime.fromtimestamp(
                int(batch.created_at.timestamp()) // cadence_seconds * cadence_seconds,
                tz=UTC,
            )
            if (
                self.context_activation_at is not None
                and slot_at < self.context_activation_at
            ):
                return self.capital.review(batch)
            assert self.context_completion_deadline_seconds is not None
            cadence_cause_id = stable_id(
                "context_forecast_cadence",
                self.capital.portfolio_id,
                cadence_seconds,
                slot_at.isoformat(),
            )
            self.capital.recover_missed_forecasts(
                before_slot_at=slot_at,
                completed_at=batch.created_at,
            )
            # A Forecast belongs to the cadence slot, while account/risk review
            # belongs to every heartbeat.  Once the slot is complete, its later
            # heartbeats must never reclassify it as missed merely because the
            # completion deadline has elapsed.
            if self.capital.cause_completed(cadence_cause_id):
                return self.capital.review(batch)
            if batch.created_at > slot_at + timedelta(
                seconds=self.context_completion_deadline_seconds
            ):
                self.capital.record_missed_forecast(
                    slot_at=slot_at,
                    completed_at=batch.created_at,
                )
                return self.capital.review(batch)
            # The first heartbeat in a cadence slot creates its Forecast.  Later
            # heartbeats must still refresh the account and protect holdings;
            # returning the old cadence result would leave account facts stale
            # for the entire forecast horizon.
            if self.capital.cause_completed(cadence_cause_id):
                return self.capital.review(batch)
            return self.capital.produce(
                as_of=slot_at,
                cause_id=cadence_cause_id,
                symbol=batch.symbol,
                trigger_types=("FORECAST_CADENCE",),
            )
        return self.capital.review(batch)


class CapitalCycleService:
    """Run one idempotent point-in-time capital decision and persistent execution."""

    def __init__(
        self,
        *,
        capital_policy: CapitalPolicy,
        pipeline_version: str,
        market: SqlMarketDataStore,
        forecasts: SqlForecastStore,
        world_models: SqlContextAssessmentStore,
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
        self._pipeline_version = pipeline_version
        self._market = market
        self._forecasts = forecasts
        self._world_models = world_models
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
            require_utc(context_activation_at)
            if context_activation_at is not None
            else None
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
                    self._pipeline_version,
                    cause_id,
                )
            )
            is not None
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
                self._pipeline_version,
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
        cause_id: str | None = None,
        trigger_batch_id: str | None = None,
        symbol: str = "SYSTEM",
        trigger_types: tuple[str, ...] = (),
    ) -> PortfolioPipelineResult | TradePlanExecutionResult:
        requested_at = require_utc(as_of)
        evaluation_cause_id = cause_id or stable_id(
            "capital_manual_evaluation",
            self._policy.decision.portfolio_id,
            self._pipeline_version,
            requested_at.isoformat(),
        )
        prior_record = self._cycle_records.get(
            stable_id(
                "capital_cycle_record",
                self._policy.decision.portfolio_id,
                self._pipeline_version,
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
    ) -> tuple[ForecastProductionResult, ...]:
        """Ensure a late slot has a terminal result without rewriting an existing one."""

        slot = require_utc(slot_at)
        completed = require_utc(completed_at)
        results = tuple(
            source.producer.record_deadline_missed(
                as_of=slot,
                completed_at=completed,
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
                loaded_authorization = self._risks.execution_authorization(
                    plan.approved_target_id
                )
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
            execution_authorization_id = None
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
            execution_authorization_id = None
        self._cycle_records.record(
            CapitalCycleRecord.create(
                portfolio_id=self._policy.decision.portfolio_id,
                pipeline_id=self._pipeline_version,
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
            sleeve_id = SleeveTarget.identity_for(
                portfolio_id=self._policy.decision.portfolio_id,
                forecast_family=forecast.outcome_family_id,
                forecast_target_id=forecast.target.target_id,
            )
            candidate = PortfolioSleeveInput(
                sleeve_id=sleeve_id,
                forecast=forecast,
                capital_authorization=source.capital_authorization,
                external_validity=forecast_external_validity(
                    world_models=self._world_models,
                    world_model_scope=source.world_model_scope,
                    forecast_world_model_id=forecast.world_model_id,
                    as_of=as_of,
                ),
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
                capital_authorization=source.capital_authorization,
                external_validity=forecast_external_validity(
                    world_models=self._world_models,
                    world_model_scope=source.world_model_scope,
                    forecast_world_model_id=forecast.world_model_id,
                    as_of=as_of,
                ),
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
            portfolio_id=self._policy.decision.portfolio_id,
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
            self._policy.version,
            self._policy.decision.portfolio_id,
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
                    capital_authorization=source.capital_authorization,
                    external_validity=forecast_external_validity(
                        world_models=self._world_models,
                        world_model_scope=source.world_model_scope,
                        forecast_world_model_id=forecast.world_model_id,
                        as_of=as_of,
                    ),
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
        cycle_id: str,
        as_of: datetime,
        quotes: tuple[ExecutableQuote, ...],
    ) -> PortfolioAccountSnapshot:
        portfolio_id = self._policy.decision.portfolio_id
        with self._portfolio.account_projection_lock(portfolio_id=portfolio_id):
            account = self._portfolio.account_for_cycle(
                cycle_id=cycle_id,
                portfolio_id=portfolio_id,
            )
            if account is None:
                head = self._portfolio.head_account(portfolio_id=portfolio_id)
                if head is not None and head.as_of > as_of:
                    raise ValueError("Capital account 不允许倒序投影")
                if head is not None and head.as_of == as_of:
                    account = head
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
        instruments = tuple(item.instrument for item in self._policy.execution_specs)
        values = []
        for instrument in instruments:
            if instrument.product == InstrumentProduct.SPOT:
                observed = self._market.latest_spot_quote(
                    instrument=instrument,
                    evaluation_at=as_of,
                    visible_at=as_of,
                )
                observed_at = None if observed is None else observed.observed_at
            else:
                observed = self._market.latest_perpetual_quote(
                    instrument=instrument,
                    evaluation_at=as_of,
                    visible_at=as_of,
                )
                observed_at = None if observed is None else observed.exchange_time
            if observed is None or observed_at is None:
                raise PointInTimeInputUnavailable(
                    f"Capital 缺少 {instrument.key} 可成交报价"
                )
            values.append(
                ExecutableQuote(
                    source_quote_id=observed.quote_id,
                    instrument=instrument,
                    as_of=as_of,
                    observed_at=observed_at,
                    bid=observed.bid,
                    bid_quantity=observed.bid_quantity,
                    ask=observed.ask,
                    ask_quantity=observed.ask_quantity,
                    source=observed.source,
                )
            )
        observed_times = tuple(item.observed_at for item in values)
        if observed_times and (
            max(observed_times) - min(observed_times)
        ).total_seconds() > self._policy.risk.maximum_quote_skew_seconds:
            raise PointInTimeInputUnavailable("Capital 多产品可成交报价时间偏差过大")
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
    venue: ProductOrderVenue,
    initial_cash: Decimal,
    forecast_sources: tuple[CapitalForecastSource, ...] | None = None,
    code_version: str | None = None,
    producer_activation_at: datetime | None = None,
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
            binding = ForecastProducerBinding.create(
                contract_id=contract.contract_id,
                producer_kind=ForecastProducerKind.CONTEXT,
                producer_id=context.producer_id,
                producer_behavior_id=context.producer_behavior_id,
                permission=ForecastPermission.CAPITAL_CANDIDATE,
                required_feature_keys=context.required_feature_keys,
            )
            authorization = next(
                item
                for item in config.capital.candidate_capital_authorizations
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
            contracts.record_contract(contract)
            binding = contracts.resolve_binding(
                binding,
                activated_at=producer_activation_at,
            )
            context_activation_at = contracts.binding_activation_at(binding.binding_id)
            configured_sources.append(
                CapitalForecastSource(
                    contract=contract,
                    binding=binding,
                    producer=ContextForecastProducer(
                        policy=context,
                        contract=contract,
                        binding=binding,
                        market=market,
                        contexts=world_models,
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
                        activated_at=context_activation_at,
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
                    capital_authorization=authorization,
                    world_model_scope=config.assessment.mandate.analysis_scope,
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
        pipeline_version=config.pipeline.version,
        market=market,
        forecasts=forecasts,
        world_models=world_models,
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
