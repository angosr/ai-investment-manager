from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, model_validator

from quant_core.asset_management import PortfolioTarget
from quant_core.domain import (
    AccountSnapshot,
    FrozenModel,
    MarketSnapshot,
    RiskOutcome,
    _require_utc,
)
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
        as_of = _require_utc(as_of)
        if reference_equity <= 0:
            raise ValueError("PortfolioPipeline reference_equity 必须为正数")
        if account.cycle_id != cycle_id or any(
            item.cycle_id != cycle_id for item in markets
        ):
            raise ValueError("PortfolioPipeline 冻结输入 cycle_id 不一致")
        self._require_frozen_inputs(
            assets=assets,
            account=account,
            markets=markets,
            as_of=as_of,
        )
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

    @staticmethod
    def _require_frozen_inputs(
        *,
        assets: tuple[PortfolioAssetInput, ...],
        account: AccountSnapshot,
        markets: tuple[MarketSnapshot, ...],
        as_of: datetime,
    ) -> None:
        if account.as_of != as_of or any(item.as_of != as_of for item in markets):
            raise ValueError("PortfolioPipeline 冻结输入 as_of 不一致")
        market_by_symbol = {item.symbol: item for item in markets}
        if len(market_by_symbol) != len(markets):
            raise ValueError("PortfolioPipeline MarketSnapshot symbol 必须唯一")
        position_by_symbol = {item.symbol: item for item in account.positions}
        if len(position_by_symbol) != len(account.positions):
            raise ValueError("PortfolioPipeline Account position symbol 必须唯一")
        asset_symbols = tuple(item.symbol for item in assets)
        if tuple(sorted(set(asset_symbols))) != asset_symbols:
            raise ValueError("PortfolioAssetInput 必须按 symbol 唯一且排序")
        missing_positions = tuple(sorted(set(position_by_symbol) - set(asset_symbols)))
        if missing_positions:
            raise ValueError(
                "PortfolioPipeline 当前持仓缺少资产输入: "
                + ", ".join(missing_positions)
            )
        for asset in assets:
            market = market_by_symbol.get(asset.symbol)
            if market is None:
                raise ValueError(
                    f"PortfolioPipeline 资产缺少冻结行情: {asset.symbol}"
                )
            position = position_by_symbol.get(asset.symbol)
            expected_notional = (
                position.quantity * market.bid
                if position is not None
                else Decimal("0")
            )
            if asset.current_price != market.last:
                raise ValueError(
                    f"PortfolioPipeline current_price 与冻结行情不一致: {asset.symbol}"
                )
            if asset.current_quote_notional != expected_notional:
                raise ValueError(
                    "PortfolioPipeline current_quote_notional 与冻结账户不一致: "
                    + asset.symbol
                )
