from decimal import Decimal

from pydantic import Field, model_validator

from investment_manager.execution.planning.planner import (
    InstrumentExecutionSpec,
    TradePlannerPolicy,
)
from investment_manager.kernel.configuration import StrictConfig
from investment_manager.portfolio.decision import PortfolioDecisionPolicy
from investment_manager.portfolio.models import MockCandidateAuthorization
from investment_manager.risk.portfolio import PortfolioRiskPolicy


class CompositionPolicy(StrictConfig):
    version: str


class FrequencyPolicy(StrictConfig):
    version: str
    cooldown_minutes: int = Field(default=30, ge=0)
    maximum_orders_per_day: int = Field(default=8, gt=0)
    minimum_net_edge_bps: Decimal = Field(default=Decimal("4"), ge=0)
    funding_bps: Decimal = Decimal("0")
    latency_bps: Decimal = Field(default=Decimal("0.5"), ge=0)
    adverse_selection_bps: Decimal = Field(default=Decimal("0.75"), ge=0)
    uncertainty_buffer_bps: Decimal = Field(default=Decimal("1.5"), ge=0)


class SleeveRiskTemplate(StrictConfig):
    version: str
    basis_stress_bps: Decimal = Field(ge=0)
    funding_stress_bps: Decimal = Field(ge=0)
    execution_stress_bps: Decimal = Field(ge=0)
    derivative_initial_margin_fraction: Decimal = Field(gt=0, le=1)


class CapitalPolicy(StrictConfig):
    """One explicit assembly contract for the product-qualified capital path."""

    version: str
    enabled: bool = False
    settlement_asset: str = Field(pattern=r"^[A-Z0-9._-]+$")
    decision: PortfolioDecisionPolicy
    risk: PortfolioRiskPolicy
    planner: TradePlannerPolicy
    execution_specs: tuple[InstrumentExecutionSpec, ...] = Field(min_length=1)
    sleeve_risk: SleeveRiskTemplate
    mock_candidate_authorizations: tuple[MockCandidateAuthorization, ...] = ()

    @model_validator(mode="after")
    def capital_path_has_one_exact_instrument_scope(self):
        spec_keys = tuple(item.instrument.key for item in self.execution_specs)
        if tuple(sorted(set(spec_keys))) != spec_keys:
            raise ValueError("Capital execution_specs 必须按 Instrument 唯一且排序")
        if self.planner.managed_instruments != spec_keys:
            raise ValueError("Capital Planner 与 execution_specs 托管范围必须一致")
        if self.risk.instrument_allowlist != spec_keys:
            raise ValueError("Capital Risk 与 execution_specs 白名单必须一致")
        if self.enabled and not self.decision.enabled:
            raise ValueError("启用 Capital 时 PortfolioDecision 必须启用")
        identities = tuple(
            (item.producer_id, item.producer_version, item.forecast_family)
            for item in self.mock_candidate_authorizations
        )
        if len(identities) > 1:
            raise ValueError("Capital 同时只允许一个 Mock challenger")
        if len(set(identities)) != len(identities):
            raise ValueError("Capital Mock candidate authorization 不得重复")
        return self
