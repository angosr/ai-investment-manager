from __future__ import annotations

import math
from decimal import Decimal
from itertools import pairwise

from investment_manager.market.models import FeatureSnapshot, MarketSnapshot
from investment_manager.market.policy import FeaturePolicy


class FeatureEngine:
    def __init__(self, policy: FeaturePolicy) -> None:
        self._policy = policy

    def compute(self, market: MarketSnapshot) -> FeatureSnapshot:
        bars = market.bars
        closes = [bar.close for bar in bars]
        return_fraction = (closes[-1] / closes[0]) - Decimal("1")

        returns = [float((right / left) - Decimal("1")) for left, right in pairwise(closes)]
        window = returns[-self._policy.volatility_window :]
        mean = sum(window) / len(window)
        variance = sum((value - mean) ** 2 for value in window) / len(window)
        realized_volatility = Decimal(str(math.sqrt(variance)))

        ranges: list[Decimal] = []
        for index, bar in enumerate(bars):
            if index == 0:
                ranges.append(bar.high - bar.low)
                continue
            previous_close = bars[index - 1].close
            ranges.append(
                max(
                    bar.high - bar.low,
                    abs(bar.high - previous_close),
                    abs(bar.low - previous_close),
                )
            )
        atr_window = ranges[-self._policy.volatility_window :]
        atr = sum(atr_window, Decimal("0")) / Decimal(len(atr_window))

        previous_volumes = [bar.volume for bar in bars[:-1]]
        average_volume = sum(previous_volumes, Decimal("0")) / Decimal(len(previous_volumes))
        volume_ratio = bars[-1].volume / average_volume if average_volume > 0 else Decimal("0")
        spread_bps = ((market.ask - market.bid) / market.last) * Decimal("10000")

        if return_fraction >= self._policy.trend_threshold:
            regime = "TRENDING_UP"
        elif return_fraction <= -self._policy.trend_threshold:
            regime = "TRENDING_DOWN"
        else:
            regime = "RANGING"

        market_age = max(0, int((market.as_of - market.observed_at).total_seconds()))
        return FeatureSnapshot(
            cycle_id=market.cycle_id,
            symbol=market.symbol,
            as_of=market.as_of,
            feature_set_version=self._policy.version,
            return_fraction=return_fraction,
            realized_volatility=realized_volatility,
            atr=atr,
            spread_bps=spread_bps,
            volume_ratio=volume_ratio,
            regime=regime,
            market_age_seconds=market_age,
        )
