from decimal import Decimal

from pydantic import Field, model_validator

from investment_manager.execution.planning.planner import (
    InstrumentExecutionSpec,
    TradePlannerPolicy,
)
from investment_manager.forecast.contracts import (
    ForecastBenchmarkProbability,
    ForecastOutcomeBucket,
)
from investment_manager.kernel.configuration import StrictConfig
from investment_manager.portfolio.decision import PortfolioDecisionPolicy
from investment_manager.portfolio.models import CandidateCapitalAuthorization
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


class ContextForecastPolicy(StrictConfig):
    """One pre-registered Context forecast question; no portfolio discretion."""

    version: str
    enabled: bool = False
    producer_id: str = Field(min_length=1)
    producer_behavior_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome_family_id: str = Field(min_length=1)
    contract_version: str = Field(min_length=1)
    target_instrument_key: str = Field(min_length=1)
    derivative_evidence_instrument_key: str | None = Field(default=None, min_length=1)
    horizon_minutes: int = Field(gt=0, le=43_200)
    cadence_minutes: int = Field(gt=0, le=43_200)
    material_event_slots_enabled: bool = False
    material_event_slot_policy_version: str | None = Field(default=None, min_length=1)
    validity_minutes: int = Field(gt=0, le=1_440)
    completion_deadline_seconds: int = Field(gt=0)
    minimum_remaining_horizon_minutes: int = Field(gt=0)
    maximum_quote_age_seconds: int = Field(gt=0)
    maximum_reanchor_move_bps: Decimal = Field(gt=0)
    required_feature_keys: tuple[str, ...] = ()
    outcome_buckets: tuple[ForecastOutcomeBucket, ...] = Field(min_length=3)
    forecast_benchmark: tuple[ForecastBenchmarkProbability, ...] = Field(min_length=3)

    @model_validator(mode="after")
    def feature_keys_are_canonical(self):
        if tuple(sorted(set(self.required_feature_keys))) != self.required_feature_keys:
            raise ValueError("Context Forecast 必需特征必须唯一且排序")
        if self.cadence_minutes > self.horizon_minutes:
            raise ValueError("Context Forecast cadence 不能长于预测周期")
        if self.material_event_slots_enabled != (
            self.material_event_slot_policy_version is not None
        ):
            raise ValueError("Context Forecast 事件槽启用状态与政策版本必须同时配置")
        return self


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
    context_forecast: ContextForecastPolicy | None = None
    candidate_capital_authorizations: tuple[CandidateCapitalAuthorization, ...] = ()

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
            (
                item.producer_id,
                item.producer_behavior_id,
                item.outcome_family_id,
            )
            for item in self.candidate_capital_authorizations
        )
        if len(identities) > 1:
            raise ValueError("Capital 同时只允许一个实验候选")
        if len(set(identities)) != len(identities):
            raise ValueError("Capital candidate authorization 不得重复")
        context = self.context_forecast
        if context is not None and context.enabled:
            matching = tuple(
                item
                for item in self.candidate_capital_authorizations
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
            if len(matching) != 1:
                raise ValueError("启用 Context Forecast 必须绑定唯一资本授权")
        return self
