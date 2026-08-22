from __future__ import annotations

import math
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise

from investment_manager.kernel.identity import content_hash
from investment_manager.market.models import FeatureSnapshot, MarketQuote, MarketSnapshot
from investment_manager.market.perpetual.models import (
    DerivativeContextSnapshot,
    FundingSettlement,
    PerpetualMarketState,
    PerpetualQuote,
)
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


def build_derivative_context_snapshot(
    *,
    cycle_id: str,
    asset: str,
    spot: MarketSnapshot,
    aligned_spot_quote: MarketQuote,
    state: PerpetualMarketState,
    quote: PerpetualQuote,
    settlements: tuple[FundingSettlement, ...],
    funding_window_hours: int,
    maximum_quote_skew_seconds: int,
) -> DerivativeContextSnapshot:
    """Project executable basis and funding history into one dense, replayable fact."""

    if not 1 <= funding_window_hours <= 720:
        raise ValueError("Funding 汇总窗口必须在 1..720 小时")
    if maximum_quote_skew_seconds < 1:
        raise ValueError("跨市场报价时间偏差上限必须为正数")
    if state.instrument != quote.instrument or state.instrument.symbol != spot.symbol:
        raise ValueError("衍生品状态、报价和 Spot 快照必须属于同一产品标的")
    if aligned_spot_quote.symbol != spot.symbol:
        raise ValueError("对齐 Spot 报价必须属于同一标的")
    if spot.cycle_id != cycle_id:
        raise ValueError("衍生品决策状态必须绑定同一 cycle")
    if state.observed_at > spot.as_of or quote.observed_at > spot.as_of:
        raise ValueError("衍生品决策状态不能使用 as_of 后才可见的数据")
    if aligned_spot_quote.observed_at > spot.as_of:
        raise ValueError("对齐 Spot 报价不能在 as_of 后才可见")
    if abs(
        (quote.observed_at - aligned_spot_quote.observed_at).total_seconds()
    ) > maximum_quote_skew_seconds:
        raise ValueError("Spot/Perpetual 报价时间偏差过大")
    window_start = spot.as_of - timedelta(hours=funding_window_hours)
    visible = tuple(
        item
        for item in settlements
        if item.instrument == state.instrument
        and window_start <= item.funding_time < spot.as_of
        and item.observed_at <= spot.as_of
    )
    if len({item.settlement_id for item in visible}) != len(visible):
        raise ValueError("Funding 汇总不能包含重复结算")
    rates_bps = tuple(item.funding_rate * Decimal("10000") for item in visible)
    rate_sum = sum(rates_bps, Decimal("0")) if rates_bps else None
    rate_mean = rate_sum / Decimal(len(rates_bps)) if rate_sum is not None else None
    if rate_mean is None:
        rate_stddev = None
        positive_fraction = None
        rate_min = None
    else:
        variance = sum(
            ((rate - rate_mean) ** 2 for rate in rates_bps),
            Decimal("0"),
        ) / Decimal(len(rates_bps))
        rate_stddev = variance.sqrt()
        positive_fraction = Decimal(
            sum(1 for rate in rates_bps if rate > 0)
        ) / Decimal(len(rates_bps))
        rate_min = min(rates_bps)
    spot_flow = _spot_flow_summary(spot, window_minutes=60)
    return DerivativeContextSnapshot(
        cycle_id=cycle_id,
        asset=asset,
        instrument=state.instrument,
        as_of=spot.as_of,
        observed_at=max(state.observed_at, quote.observed_at),
        mark_index_premium_bps=(state.mark_price / state.index_price - Decimal("1"))
        * Decimal("10000"),
        executable_short_basis_bps=(
            quote.bid / aligned_spot_quote.ask - Decimal("1")
        )
        * Decimal("10000"),
        perpetual_spread_bps=(quote.ask - quote.bid)
        / ((quote.ask + quote.bid) / Decimal("2"))
        * Decimal("10000"),
        last_funding_rate_bps=state.last_funding_rate * Decimal("10000"),
        trailing_funding_rate_mean_bps=rate_mean,
        trailing_funding_rate_sum_bps=rate_sum,
        trailing_funding_rate_stddev_bps=rate_stddev,
        trailing_funding_positive_fraction=positive_fraction,
        trailing_funding_rate_min_bps=rate_min,
        funding_settlement_count=len(rates_bps),
        funding_window_hours=funding_window_hours,
        next_funding_time=state.next_funding_time,
        **spot_flow,
        positioning_observed_at=state.positioning_observed_at,
        positioning_window_minutes=state.positioning_window_minutes,
        open_interest=state.open_interest,
        open_interest_value=state.open_interest_value,
        open_interest_change_fraction=state.open_interest_change_fraction,
        global_long_short_account_ratio=state.global_long_short_account_ratio,
        global_long_account_fraction=state.global_long_account_fraction,
        global_short_account_fraction=state.global_short_account_fraction,
        taker_buy_sell_ratio=state.taker_buy_sell_ratio,
        taker_buy_volume=state.taker_buy_volume,
        taker_sell_volume=state.taker_sell_volume,
        input_refs=tuple(
            sorted(
                {
                    content_hash(spot),
                    aligned_spot_quote.quote_id,
                    state.state_id,
                    quote.quote_id,
                    *(item.settlement_id for item in visible),
                }
            )
        ),
    )


def _spot_flow_summary(
    spot: MarketSnapshot,
    *,
    window_minutes: int,
) -> dict[str, Decimal | datetime | int]:
    window_start = spot.as_of - timedelta(minutes=window_minutes)
    bars = tuple(
        item
        for item in spot.bars
        if item.event_time >= window_start
        and item.taker_buy_base_volume is not None
        and item.quote_volume is not None
        and item.taker_buy_quote_volume is not None
    )
    if len(bars) < 2:
        return {}
    intervals = tuple(
        int((right.event_time - left.event_time).total_seconds() // 60)
        for left, right in pairwise(bars)
    )
    if not intervals or intervals[0] <= 0 or len(set(intervals)) != 1:
        return {}
    covered_minutes = min(window_minutes, intervals[0] * len(bars))
    total_volume = sum((item.volume for item in bars), Decimal("0"))
    buy_volume = sum(
        (item.taker_buy_base_volume for item in bars if item.taker_buy_base_volume is not None),
        Decimal("0"),
    )
    sell_volume = total_volume - buy_volume
    if sell_volume <= 0 or buy_volume < 0:
        return {}
    return {
        "spot_flow_observed_at": max(item.observed_at for item in bars),
        "spot_flow_window_minutes": covered_minutes,
        "spot_taker_buy_sell_ratio": buy_volume / sell_volume,
        "spot_taker_buy_volume": buy_volume,
        "spot_taker_sell_volume": sell_volume,
    }
