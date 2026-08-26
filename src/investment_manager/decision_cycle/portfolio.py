from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from investment_manager.execution.group.accounting import ProductAccountProjectionService
from investment_manager.execution.group.engine import ExecutionGroupEngine
from investment_manager.execution.group.models import ExecutionGroup
from investment_manager.execution.group.repository import ExecutionGroupStore
from investment_manager.execution.planning.planner import (
    InstrumentExecutionSpec,
    TradePlan,
    TradePlanner,
)
from investment_manager.execution.planning.repository import TradePlanStore
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.market.models import ExecutableQuote
from investment_manager.portfolio.decision import (
    PortfolioDecisionEngine,
    PortfolioSleeveInput,
)
from investment_manager.portfolio.models import (
    PortfolioAccountSnapshot,
    PortfolioTarget,
)
from investment_manager.portfolio.repository import PortfolioStore
from investment_manager.risk.models import RiskOutcome
from investment_manager.risk.portfolio import (
    HoldingRiskOutcome,
    PortfolioHoldingRiskReview,
    PortfolioRiskDecision,
    PortfolioRiskEngine,
    SleeveRiskProfile,
)
from investment_manager.risk.repository import PortfolioRiskStore


class PortfolioPipelineOutcome(StrEnum):
    NO_CHANGE = "NO_CHANGE"
    RISK_REJECTED = "RISK_REJECTED"
    PLANNED = "PLANNED"


class PortfolioPipelineResult(FrozenModel):
    cycle_id: str = Field(min_length=1)
    outcome: PortfolioPipelineOutcome
    target: PortfolioTarget | None = None
    risk_decision: PortfolioRiskDecision | None = None
    holding_risk_review: PortfolioHoldingRiskReview | None = None
    trade_plan: TradePlan | None = None

    @model_validator(mode="after")
    def outcome_must_match_stage_outputs(self):
        investment = self.target is not None or self.risk_decision is not None
        protection = self.holding_risk_review is not None
        if investment and protection:
            raise ValueError("投资决策与独立保护复核不得混入同一结果")
        if self.outcome == PortfolioPipelineOutcome.NO_CHANGE:
            if investment or self.trade_plan is not None:
                raise ValueError("NO_CHANGE 不得携带投资目标或 TradePlan")
        elif self.outcome == PortfolioPipelineOutcome.RISK_REJECTED:
            if self.target is None or self.risk_decision is None or self.trade_plan is not None:
                raise ValueError("RISK_REJECTED 必须绑定 Target 与 RiskDecision")
        elif self.outcome == PortfolioPipelineOutcome.PLANNED:
            investment_planned = (
                self.target is not None
                and self.risk_decision is not None
                and self.trade_plan is not None
            )
            protection_planned = (
                self.holding_risk_review is not None
                and self.holding_risk_review.reduction_authorization is not None
                and self.trade_plan is not None
            )
            if investment_planned == protection_planned:
                raise ValueError("PLANNED 必须且只能来自投资授权或只减险授权")
        return self


class PortfolioDecisionPipeline:
    """Coordinate one frozen Portfolio → Risk → grouped TradePlan decision."""

    def __init__(
        self,
        decision: PortfolioDecisionEngine,
        risk: PortfolioRiskEngine,
        planner: TradePlanner,
        portfolio_store: PortfolioStore,
        risk_store: PortfolioRiskStore,
        plan_store: TradePlanStore,
    ) -> None:
        self._decision = decision
        self._risk = risk
        self._planner = planner
        self._portfolio_store = portfolio_store
        self._risk_store = risk_store
        self._plan_store = plan_store

    def run(
        self,
        *,
        cycle_id: str,
        as_of: datetime,
        sleeves: tuple[PortfolioSleeveInput, ...],
        account: PortfolioAccountSnapshot,
        quotes: tuple[ExecutableQuote, ...],
        risk_profiles: tuple[SleeveRiskProfile, ...],
        execution_specs: tuple[InstrumentExecutionSpec, ...],
        decision_valid_until: datetime | None = None,
    ) -> PortfolioPipelineResult:
        as_of = require_utc(as_of)
        self._require_frozen_inputs(
            cycle_id=cycle_id,
            sleeves=sleeves,
            account=account,
            quotes=quotes,
            as_of=as_of,
        )
        self._portfolio_store.record_account(account)
        target = self._decision.decide(
            cycle_id=cycle_id,
            as_of=as_of,
            account=account,
            sleeves=sleeves,
            quotes=quotes,
            execution_specs=execution_specs,
            decision_valid_until=decision_valid_until,
        )
        if target is None:
            return PortfolioPipelineResult(
                cycle_id=cycle_id,
                outcome=PortfolioPipelineOutcome.NO_CHANGE,
            )
        self._portfolio_store.record_target(target)
        target_quotes = target.quotes
        risk_decision = self._risk.evaluate(
            target=target,
            account=account,
            quotes=target_quotes,
            risk_profiles=self._profiles_for_target(
                target=target,
                risk_profiles=risk_profiles,
            ),
            as_of=as_of,
        )
        self._risk_store.record(risk_decision)
        if risk_decision.outcome != RiskOutcome.APPROVED or risk_decision.approved_target is None:
            return PortfolioPipelineResult(
                cycle_id=cycle_id,
                outcome=PortfolioPipelineOutcome.RISK_REJECTED,
                target=target,
                risk_decision=risk_decision,
            )
        trade_plan = self._planner.plan(
            approved=risk_decision.approved_target,
            account=account,
            quotes=self._quotes_for_target(
                target=risk_decision.approved_target,
                quotes=target_quotes,
            ),
            specs=execution_specs,
            as_of=as_of,
        )
        self._plan_store.record(trade_plan)
        return PortfolioPipelineResult(
            cycle_id=cycle_id,
            outcome=PortfolioPipelineOutcome.PLANNED,
            target=target,
            risk_decision=risk_decision,
            trade_plan=trade_plan,
        )

    def protect(
        self,
        *,
        cycle_id: str,
        as_of: datetime,
        account: PortfolioAccountSnapshot,
        quotes: tuple[ExecutableQuote, ...],
        risk_profiles: tuple[SleeveRiskProfile, ...],
        execution_specs: tuple[InstrumentExecutionSpec, ...],
    ) -> PortfolioPipelineResult:
        """Persist one review and let Risk authorize a direct reduce-only plan."""

        review = self._risk.review_holding(
            cycle_id=cycle_id,
            account=account,
            quotes=quotes,
            risk_profiles=risk_profiles,
            as_of=as_of,
        )
        self._risk_store.record_holding_review(review)
        if review.outcome != HoldingRiskOutcome.EXIT:
            return PortfolioPipelineResult(
                cycle_id=cycle_id,
                outcome=PortfolioPipelineOutcome.NO_CHANGE,
                holding_risk_review=review,
            )
        authorization = review.reduction_authorization
        assert authorization is not None
        plan = self._planner.plan(
            approved=authorization,
            account=account,
            quotes=quotes,
            specs=execution_specs,
            as_of=as_of,
        )
        self._plan_store.record(plan)
        return PortfolioPipelineResult(
            cycle_id=cycle_id,
            outcome=PortfolioPipelineOutcome.PLANNED,
            holding_risk_review=review,
            trade_plan=plan,
        )

    @classmethod
    def _require_frozen_inputs(
        cls,
        *,
        cycle_id: str,
        sleeves: tuple[PortfolioSleeveInput, ...],
        account: PortfolioAccountSnapshot,
        quotes: tuple[ExecutableQuote, ...],
        as_of: datetime,
    ) -> None:
        if account.as_of != as_of:
            raise ValueError("PortfolioPipeline 账户 as_of 不一致")
        quote_keys = tuple(item.instrument.key for item in quotes)
        if tuple(sorted(set(quote_keys))) != quote_keys:
            raise ValueError("PortfolioPipeline ExecutableQuote 必须唯一且排序")
        if any(item.as_of != as_of for item in quotes):
            raise ValueError("PortfolioPipeline 行情 as_of 不一致")
        sleeve_ids = tuple(item.sleeve_id for item in sleeves)
        if tuple(sorted(set(sleeve_ids))) != sleeve_ids:
            raise ValueError("PortfolioSleeveInput 必须按 sleeve_id 唯一且排序")

    @staticmethod
    def _quotes_for_target(
        *,
        target: PortfolioTarget,
        quotes: tuple[ExecutableQuote, ...],
    ) -> tuple[ExecutableQuote, ...]:
        required = {
            leg.instrument.key for sleeve in target.sleeves for leg in sleeve.forecast_target.legs
        }
        return tuple(item for item in quotes if item.instrument.key in required)

    @staticmethod
    def _profiles_for_target(
        *,
        target: PortfolioTarget,
        risk_profiles: tuple[SleeveRiskProfile, ...],
    ) -> tuple[SleeveRiskProfile, ...]:
        required = {item.sleeve_id for item in target.sleeves}
        return tuple(item for item in risk_profiles if item.sleeve_id in required)


class TradePlanExecutionResult(FrozenModel):
    plan_id: str = Field(min_length=1)
    groups: tuple[ExecutionGroup, ...]
    account: PortfolioAccountSnapshot

    @model_validator(mode="after")
    def result_must_match_plan_and_account_pending_groups(self):
        group_ids = tuple(item.group_id for item in self.groups)
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("TradePlanExecutionResult groups 不得重复")
        nonterminal = tuple(sorted(item.group_id for item in self.groups if not item.terminal))
        if not set(nonterminal).issubset(self.account.pending_execution_group_ids):
            raise ValueError("执行后账户必须包含本 Plan 的非终态 groups")
        return self


class TradePlanExecutionPipeline:
    """Recover one persisted TradePlan, advance its groups, then project one account fact."""

    def __init__(
        self,
        *,
        plans: TradePlanStore,
        groups: ExecutionGroupStore,
        engine: ExecutionGroupEngine,
        accounts: ProductAccountProjectionService,
        portfolio_store: PortfolioStore,
    ) -> None:
        self._plans = plans
        self._groups = groups
        self._engine = engine
        self._accounts = accounts
        self._portfolio_store = portfolio_store

    def run(
        self,
        *,
        plan_id: str,
        as_of: datetime,
        quotes: tuple[ExecutableQuote, ...],
    ) -> TradePlanExecutionResult:
        as_of = require_utc(as_of)
        plan = self._plans.plan(plan_id)
        if plan is None:
            raise ValueError("TradePlan 不存在")
        groups = []
        for planned in plan.groups:
            group = self._groups.group(planned.group_id)
            if group is None:
                group = self._engine.start(
                    plan=plan,
                    planned=planned,
                    as_of=as_of,
                )
            group = self._engine.run_once(group.group_id, as_of=as_of)
            groups.append(group)
        runtime_groups = tuple(groups)
        projection_cycle_id = stable_id(
            "execution_account",
            plan.cycle_id,
            as_of.isoformat(),
            content_hash(runtime_groups),
        )
        with self._portfolio_store.account_projection_lock(
            portfolio_id=self._accounts.portfolio_id
        ):
            account = self._portfolio_store.account_for_cycle(
                cycle_id=projection_cycle_id,
                portfolio_id=self._accounts.portfolio_id,
            )
            if account is None:
                account = self._accounts.project(
                    cycle_id=projection_cycle_id,
                    as_of=as_of,
                    quotes=quotes,
                )
                self._portfolio_store.record_account(account)
        return TradePlanExecutionResult(
            plan_id=plan.plan_id,
            groups=runtime_groups,
            account=account,
        )

    def recover_pending(self, *, as_of: datetime) -> tuple[ExecutionGroup, ...]:
        """Advance every visible nonterminal group before admitting a new decision."""

        as_of = require_utc(as_of)
        pending = tuple(item for item in self._groups.visible(as_of=as_of) if not item.terminal)
        return tuple(self._engine.run_once(item.group_id, as_of=as_of) for item in pending)
