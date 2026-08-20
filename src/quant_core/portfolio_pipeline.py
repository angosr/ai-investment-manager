from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, model_validator

from quant_core.asset_management import PortfolioTarget
from quant_core.domain import AccountSnapshot, FrozenModel, MarketSnapshot, RiskOutcome
from quant_core.portfolio_decision import PortfolioAssetInput, PortfolioDecisionEngine
from quant_core.portfolio_risk import (
    PortfolioRiskDecision,
    PortfolioRiskEngine,
    ProtectiveStop,
)
from quant_core.trade_planner import MarketExecutionSpec, TradePlan, TradePlanner


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
    """Pure orchestration only; every economic and safety decision stays downstream-owned."""

    def __init__(
        self,
        decision: PortfolioDecisionEngine,
        risk: PortfolioRiskEngine,
        planner: TradePlanner,
    ) -> None:
        self._decision = decision
        self._risk = risk
        self._planner = planner

    def run(
        self,
        *,
        cycle_id: str,
        as_of: datetime,
        reference_equity: Decimal,
        assets: tuple[PortfolioAssetInput, ...],
        account: AccountSnapshot,
        markets: tuple[MarketSnapshot, ...],
        protective_stops: tuple[ProtectiveStop, ...],
        execution_specs: tuple[MarketExecutionSpec, ...],
    ) -> PortfolioPipelineResult:
        if reference_equity <= 0:
            raise ValueError("PortfolioPipeline reference_equity 必须为正数")
        if account.cycle_id != cycle_id or any(
            item.cycle_id != cycle_id for item in markets
        ):
            raise ValueError("PortfolioPipeline 冻结输入 cycle_id 不一致")
        target = self._decision.decide(
            cycle_id=cycle_id,
            as_of=as_of,
            reference_equity=reference_equity,
            assets=assets,
        )
        if target is None:
            return PortfolioPipelineResult(
                cycle_id=cycle_id,
                outcome=PortfolioPipelineOutcome.NO_CHANGE,
            )
        risk_decision = self._risk.evaluate(
            target=target,
            account=account,
            markets=markets,
            protective_stops=protective_stops,
            as_of=as_of,
        )
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
            markets=markets,
            specs=execution_specs,
            as_of=as_of,
        )
        return PortfolioPipelineResult(
            cycle_id=cycle_id,
            outcome=PortfolioPipelineOutcome.PLANNED,
            target=target,
            risk_decision=risk_decision,
            trade_plan=trade_plan,
        )
