from __future__ import annotations

from investment_manager.research.backtest import ResearchStrategy
from investment_manager.research.dataset import HistoricalFundingDataset
from investment_manager.settings import AppConfig
from investment_manager.strategy import PriceTrendStrategy


def resolve_research_candidate(
    candidate: str,
    config: AppConfig,
    *,
    funding_dataset: HistoricalFundingDataset | None = None,
) -> tuple[AppConfig, ResearchStrategy]:
    """Resolve active research code; failed candidates live only in result artifacts."""

    if candidate == "configured":
        if funding_dataset is not None:
            raise ValueError("configured 基线不接受未使用的资金费率数据集")
        if not config.strategy.enabled:
            raise ValueError("已禁用的程序策略不能作为历史或前瞻评价基线")
        return config, PriceTrendStrategy(config.strategy)
    raise ValueError(f"未知或已退役的历史候选: {candidate}")
