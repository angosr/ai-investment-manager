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
    trade_plan: TradePlan | None = None

    @model_validator(mode="after")
    def outcome_must_match_stage_outputs(self):
        expected = {
            PortfolioPipelineOutcome.NO_CHANGE: (False, False, False),
            PortfolioPipelineOutcome.RISK_REJECTED: (True, True, False),
            PortfolioPipelineOutcome.PLANNED: (True, True, True),
        }[self.outcome]
        actual = (
            self.target is not None,
            self.risk_decision is not None,
            self.trade_plan is not None,
        )
        if actual != expected:
            raise ValueError("PortfolioPipeline outcome 与阶段输出不一致")
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
        )
        if target is None:
            return PortfolioPipelineResult(
                cycle_id=cycle_id,
                outcome=PortfolioPipelineOutcome.NO_CHANGE,
            )
        self._portfolio_store.record_target(target)
        risk_decision = self._risk.evaluate(
            target=target,
            account=account,
            quotes=quotes,
            risk_profiles=risk_profiles,
            as_of=as_of,
        )
        self._risk_store.record(risk_decision)
        if (
            risk_decision.outcome != RiskOutcome.APPROVED
            or risk_decision.approved_target is None
        ):
            return PortfolioPipelineResult(
                cycle_id=cycle_id,
                outcome=PortfolioPipelineOutcome.RISK_REJECTED,
                target=target,
                risk_decision=risk_decision,
            )
        trade_plan = self._planner.plan(
            approved=risk_decision.approved_target,
            account=account,
            quotes=quotes,
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
        if account.cycle_id != cycle_id or account.as_of != as_of:
            raise ValueError("PortfolioPipeline 账户 cycle_id/as_of 不一致")
        quote_keys = tuple(item.instrument.key for item in quotes)
        if tuple(sorted(set(quote_keys))) != quote_keys:
            raise ValueError("PortfolioPipeline ExecutableQuote 必须唯一且排序")
        if any(item.as_of != as_of for item in quotes):
            raise ValueError("PortfolioPipeline 行情 as_of 不一致")
        sleeve_ids = tuple(item.sleeve_id for item in sleeves)
        if tuple(sorted(set(sleeve_ids))) != sleeve_ids:
            raise ValueError("PortfolioSleeveInput 必须按 sleeve_id 唯一且排序")


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
