"""Strict configuration for offline research and retired replay contracts."""

from __future__ import annotations

from pathlib import Path

from pydantic import model_validator

from investment_manager.execution.models import SUPPORTED_OPEN_SIDES
from investment_manager.execution.policy import ExecutionPolicy, ReconciliationPolicy
from investment_manager.forecast.policy import (
    AiMode,
    CalibrationPolicy,
    ProposalPolicy,
    ResearchPipelinePolicy,
    StrategyPolicy,
)
from investment_manager.portfolio.policy import CompositionPolicy, FrequencyPolicy
from investment_manager.risk.policy import RiskPolicy
from investment_manager.settings import AppConfig, load_config_mapping
from investment_manager.state.policy import PanelPolicy


class ResearchConfig(AppConfig):
    """Offline-only policies excluded from managed release identity."""

    panel: PanelPolicy
    strategy: StrategyPolicy
    calibration: CalibrationPolicy
    composition: CompositionPolicy
    frequency: FrequencyPolicy
    risk: RiskPolicy
    execution: ExecutionPolicy
    reconciliation: ReconciliationPolicy
    proposal: ProposalPolicy
    pipeline: ResearchPipelinePolicy

    @model_validator(mode="after")
    def legacy_replay_invariants_hold(self):
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
                raise ValueError("校准制品必须绑定研究管线实际装配的 Producer 版本")
            expected_horizon = active_producers[producer]
            if expected_horizon is not None and artifact.horizon_minutes != expected_horizon:
                raise ValueError("程序策略校准周期必须与当前策略周期一致")
            if (
                expected_horizon is None
                and artifact.horizon_minutes > self.proposal.maximum_horizon_minutes
            ):
                raise ValueError("AI 校准周期超过 ProposalPolicy 上限")
            if artifact.symbol not in self.market_data.symbols:
                raise ValueError("校准制品品种必须属于研究行情 universe")
            if artifact.side not in SUPPORTED_OPEN_SIDES:
                raise ValueError("校准制品方向必须属于允许建仓方向")
        return self


def load_research_config(path: str | Path) -> ResearchConfig:
    config_path = Path(path).resolve()
    return ResearchConfig.model_validate(load_config_mapping(config_path))
