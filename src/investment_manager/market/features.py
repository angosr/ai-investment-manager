from __future__ import annotations

import math
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import TYPE_CHECKING

from investment_manager.kernel.identity import content_hash
from investment_manager.kernel.time import require_utc
from investment_manager.market.models import (
    CrossVenueSpotQuote,
    ExecutableQuote,
    FeatureSnapshot,
    InstrumentId,
    InstrumentProduct,
    MarketQuote,
    MarketSnapshot,
    ValuationQuote,
    ValuationQuoteQuality,
)
from investment_manager.market.perpetual.models import (
    DerivativeContextSnapshot,
    FundingSettlement,
    PerpetualMarketState,
    PerpetualQuote,
    TradingScheduleSnapshot,
)
from investment_manager.market.policy import FeaturePolicy

if TYPE_CHECKING:
    from investment_manager.market.repository import MarketDataStore


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
    cross_venue_quotes: tuple[CrossVenueSpotQuote, ...] = (),
    maximum_cross_venue_age_seconds: int = 30,
) -> DerivativeContextSnapshot:
    """Project executable basis and funding history into one dense, replayable fact."""

    if not 1 <= funding_window_hours <= 720:
        raise ValueError("Funding 汇总窗口必须在 1..720 小时")
    if maximum_quote_skew_seconds < 1:
        raise ValueError("跨市场报价时间偏差上限必须为正数")
    if maximum_cross_venue_age_seconds < 1:
        raise ValueError("跨场所现货最大年龄必须为正数")
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
    visible, funding = _funding_summary(
        instrument=state.instrument,
        as_of=spot.as_of,
        settlements=settlements,
        funding_window_hours=funding_window_hours,
    )
    spot_flow = _spot_flow_summary(spot, window_minutes=60)
    cross_venue = _cross_venue_spot_summary(
        spot=spot,
        aligned_spot_quote=aligned_spot_quote,
        quotes=cross_venue_quotes,
        maximum_age_seconds=maximum_cross_venue_age_seconds,
    )
    return DerivativeContextSnapshot(
        cycle_id=cycle_id,
        asset=asset,
        instrument=state.instrument,
        as_of=spot.as_of,
        observed_at=max(
            state.observed_at,
            quote.observed_at,
            *(item.observed_at for item in cross_venue_quotes),
        ),
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
        **funding,
        funding_window_hours=funding_window_hours,
        next_funding_time=state.next_funding_time,
        **spot_flow,
        **cross_venue,
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
                    *(item.quote_id for item in cross_venue_quotes),
                }
            )
        ),
    )


def build_perpetual_only_context_snapshot(
    *,
    cycle_id: str,
    asset: str,
    as_of: datetime,
    state: PerpetualMarketState,
    quote: PerpetualQuote,
    settlements: tuple[FundingSettlement, ...],
    funding_window_hours: int,
) -> DerivativeContextSnapshot:
    """Build a dense derivative state when the venue has no executable Spot leg.

    TradFi perpetuals expose their own exchange index.  It is a point-in-time
    reference for basis diagnostics, not a fabricated Spot instrument.
    """

    at = require_utc(as_of)
    if state.instrument != quote.instrument:
        raise ValueError("永续状态与报价必须属于同一产品")
    if state.observed_at > at or quote.observed_at > at:
        raise ValueError("永续决策状态不能使用 as_of 后才可见的数据")
    visible, funding = _funding_summary(
        instrument=state.instrument,
        as_of=at,
        settlements=settlements,
        funding_window_hours=funding_window_hours,
    )
    return DerivativeContextSnapshot(
        cycle_id=cycle_id,
        asset=asset,
        instrument=state.instrument,
        as_of=at,
        observed_at=max(state.observed_at, quote.observed_at),
        mark_index_premium_bps=(state.mark_price / state.index_price - Decimal("1"))
        * Decimal("10000"),
        executable_short_basis_bps=(quote.bid / state.index_price - Decimal("1"))
        * Decimal("10000"),
        perpetual_spread_bps=(quote.ask - quote.bid)
        / ((quote.ask + quote.bid) / Decimal("2"))
        * Decimal("10000"),
        last_funding_rate_bps=state.last_funding_rate * Decimal("10000"),
        **funding,
        funding_window_hours=funding_window_hours,
        next_funding_time=state.next_funding_time,
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
                    state.state_id,
                    quote.quote_id,
                    *(item.settlement_id for item in visible),
                }
            )
        ),
    )


def _funding_summary(
    *,
    instrument: InstrumentId,
    as_of: datetime,
    settlements: tuple[FundingSettlement, ...],
    funding_window_hours: int,
) -> tuple[tuple[FundingSettlement, ...], dict[str, object]]:
    if not 1 <= funding_window_hours <= 720:
        raise ValueError("Funding 汇总窗口必须在 1..720 小时")
    window_start = as_of - timedelta(hours=funding_window_hours)
    visible = tuple(
        item
        for item in settlements
        if item.instrument == instrument
        and window_start <= item.funding_time < as_of
        and item.observed_at <= as_of
    )
    if len({item.settlement_id for item in visible}) != len(visible):
        raise ValueError("Funding 汇总不能包含重复结算")
    rates = tuple(item.funding_rate * Decimal("10000") for item in visible)
    rate_sum = sum(rates, Decimal("0")) if rates else None
    rate_mean = rate_sum / Decimal(len(rates)) if rate_sum is not None else None
    if rate_mean is None:
        rate_stddev = None
        positive_fraction = None
        rate_min = None
    else:
        variance = sum(
            ((rate - rate_mean) ** 2 for rate in rates),
            Decimal("0"),
        ) / Decimal(len(rates))
        rate_stddev = variance.sqrt()
        positive_fraction = Decimal(sum(1 for rate in rates if rate > 0)) / Decimal(
            len(rates)
        )
        rate_min = min(rates)
    return visible, {
        "trailing_funding_rate_mean_bps": rate_mean,
        "trailing_funding_rate_sum_bps": rate_sum,
        "trailing_funding_rate_stddev_bps": rate_stddev,
        "trailing_funding_positive_fraction": positive_fraction,
        "trailing_funding_rate_min_bps": rate_min,
        "funding_settlement_count": len(rates),
    }


def _cross_venue_spot_summary(
    *,
    spot: MarketSnapshot,
    aligned_spot_quote: MarketQuote,
    quotes: tuple[CrossVenueSpotQuote, ...],
    maximum_age_seconds: int,
) -> dict[str, Decimal | datetime | int]:
    if not quotes:
        return {}
    if len(quotes) < 2:
        raise ValueError("跨场所现货摘要至少需要两个独立外部场所")
    venues = tuple(item.venue.value for item in quotes)
    if tuple(sorted(set(venues))) != venues:
        raise ValueError("跨场所现货报价必须按 venue 唯一排序")
    if any(item.symbol != spot.symbol for item in quotes):
        raise ValueError("跨场所现货报价必须属于同一标的")
    if any(item.observed_at > spot.as_of for item in quotes):
        raise ValueError("跨场所现货报价不能在 as_of 后才可见")
    if any(
        (spot.as_of - item.observed_at).total_seconds() > maximum_age_seconds
        for item in quotes
    ):
        raise ValueError("跨场所现货报价已过期")
    binance_mid = (aligned_spot_quote.bid + aligned_spot_quote.ask) / Decimal("2")
    external_mids = tuple((item.bid + item.ask) / Decimal("2") for item in quotes)
    mids = tuple(sorted((binance_mid, *external_mids)))
    middle = len(mids) // 2
    median_mid = (
        mids[middle]
        if len(mids) % 2
        else (mids[middle - 1] + mids[middle]) / Decimal("2")
    )
    spreads = (
        (aligned_spot_quote.ask - aligned_spot_quote.bid) / binance_mid,
        *(
            (item.ask - item.bid) / ((item.ask + item.bid) / Decimal("2"))
            for item in quotes
        ),
    )
    return {
        "cross_venue_observed_at": max(item.observed_at for item in quotes),
        "spot_venue_count": 1 + len(quotes),
        "spot_mid_range_bps": (mids[-1] / mids[0] - Decimal("1"))
        * Decimal("10000"),
        "reference_spot_mid_deviation_bps": (binance_mid / median_mid - Decimal("1"))
        * Decimal("10000"),
        "widest_spot_spread_bps": max(spreads) * Decimal("10000"),
    }


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


def freeze_quote_views(
    *,
    instrument: InstrumentId,
    quote: MarketQuote | PerpetualQuote,
    as_of: datetime,
    maximum_live_age_seconds: int,
    trading_schedule: TradingScheduleSnapshot | None = None,
) -> tuple[ValuationQuote, ExecutableQuote | None]:
    """Project one raw quote into valuation and, only when legal, execution views."""

    at = require_utc(as_of)
    if maximum_live_age_seconds < 1:
        raise ValueError("报价最大实时年龄必须为正数")
    if isinstance(quote, MarketQuote):
        if instrument.product != InstrumentProduct.SPOT or quote.symbol != instrument.symbol:
            raise ValueError("Spot 原始报价与 Instrument 不一致")
        market_at = quote.observed_at
    else:
        if quote.instrument != instrument:
            raise ValueError("Perpetual 原始报价与 Instrument 不一致")
        market_at = quote.exchange_time
    if quote.observed_at > at or market_at > at:
        raise ValueError("报价在资本截止时点尚不可见")

    schedule_ref: str | None = None
    quality = ValuationQuoteQuality.LIVE_MARKET
    if instrument.product == InstrumentProduct.TRADFI_PERPETUAL:
        quality, schedule_ref = _tradfi_quote_quality(
            instrument=instrument,
            at=at,
            market_at=market_at,
            maximum_live_age_seconds=maximum_live_age_seconds,
            schedule=trading_schedule,
        )
    elif trading_schedule is not None:
        raise ValueError("非 TradFi 报价不得读取交易日历")
    elif (at - market_at).total_seconds() > maximum_live_age_seconds:
        quality = ValuationQuoteQuality.STALE_MARKET

    common = {
        "source_quote_id": quote.quote_id,
        "instrument": instrument,
        "as_of": at,
        "observed_at": market_at,
        "bid": quote.bid,
        "ask": quote.ask,
        "source": quote.source,
        "quality": quality,
        "trading_schedule_ref": schedule_ref,
    }
    valuation = ValuationQuote(**common)
    executable = (
        ExecutableQuote(
            **common,
            bid_quantity=quote.bid_quantity,
            ask_quantity=quote.ask_quantity,
        )
        if quality == ValuationQuoteQuality.LIVE_MARKET
        and quote.bid_quantity > 0
        and quote.ask_quantity > 0
        else None
    )
    return valuation, executable


def point_in_time_quote_views(
    *,
    market: MarketDataStore,
    instrument: InstrumentId,
    as_of: datetime,
    maximum_live_age_seconds: int,
    trading_schedule: TradingScheduleSnapshot | None = None,
) -> tuple[ValuationQuote, ExecutableQuote | None] | None:
    """Read and freeze the latest quote visible at one capital cutoff."""

    at = require_utc(as_of)
    quote = (
        market.latest_spot_quote(
            instrument=instrument,
            evaluation_at=at,
            visible_at=at,
        )
        if instrument.product == InstrumentProduct.SPOT
        else market.latest_perpetual_quote(
            instrument=instrument,
            evaluation_at=at,
            visible_at=at,
        )
    )
    if quote is None:
        return None
    return freeze_quote_views(
        instrument=instrument,
        quote=quote,
        as_of=at,
        maximum_live_age_seconds=maximum_live_age_seconds,
        trading_schedule=trading_schedule,
    )


def _tradfi_quote_quality(
    *,
    instrument: InstrumentId,
    at: datetime,
    market_at: datetime,
    maximum_live_age_seconds: int,
    schedule: TradingScheduleSnapshot | None,
) -> tuple[ValuationQuoteQuality, str | None]:
    if schedule is None:
        return ValuationQuoteQuality.STALE_MARKET, None
    if schedule.observed_at > at:
        raise ValueError("交易日历在资本截止时点尚不可见")
    relevant = tuple(
        item for item in schedule.sessions if item.market == instrument.tradfi_market
    )
    if not relevant or not relevant[0].starts_at <= at < relevant[-1].ends_at:
        return ValuationQuoteQuality.STALE_MARKET, None
    session = schedule.session_at(instrument=instrument, at=at)
    if session is None or not session.session_type.tradable:
        return ValuationQuoteQuality.CLOSED_MARKET, schedule.schedule_id
    if (at - market_at).total_seconds() > maximum_live_age_seconds:
        return ValuationQuoteQuality.STALE_MARKET, schedule.schedule_id
    return ValuationQuoteQuality.LIVE_MARKET, schedule.schedule_id
