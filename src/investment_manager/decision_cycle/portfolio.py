from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, model_validator

from investment_manager.execution.planner import (
    InstrumentExecutionSpec,
    TradePlan,
    TradePlanner,
)
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
    sleeve_gross_notional,
)
from investment_manager.risk.models import RiskOutcome
from investment_manager.risk.portfolio import (
    PortfolioRiskDecision,
    PortfolioRiskEngine,
    SleeveRiskProfile,
)


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
        sleeves: tuple[PortfolioSleeveInput, ...],
        account: PortfolioAccountSnapshot,
        quotes: tuple[ExecutableQuote, ...],
        risk_profiles: tuple[SleeveRiskProfile, ...],
        execution_specs: tuple[InstrumentExecutionSpec, ...],
    ) -> PortfolioPipelineResult:
        as_of = require_utc(as_of)
        if reference_equity <= 0:
            raise ValueError("PortfolioPipeline reference_equity 必须为正数")
        self._require_frozen_inputs(
            cycle_id=cycle_id,
            sleeves=sleeves,
            account=account,
            quotes=quotes,
            as_of=as_of,
        )
        target = self._decision.decide(
            cycle_id=cycle_id,
            as_of=as_of,
            reference_equity=reference_equity,
            sleeves=sleeves,
            quotes=quotes,
        )
        if target is None:
            return PortfolioPipelineResult(
                cycle_id=cycle_id,
                outcome=PortfolioPipelineOutcome.NO_CHANGE,
            )
        risk_decision = self._risk.evaluate(
            target=target,
            account=account,
            quotes=quotes,
            risk_profiles=risk_profiles,
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
            quotes=quotes,
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
        quote_by_instrument = {item.instrument.key: item for item in quotes}
        quote_keys = tuple(item.instrument.key for item in quotes)
        if tuple(sorted(set(quote_keys))) != quote_keys:
            raise ValueError("PortfolioPipeline ExecutableQuote 必须唯一且排序")
        if any(item.as_of != as_of for item in quotes):
            raise ValueError("PortfolioPipeline 行情 as_of 不一致")
        sleeve_ids = tuple(item.sleeve_id for item in sleeves)
        if tuple(sorted(set(sleeve_ids))) != sleeve_ids:
            raise ValueError("PortfolioSleeveInput 必须按 sleeve_id 唯一且排序")
        account_by_sleeve = {item.sleeve_id: item for item in account.sleeves}
        missing_positions = tuple(sorted(set(account_by_sleeve) - set(sleeve_ids)))
        if missing_positions:
            raise ValueError(
                "PortfolioPipeline 当前 Sleeve 缺少输入: "
                + ", ".join(missing_positions)
            )
        for item in sleeves:
            current = sleeve_gross_notional(
                account_by_sleeve.get(item.sleeve_id),
                quote_by_instrument=quote_by_instrument,
            )
            if item.current_gross_notional != current:
                raise ValueError(
                    "PortfolioPipeline current_gross_notional 与冻结账户不一致: "
                    + item.sleeve_id
                )
