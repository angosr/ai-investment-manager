from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationInfo, model_validator

from investment_manager.execution.models import SUPPORTED_OPEN_SIDES
from investment_manager.execution.policy import (
    BinanceTestnetPolicy,
    ExecutionPolicy,
    ReconciliationPolicy,
    ShadowSimulationPolicy,
)
from investment_manager.forecast.policy import (
    AiMode,
    CalibrationPolicy,
    CodexAccountRegistry,
    CodexRuntimePolicy,
    ContextAssessmentPolicy,
    PipelinePolicy,
    ProposalPolicy,
    StrategyPolicy,
)
from investment_manager.governance.policy import (
    DeploymentPolicy,
    DeploymentStage,
    GovernancePolicy,
    OutcomeEvaluationPolicy,
)
from investment_manager.information.aggregated_flows import (
    AGGREGATED_FLOW_FACT_TYPES,
    AGGREGATED_FLOW_RISK_FACTORS,
)
from investment_manager.information.official.metrics import (
    OFFICIAL_METRIC_FACT_TYPES,
    OFFICIAL_METRIC_RISK_FACTORS,
)
from investment_manager.information.policy import InformationPolicy
from investment_manager.kernel.configuration import StrictConfig
from investment_manager.market.policy import FeaturePolicy, MarketDataPolicy
from investment_manager.portfolio.policy import (
    CapitalPolicy,
    CompositionPolicy,
    FrequencyPolicy,
)
from investment_manager.risk.policy import RiskPolicy
from investment_manager.scheduling.policy import TemporalPolicy, TriggerPolicy
from investment_manager.state.policy import DecisionStatePolicy, PanelPolicy


class AppConfig(StrictConfig):
    feature: FeaturePolicy
    panel: PanelPolicy
    decision_state: DecisionStatePolicy
    strategy: StrategyPolicy
    calibration: CalibrationPolicy
    capital: CapitalPolicy
    composition: CompositionPolicy
    frequency: FrequencyPolicy
    risk: RiskPolicy
    execution: ExecutionPolicy
    reconciliation: ReconciliationPolicy
    outcome_evaluation: OutcomeEvaluationPolicy
    governance: GovernancePolicy
    trigger: TriggerPolicy
    temporal: TemporalPolicy
    market_data: MarketDataPolicy
    shadow: ShadowSimulationPolicy
    information: InformationPolicy
    pipeline: PipelinePolicy
    proposal: ProposalPolicy
    codex_runtime: CodexRuntimePolicy
    assessment: ContextAssessmentPolicy
    codex_accounts: CodexAccountRegistry
    binance_testnet: BinanceTestnetPolicy
    deployment: DeploymentPolicy

    @model_validator(mode="after")
    def cross_domain_invariants_hold(self, info: ValidationInfo):
        if (
            isinstance(info.context, dict)
            and info.context.get("historical_read_only") is True
        ):
            return self
        if self.decision_state.analysis_scope != self.assessment.mandate.analysis_scope:
            raise ValueError("DecisionState 与 Assessment mandate scope 必须一致")
        mandate_symbols = tuple(
            item.market_symbol for item in self.assessment.mandate.assets
        )
        if tuple(sorted(mandate_symbols)) != tuple(sorted(self.market_data.symbols)):
            raise ValueError("Assessment mandate 必须完整覆盖且排序匹配行情 universe")
        perpetual_symbols = tuple(
            item.symbol for item in self.market_data.perpetual_instruments
        )
        if self.assessment.enabled and set(perpetual_symbols) != set(mandate_symbols):
            raise ValueError("启用 ContextAssessment 时 Perpetual universe 必须完整覆盖 Mandate")
        if self.assessment.enabled and (
            self.assessment.review_trigger_symbol not in mandate_symbols
        ):
            raise ValueError("启用 ContextAssessment 时必须指定 Mandate 内的复核协调 symbol")
        mandate_horizons = tuple(
            sorted(
                {
                    horizon
                    for asset in self.assessment.mandate.assets
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
        mandate_assets = tuple(item.asset for item in self.assessment.mandate.assets)
        if mandate_assets != self.decision_state.official_fact_policy.affected_assets:
            raise ValueError("OfficialFact projection 与 Assessment mandate 资产必须一致")
        required_risk_factors = set(self.assessment.mandate.required_risk_factors)
        configured_risk_factors = {
            *self.decision_state.official_fact_policy.release_risk_factors,
            *self.decision_state.delta_policy.intelligence_risk_factors,
            *self.decision_state.delta_policy.market_risk_factors,
        }
        configured_fact_types = {
            item.fact_type for item in self.decision_state.delta_policy.rules
        }
        if configured_fact_types & OFFICIAL_METRIC_FACT_TYPES:
            configured_risk_factors.update(OFFICIAL_METRIC_RISK_FACTORS)
        if configured_fact_types & AGGREGATED_FLOW_FACT_TYPES:
            configured_risk_factors.update(AGGREGATED_FLOW_RISK_FACTORS)
        if not configured_risk_factors.issubset(required_risk_factors):
            raise ValueError("DecisionState 风险因子必须属于 Assessment mandate")
        if self.assessment.enabled and not self.codex_runtime.enabled:
            raise ValueError("启用 ContextAssessment 前必须启用受控 Codex runtime")
        if self.assessment.enabled and self.pipeline.ai_mode == AiMode.PROPOSE:
            raise ValueError("旧 PROPOSE 与 ContextAssessment 不得同时调用 Codex")
        if not set(self.market_data.symbols).issubset(self.risk.symbol_allowlist):
            raise ValueError("行情 symbols 必须是风控允许品种的子集")
        if self.capital.enabled:
            if self.deployment.stage != DeploymentStage.SHADOW:
                raise ValueError("当前 Capital 候选权限只允许 SHADOW Mock")
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
        permissions = self.capital.mock_candidate_authorizations
        if permissions and not self.capital.enabled:
            raise ValueError("禁用 Capital 时不得保留 Mock candidate authorization")
        if any(
            not symbol.endswith(self.binance_testnet.quote_asset)
            for symbol in self.market_data.symbols
        ):
            raise ValueError("行情 symbols 必须使用配置的统一 quote_asset")
        testnet_market_data = (
            self.market_data.rest_base_url == "https://testnet.binance.vision"
        )
        if self.deployment.stage == DeploymentStage.TESTNET and not testnet_market_data:
            raise ValueError("TESTNET 必须使用 Binance Spot Testnet 行情")
        if self.deployment.stage != DeploymentStage.TESTNET and testnet_market_data:
            raise ValueError("非 TESTNET 阶段不得混用 Binance Spot Testnet 行情")
        if self.pipeline.ai_mode == AiMode.PROPOSE:
            enabled_accounts = sum(item.enabled for item in self.codex_accounts.accounts)
            if self.temporal.worker_threads > enabled_accounts:
                raise ValueError("PROPOSE 分析并发不得超过已启用 Codex 账号数")
        active_producers = {
            (self.strategy.strategy_id, self.strategy.version): self.strategy.horizon_minutes,
        }
        if self.pipeline.ai_mode == AiMode.PROPOSE:
            active_producers[(self.proposal.producer_id, self.proposal.version)] = None
        for artifact in self.calibration.artifacts:
            producer = (artifact.producer_id, artifact.producer_version)
            if producer not in active_producers:
                raise ValueError("校准制品必须绑定当前管线实际装配的 Producer 版本")
            expected_horizon = active_producers[producer]
            if expected_horizon is not None and artifact.horizon_minutes != expected_horizon:
                raise ValueError("程序策略校准周期必须与当前策略周期一致")
            if (
                expected_horizon is None
                and artifact.horizon_minutes > self.proposal.maximum_horizon_minutes
            ):
                raise ValueError("AI 校准周期超过 ProposalPolicy 上限")
            if artifact.symbol not in self.market_data.symbols:
                raise ValueError("校准制品品种必须属于当前行情 universe")
            if artifact.side not in SUPPORTED_OPEN_SIDES:
                raise ValueError("校准制品方向必须属于当前允许建仓方向")
        return self


def load_config(
    path: str | Path,
    *,
    historical_read_only: bool = False,
) -> AppConfig:
    """加载严格配置；小型环境文件可用 ``extends`` 继承同目录基线。"""

    config_path = Path(path).resolve()
    return AppConfig.model_validate(
        _load_config_mapping(config_path, stack=()),
        context={"historical_read_only": historical_read_only},
    )


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
