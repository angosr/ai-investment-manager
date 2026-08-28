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
from investment_manager.market.models import InstrumentId
from investment_manager.portfolio.decision import PortfolioDecisionPolicy
from investment_manager.portfolio.models import CandidateCapitalAuthorization
from investment_manager.risk.portfolio import PortfolioRiskPolicy


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
    US_EQUITY = "US_EQUITY"
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
    maximum_drawdown_fraction: UnitInterval
    maximum_stress_loss_fraction: UnitInterval
    maximum_gross_exposure_fraction: Decimal = Field(gt=0, le=2)
    allowed_exposures: tuple[EconomicExposure, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def objective_and_exposures_are_canonical(self):
        if tuple(sorted(set(self.allowed_exposures))) != self.allowed_exposures:
            raise ValueError("Mandate allowed exposures 必须唯一且排序")
        if EconomicExposure.CASH not in self.allowed_exposures:
            raise ValueError("Mandate 必须把现金视为正式经济暴露")
        return self


class InvestableInstrumentPolicy(StrictConfig):
    instrument_key: str = Field(min_length=1)
    economic_exposure: EconomicExposure
    reference_candidate: bool = False

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
    target_exposure_fraction: UnitInterval

    @model_validator(mode="after")
    def allocation_is_material(self):
        if self.target_exposure_fraction <= 0:
            raise ValueError("Reference allocation 目标经济暴露必须为正")
        return self


class ReferencePortfolioPolicy(StrictConfig):
    """Unique low-cost total-account benchmark, independent of active views."""

    version: str = Field(min_length=1)
    mandate_version: str = Field(min_length=1)
    universe_version: str = Field(min_length=1)
    selection_artifact_id: str = Field(min_length=1)
    allocations: tuple[ReferenceAllocationPolicy, ...] = Field(min_length=2)
    rebalance_band_fraction: UnitInterval

    @model_validator(mode="after")
    def allocations_are_unique_complete_and_sorted(self):
        keys = tuple(item.implementation_key for item in self.allocations)
        if tuple(sorted(set(keys))) != keys:
            raise ValueError("Reference allocations 必须唯一且排序")
        if (
            sum(
                (item.target_exposure_fraction for item in self.allocations),
                Decimal("0"),
            )
            != 1
        ):
            raise ValueError("Reference allocations 目标经济暴露之和必须为 1")
        if self.rebalance_band_fraction <= 0:
            raise ValueError("Reference Policy 再平衡带必须为正")
        return self


class ProductPayoffPolicy(StrictConfig):
    """One economic Forecast's allowed deterministic product expressions."""

    version: str = Field(min_length=1)
    economic_exposure_id: str = Field(min_length=1)
    instrument_keys: tuple[str, ...] = Field(min_length=1)
    maximum_rule_age_seconds: int = Field(gt=0)

    @model_validator(mode="after")
    def instruments_must_be_unique_and_sorted(self):
        if tuple(sorted(set(self.instrument_keys))) != self.instrument_keys:
            raise ValueError("Product payoff instruments 必须唯一且排序")
        return self


class ContextForecastComparisonPolicy(StrictConfig):
    """Observation-only economic reference for one proxy Forecast target."""

    instrument_key: str = Field(min_length=1)
    reference_price_multiplier: Decimal = Field(gt=0)


class ContextForecastTargetPolicy(StrictConfig):
    """One economic outcome contract inside a shared Context forecast call."""

    outcome_family_id: str = Field(min_length=1)
    contract_version: str = Field(min_length=1)
    reference_instrument_key: str = Field(min_length=1)
    derivative_evidence_instrument_key: str | None = Field(default=None, min_length=1)
    comparison: ContextForecastComparisonPolicy | None = None
    required_feature_keys: tuple[str, ...] = ()
    outcome_buckets: tuple[ForecastOutcomeBucket, ...] = Field(min_length=3)
    forecast_benchmark: tuple[ForecastBenchmarkProbability, ...] = Field(min_length=3)
    product_payoffs: ProductPayoffPolicy | None = None

    @model_validator(mode="after")
    def feature_keys_are_canonical(self):
        if tuple(sorted(set(self.required_feature_keys))) != self.required_feature_keys:
            raise ValueError("Context Forecast 必需特征必须唯一且排序")
        bucket_ids = tuple(item.bucket_id for item in self.outcome_buckets)
        benchmark_ids = tuple(item.bucket_id for item in self.forecast_benchmark)
        if bucket_ids != benchmark_ids:
            raise ValueError("Context Forecast bucket 与 benchmark 必须完整同序")
        return self


class ContextForecastPolicy(StrictConfig):
    """Shared economic Forecast contracts and source-independent slot policy."""

    version: str
    enabled: bool = False
    reasoning_effort: str = Field(
        default="medium",
        pattern=r"^(low|medium|high|xhigh|max|ultra)$",
    )
    horizon_minutes: int = Field(gt=0, le=43_200)
    cadence_minutes: int = Field(gt=0, le=43_200)
    material_event_slots_enabled: bool = False
    material_event_slot_policy_version: str | None = Field(default=None, min_length=1)
    validity_minutes: int = Field(gt=0, le=1_440)
    completion_deadline_seconds: int = Field(gt=0)
    minimum_remaining_horizon_minutes: int = Field(gt=0)
    maximum_quote_age_seconds: int = Field(gt=0)
    maximum_reanchor_move_bps: Decimal = Field(gt=0)
    targets: tuple[ContextForecastTargetPolicy, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def targets_and_sampling_are_canonical(self):
        if self.cadence_minutes > self.horizon_minutes:
            raise ValueError("Context Forecast cadence 不能长于预测周期")
        if self.material_event_slots_enabled != (
            self.material_event_slot_policy_version is not None
        ):
            raise ValueError("Context Forecast 事件槽启用状态与政策版本必须同时配置")
        target_keys = tuple(item.reference_instrument_key for item in self.targets)
        family_ids = tuple(item.outcome_family_id for item in self.targets)
        if tuple(sorted(set(target_keys))) != target_keys:
            raise ValueError("Context Forecast targets 必须按规范参考产品唯一排序")
        if len(set(family_ids)) != len(family_ids):
            raise ValueError("Context Forecast outcome family 不得重复")
        return self


class CapitalPolicy(StrictConfig):
    """One explicit assembly contract for the product-qualified capital path."""

    version: str
    enabled: bool = False
    settlement_asset: str = Field(pattern=r"^[A-Z0-9._-]+$")
    mandate: InvestmentMandatePolicy
    investable_universe: InvestableUniversePolicy
    forecast_reference_instruments: tuple[InstrumentId, ...] = ()
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
        universe_keys = tuple(item.instrument_key for item in self.investable_universe.instruments)
        if universe_keys != spec_keys:
            raise ValueError("Investable universe 必须唯一覆盖 Capital execution specs")
        reference_keys = tuple(item.key for item in self.forecast_reference_instruments)
        if tuple(sorted(set(reference_keys))) != reference_keys:
            raise ValueError("Forecast reference instruments 必须唯一且排序")
        if set(reference_keys) & set(spec_keys):
            raise ValueError("Forecast 只读参考不得重复进入 Capital execution specs")
        disallowed_exposures = {
            item.economic_exposure
            for item in self.investable_universe.instruments
            if item.economic_exposure not in self.mandate.allowed_exposures
        }
        if disallowed_exposures:
            raise ValueError("Investable universe 包含 Mandate 未允许的经济暴露")
        if self.risk.maximum_drawdown_fraction > self.mandate.maximum_drawdown_fraction:
            raise ValueError("Capital Risk 回撤上限不得宽于 Mandate")
        if self.risk.maximum_stress_loss_fraction > self.mandate.maximum_stress_loss_fraction:
            raise ValueError("Capital Risk 压力损失上限不得宽于 Mandate")
        if self.risk.maximum_gross_exposure_fraction > self.mandate.maximum_gross_exposure_fraction:
            raise ValueError("Capital Risk gross 上限不得宽于 Mandate")
        reference = self.reference_policy
        if reference is not None:
            if self.mandate.status != MandateStatus.APPROVED:
                raise ValueError("Reference Policy 只能绑定资产所有者已批准的 Mandate")
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
                    if item.reference_candidate
                },
            }
            allocation_keys = tuple(item.implementation_key for item in reference.allocations)
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
        if len(set(identities)) != len(identities):
            raise ValueError("Capital candidate authorization 不得重复")
        context = self.context_forecast
        if context is not None:
            target_reference_keys = {item.reference_instrument_key for item in context.targets}
            expected_read_only_references = target_reference_keys - set(spec_keys)
            if set(reference_keys) != expected_read_only_references:
                raise ValueError("Forecast 只读参考必须精确覆盖不属于 execution specs 的规范参考")
            exposure_by_key = {
                item.instrument_key: item.economic_exposure
                for item in self.investable_universe.instruments
            }
            for target in context.targets:
                payoffs = target.product_payoffs
                if payoffs is None:
                    continue
                if not set(payoffs.instrument_keys).issubset(spec_keys):
                    raise ValueError("Product payoff instruments 必须属于 execution specs")
                if len({exposure_by_key[key] for key in payoffs.instrument_keys}) != 1:
                    raise ValueError("Product payoff products 必须表达同一经济暴露")
        return self
