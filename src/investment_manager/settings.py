from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import model_validator

from investment_manager.execution.policy import (
    BinanceTestnetPolicy,
    ShadowSimulationPolicy,
)
from investment_manager.forecast.policy import (
    CodexAccountRegistry,
    CodexRuntimePolicy,
    ContextAssessmentPolicy,
    PipelinePolicy,
)
from investment_manager.governance.policy import (
    DeploymentPolicy,
    DeploymentStage,
    GovernancePolicy,
    OutcomeEvaluationPolicy,
)
from investment_manager.information.aggregated_flows import (
    AGGREGATED_FLOW_FACT_TYPES,
    AGGREGATED_FLOW_RISK_FACTORS_BY_TYPE,
)
from investment_manager.information.official.metrics import (
    OFFICIAL_METRIC_FACT_TYPES,
    OFFICIAL_METRIC_RISK_FACTORS_BY_TYPE,
)
from investment_manager.information.policy import InformationPolicy
from investment_manager.kernel.configuration import StrictConfig
from investment_manager.market.models import InstrumentProduct
from investment_manager.market.policy import FeaturePolicy, MarketDataPolicy
from investment_manager.portfolio.policy import CapitalPolicy
from investment_manager.scheduling.policy import TemporalPolicy, TriggerPolicy
from investment_manager.state.policy import DecisionStatePolicy


class AppConfig(StrictConfig):
    feature: FeaturePolicy
    decision_state: DecisionStatePolicy
    capital: CapitalPolicy
    outcome_evaluation: OutcomeEvaluationPolicy
    governance: GovernancePolicy
    trigger: TriggerPolicy
    temporal: TemporalPolicy
    market_data: MarketDataPolicy
    shadow: ShadowSimulationPolicy
    information: InformationPolicy
    pipeline: PipelinePolicy
    codex_runtime: CodexRuntimePolicy
    assessment: ContextAssessmentPolicy
    codex_accounts: CodexAccountRegistry
    binance_testnet: BinanceTestnetPolicy
    deployment: DeploymentPolicy

    @property
    def analysis_symbols(self) -> tuple[str, ...]:
        """Symbols owned by the active decision cohort, derived from its mandate."""

        return tuple(item.market_symbol for item in self.assessment.mandate.observation_assets)

    @property
    def analysis_trigger_symbols(self) -> tuple[str, ...]:
        """Single portfolio coordination scope; observation assets remain broader."""

        owner = self.assessment.review_trigger_symbol
        return (owner,) if owner is not None else self.analysis_symbols

    @model_validator(mode="after")
    def cross_domain_invariants_hold(self):
        if self.decision_state.analysis_scope != self.assessment.mandate.analysis_scope:
            raise ValueError("DecisionState 与 Assessment mandate scope 必须一致")
        mandate_symbols = tuple(
            item.market_symbol for item in self.assessment.mandate.observation_assets
        )
        if self.assessment.enabled and not set(mandate_symbols).issubset(self.market_data.symbols):
            raise ValueError("Assessment mandate 必须属于 MarketData 观测域")
        perpetual_symbols = tuple(item.symbol for item in self.market_data.perpetual_instruments)
        if self.assessment.enabled and not set(mandate_symbols).issubset(perpetual_symbols):
            raise ValueError("启用 ContextAssessment 时 Perpetual 观测域必须覆盖 Mandate")
        perpetual_by_key = {
            item.key: item for item in self.market_data.perpetual_instruments
        }
        capital_instrument_keys = {
            item.instrument.key for item in self.capital.execution_specs
        }
        if self.assessment.enabled:
            for reference in self.assessment.mandate.observation_references:
                instrument = perpetual_by_key.get(reference.reference_instrument_key)
                if instrument is None:
                    raise ValueError("Assessment 经济参照必须属于 MarketData 观测域")
                if instrument.product != InstrumentProduct.TRADFI_PERPETUAL:
                    raise ValueError("Assessment 经济参照必须具有官方交易日历")
                if instrument.key in capital_instrument_keys:
                    raise ValueError("Assessment 经济参照只能观察，不能拥有执行权限")
        if self.assessment.enabled and (
            self.assessment.review_trigger_symbol not in mandate_symbols
        ):
            raise ValueError("启用 ContextAssessment 时必须指定 Mandate 内的复核协调 symbol")
        mandate_horizons = tuple(
            sorted(
                {
                    horizon
                    for asset in self.assessment.mandate.observation_assets
                    for horizon in asset.horizons_minutes
                }
            )
        )
        if mandate_horizons != self.decision_state.delta_policy.horizons_minutes:
            raise ValueError("Assessment mandate 与 FactDelta 时域必须一致")
        if (
            self.decision_state.packet_policy.maximum_background_fact_distance_seconds
            < max(mandate_horizons) * 60
        ):
            raise ValueError("DecisionPacket 背景事实窗口不得短于最长 Assessment 时域")
        mandate_assets = tuple(item.asset for item in self.assessment.mandate.observation_assets)
        if mandate_assets != self.decision_state.official_fact_policy.affected_assets:
            raise ValueError("OfficialFact projection 与 Assessment 观察资产必须一致")
        execution_by_key = {
            item.instrument.key: item.instrument for item in self.capital.execution_specs
        }
        expected_exposures = tuple(
            sorted(
                (
                    item.economic_exposure.value,
                    execution_by_key[item.instrument_key].base_asset,
                )
                for item in self.capital.investable_universe.instruments
            )
        )
        configured_exposures = tuple(item.key for item in self.assessment.mandate.mandate_exposures)
        if configured_exposures != expected_exposures:
            raise ValueError("Assessment mandate economic exposures 必须精确覆盖 Capital 可投资域")
        required_risk_factors = set(self.assessment.mandate.required_risk_factors)
        configured_risk_factors = {
            *self.decision_state.official_fact_policy.release_risk_factors,
            *self.decision_state.delta_policy.intelligence_risk_factors,
            *self.decision_state.delta_policy.market_risk_factors,
        }
        configured_fact_types = {item.fact_type for item in self.decision_state.delta_policy.rules}
        for fact_type in configured_fact_types & OFFICIAL_METRIC_FACT_TYPES:
            configured_risk_factors.update(OFFICIAL_METRIC_RISK_FACTORS_BY_TYPE[fact_type])
        for fact_type in configured_fact_types & AGGREGATED_FLOW_FACT_TYPES:
            configured_risk_factors.update(AGGREGATED_FLOW_RISK_FACTORS_BY_TYPE[fact_type])
        if not configured_risk_factors.issubset(required_risk_factors):
            raise ValueError("DecisionState 风险因子必须属于 Assessment mandate")
        if self.assessment.enabled and not self.codex_runtime.enabled:
            raise ValueError("启用 ContextAssessment 前必须启用受控 Codex runtime")
        if self.assessment.enabled:
            enabled_account_count = sum(item.enabled for item in self.codex_accounts.accounts)
            account_attempt_count = min(
                enabled_account_count,
                1 + self.codex_runtime.max_account_switches,
            )
            capacity_probe_budget = (
                enabled_account_count * self.codex_runtime.capacity_probe_timeout_seconds
            )
            invocation_budget = account_attempt_count * self.codex_runtime.timeout_seconds
            if self.codex_runtime.lease_ttl_seconds <= self.codex_runtime.timeout_seconds:
                raise ValueError("Codex 账号租约必须长于单账号调用超时")
            if self.temporal.activity_start_to_close_seconds <= (
                capacity_probe_budget + invocation_budget
            ):
                raise ValueError("ContextAssessment activity 时限必须覆盖容量探测和账号故障切换")
            if (
                self.shadow.analysis_deadline_seconds
                < self.temporal.activity_schedule_to_close_seconds
            ):
                raise ValueError(
                    "ContextAssessment 分析截止时间必须覆盖 activity schedule-to-close"
                )
        if self.capital.enabled:
            if self.deployment.stage != DeploymentStage.SHADOW:
                raise ValueError("当前实验候选资本权限只允许 SHADOW")
            if self.capital.settlement_asset != self.binance_testnet.quote_asset:
                raise ValueError("Capital 与行情结算资产必须一致")
            if (
                self.market_data.maximum_cross_market_quote_skew_seconds * 1_000
                < self.market_data.quote_persist_interval_ms
            ):
                raise ValueError("Capital 跨产品报价偏差上限不得短于 Spot 冻结间隔")
            if (
                self.capital.risk.maximum_quote_skew_seconds
                != self.market_data.maximum_cross_market_quote_skew_seconds
            ):
                raise ValueError("Capital 风控与行情的跨产品报价偏差上限必须一致")
        permissions = self.capital.candidate_capital_authorizations
        if permissions and not self.capital.enabled:
            raise ValueError("禁用 Capital 时不得保留 candidate capital authorization")
        if any(
            not symbol.endswith(self.binance_testnet.quote_asset)
            for symbol in self.market_data.symbols
        ):
            raise ValueError("行情 symbols 必须使用配置的统一 quote_asset")
        testnet_market_data = self.market_data.rest_base_url == "https://testnet.binance.vision"
        if self.deployment.stage == DeploymentStage.TESTNET and not testnet_market_data:
            raise ValueError("TESTNET 必须使用 Binance Spot Testnet 行情")
        if self.deployment.stage != DeploymentStage.TESTNET and testnet_market_data:
            raise ValueError("非 TESTNET 阶段不得混用 Binance Spot Testnet 行情")
        return self


def load_config(path: str | Path) -> AppConfig:
    """加载严格配置；小型环境文件可用 ``extends`` 继承同目录基线。"""

    config_path = Path(path).resolve()
    return AppConfig.model_validate(_load_config_mapping(config_path, stack=()))


def load_config_mapping(path: str | Path) -> dict[str, Any]:
    """Load and merge a strict config source for another typed boundary."""

    return _load_config_mapping(Path(path).resolve(), stack=())


def _load_config_mapping(config_path: Path, *, stack: tuple[Path, ...]) -> dict[str, Any]:
    if config_path in stack:
        chain = " -> ".join(str(item) for item in (*stack, config_path))
        raise ValueError(f"配置 extends 存在循环: {chain}")
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise ValueError("配置根节点必须是对象")
    current = dict(raw)
    base_reference = current.pop("extends", None)
    if base_reference is None:
        return current
    if not isinstance(base_reference, str) or not base_reference.strip():
        raise ValueError("配置 extends 必须是非空路径")
    base_path = Path(base_reference)
    if not base_path.is_absolute():
        base_path = config_path.parent / base_path
    base = _load_config_mapping(base_path.resolve(), stack=(*stack, config_path))
    return _merge_config(base, current)


def _merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _merge_config(existing, value)
        else:
            merged[key] = value
    return merged
