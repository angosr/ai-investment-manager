from decimal import Decimal
from enum import StrEnum

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
from investment_manager.kernel.types import UnitInterval
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


class EconomicExposure(StrEnum):
    CASH = "CASH"
    NOMINAL_RATES = "NOMINAL_RATES"
    INFLATION_SENSITIVE = "INFLATION_SENSITIVE"
    GLOBAL_EQUITY = "GLOBAL_EQUITY"
    CREDIT = "CREDIT"
    COMMODITY = "COMMODITY"
    FX = "FX"
    CRYPTO_NETWORK = "CRYPTO_NETWORK"


class MandateStatus(StrEnum):
    PROVISIONAL = "PROVISIONAL"
    APPROVED = "APPROVED"


class InvestmentMandatePolicy(StrictConfig):
    """Long-lived total-portfolio objective; it never contains trade signals."""

    version: str = Field(min_length=1)
    status: MandateStatus
    portfolio_id: str = Field(min_length=1)
    base_currency: str = Field(pattern=r"^[A-Z0-9._-]+$")
    objective: str = Field(pattern=r"^REAL_CAPITAL_GROWTH$")
    horizon_years: int = Field(ge=3, le=100)
    minimum_liquidity_fraction: UnitInterval
    maximum_drawdown_fraction: UnitInterval
    maximum_stress_loss_fraction: UnitInterval
    maximum_gross_exposure_fraction: Decimal = Field(gt=0, le=2)
    strategic_exposures: tuple[EconomicExposure, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def objective_and_exposures_are_canonical(self):
        if tuple(sorted(set(self.strategic_exposures))) != self.strategic_exposures:
            raise ValueError("Mandate strategic exposures 必须唯一且排序")
        if EconomicExposure.CASH not in self.strategic_exposures:
            raise ValueError("Mandate 必须把现金视为正式经济暴露")
        return self


class InvestableInstrumentPolicy(StrictConfig):
    instrument_key: str = Field(min_length=1)
    economic_exposure: EconomicExposure
    reference_eligible: bool = False

    @model_validator(mode="after")
    def cash_is_not_a_traded_instrument(self):
        if self.economic_exposure == EconomicExposure.CASH:
            raise ValueError("结算现金由 Mandate 拥有，不得伪装成交易 Instrument")
        return self


class InvestableUniversePolicy(StrictConfig):
    """Map legal products to economic exposures without duplicating product specs."""

    version: str = Field(min_length=1)
    mandate_version: str = Field(min_length=1)
    instruments: tuple[InvestableInstrumentPolicy, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def instruments_are_unique_and_sorted(self):
        keys = tuple(item.instrument_key for item in self.instruments)
        if tuple(sorted(set(keys))) != keys:
            raise ValueError("Investable universe Instrument 必须唯一且排序")
        return self


class ReferenceAllocationPolicy(StrictConfig):
    implementation_key: str = Field(min_length=1)
    target_weight: UnitInterval

    @model_validator(mode="after")
    def allocation_is_material(self):
        if self.target_weight <= 0:
            raise ValueError("Reference allocation 权重必须为正")
        return self


class ReferencePortfolioPolicy(StrictConfig):
    """Unique low-cost total-account benchmark, independent of active views."""

    version: str = Field(min_length=1)
    mandate_version: str = Field(min_length=1)
    universe_version: str = Field(min_length=1)
    allocations: tuple[ReferenceAllocationPolicy, ...] = Field(min_length=2)
    rebalance_band_fraction: UnitInterval

    @model_validator(mode="after")
    def allocations_are_unique_complete_and_sorted(self):
        keys = tuple(item.implementation_key for item in self.allocations)
        if tuple(sorted(set(keys))) != keys:
            raise ValueError("Reference allocations 必须唯一且排序")
        if sum((item.target_weight for item in self.allocations), Decimal("0")) != 1:
            raise ValueError("Reference allocations 权重之和必须为 1")
        if self.rebalance_band_fraction <= 0:
            raise ValueError("Reference Policy 再平衡带必须为正")
        return self


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
    material_event_cadence_merge_seconds: int = Field(default=0, ge=0, le=3_600)
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
        if self.material_event_slots_enabled != (
            self.material_event_cadence_merge_seconds > 0
        ):
            raise ValueError("Context Forecast 事件槽与 cadence 合并窗口必须同时启用")
        if self.material_event_cadence_merge_seconds > min(
            self.completion_deadline_seconds,
            self.cadence_minutes * 30,
        ):
            raise ValueError("Context Forecast 合并窗口必须短于完成期限和半个 cadence")
        return self


class CapitalPolicy(StrictConfig):
    """One explicit assembly contract for the product-qualified capital path."""

    version: str
    enabled: bool = False
    settlement_asset: str = Field(pattern=r"^[A-Z0-9._-]+$")
    mandate: InvestmentMandatePolicy
    investable_universe: InvestableUniversePolicy
    reference_policy: ReferencePortfolioPolicy | None = None
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
        if self.mandate.portfolio_id != self.decision.portfolio_id:
            raise ValueError("Mandate 与 PortfolioDecision 必须属于同一总账户")
        if self.mandate.base_currency != self.settlement_asset:
            raise ValueError("Mandate 记账本位必须等于 Capital settlement asset")
        if self.investable_universe.mandate_version != self.mandate.version:
            raise ValueError("Investable universe 必须绑定当前 Mandate")
        universe_keys = tuple(
            item.instrument_key for item in self.investable_universe.instruments
        )
        if universe_keys != spec_keys:
            raise ValueError("Investable universe 必须唯一覆盖 Capital execution specs")
        if self.risk.maximum_drawdown_fraction > self.mandate.maximum_drawdown_fraction:
            raise ValueError("Capital Risk 回撤上限不得宽于 Mandate")
        if (
            self.risk.maximum_stress_loss_fraction
            > self.mandate.maximum_stress_loss_fraction
        ):
            raise ValueError("Capital Risk 压力损失上限不得宽于 Mandate")
        if (
            self.risk.maximum_gross_exposure_fraction
            > self.mandate.maximum_gross_exposure_fraction
        ):
            raise ValueError("Capital Risk gross 上限不得宽于 Mandate")
        reference = self.reference_policy
        if reference is not None:
            if (
                reference.mandate_version != self.mandate.version
                or reference.universe_version != self.investable_universe.version
            ):
                raise ValueError("Reference Policy 必须绑定当前 Mandate 与可投资域")
            by_key = {
                f"CASH:{self.settlement_asset}": EconomicExposure.CASH,
                **{
                    item.instrument_key: item.economic_exposure
                    for item in self.investable_universe.instruments
                    if item.reference_eligible
                },
            }
            allocation_keys = tuple(
                item.implementation_key for item in reference.allocations
            )
            if not set(allocation_keys).issubset(by_key):
                raise ValueError("Reference Policy 使用了不合格的实现产品")
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
