from __future__ import annotations

from quant_core.config import AppConfig
from quant_core.research.backtest import ResearchStrategy
from quant_core.strategy import PriceTrendStrategy


def resolve_research_candidate(
    candidate: str, config: AppConfig
) -> tuple[AppConfig, ResearchStrategy]:
    """Resolve active research code; failed candidates live only in result artifacts."""

    if candidate == "configured":
        if not config.strategy.enabled:
            raise ValueError("已禁用的程序策略不能作为历史或前瞻评价基线")
        return config, PriceTrendStrategy(config.strategy)
    raise ValueError(f"未知或已退役的历史候选: {candidate}")
