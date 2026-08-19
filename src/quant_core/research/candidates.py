from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal

from pydantic import Field, computed_field

from quant_core.calibration import EDGE_CALIBRATION_MISSING, uncalibrated_ref
from quant_core.config import AppConfig
from quant_core.domain import (
    AccountSnapshot,
    Action,
    FeatureSnapshot,
    FrozenModel,
    IntelligenceEvent,
    MarketBar,
    MarketSnapshot,
    OrderType,
    PriceCondition,
    Side,
    SignalCandidate,
)
from quant_core.ids import stable_id
from quant_core.research.backtest import ResearchStrategy
from quant_core.research.dataset import HistoricalFundingDataset
from quant_core.strategy import PriceTrendStrategy

FUNDING_FILTERED_DUAL_TREND_CANDIDATE = (
    "long-only-dual-trend-28d-sma200-funding-p90-5d-v1"
)


class LongOnlyFundingFilteredDualTrendSpec(FrozenModel):
    """Frozen long/cash trend rule with a point-in-time funding crowding veto."""

    strategy_id: str = "long-only-funding-filtered-dual-trend"
    version: str = FUNDING_FILTERED_DUAL_TREND_CANDIDATE
    family: str = "dual-trend-funding-crowding-filter"
    interval: str = "1d"
    momentum_lookback_bars: int = Field(default=28, ge=2)
    regime_moving_average_bars: int = Field(default=200, ge=2)
    atr_bars: int = Field(default=30, ge=2)
    stop_atr_multiple: Decimal = Field(default=Decimal("3"), gt=0)
    horizon_minutes: int = Field(default=7_200, gt=0)
    cooldown_minutes: int = Field(default=7_200, ge=0)
    signal_validity_minutes: int = Field(default=1_440, gt=0)
    expected_edge_half_life_seconds: int = Field(default=86_400, gt=0, le=86_400)
    funding_smoothing_observations: int = Field(default=21, ge=2)
    funding_reference_observations: int = Field(default=1_095, ge=30)
    funding_crowding_percentile: Decimal = Field(default=Decimal("0.90"), gt=0, lt=1)
    funding_dataset_id: str = Field(min_length=1)
    funding_observations_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    symbol: str = Field(min_length=1)

    @computed_field
    @property
    def required_bar_window(self) -> int:
        return max(
            self.momentum_lookback_bars + 1,
            self.regime_moving_average_bars,
            self.atr_bars + 1,
        )

    @computed_field
    @property
    def required_funding_observations(self) -> int:
        # One current smoothing window plus a non-overlapping reference sequence.
        return (
            self.funding_smoothing_observations
            + self.funding_reference_observations
        )


class LongOnlyFundingFilteredDualTrendStrategy:
    def __init__(
        self,
        spec: LongOnlyFundingFilteredDualTrendSpec,
        funding_dataset: HistoricalFundingDataset,
    ) -> None:
        self._spec = spec
        self._funding_rates = tuple(
            observation.funding_rate for observation in funding_dataset.observations
        )
        self._funding_available_at = tuple(
            observation.available_at for observation in funding_dataset.observations
        )
        smoothing = spec.funding_smoothing_observations
        prefix = [Decimal("0")]
        for rate in self._funding_rates:
            prefix.append(prefix[-1] + rate)
        self._funding_rolling_means = tuple(
            (prefix[index + smoothing] - prefix[index]) / Decimal(smoothing)
            for index in range(len(self._funding_rates) - smoothing + 1)
        )

    @property
    def research_spec(self) -> LongOnlyFundingFilteredDualTrendSpec:
        return self._spec

    def evaluate(
        self,
        *,
        market: MarketSnapshot,
        account: AccountSnapshot,
        features: FeatureSnapshot,
        events: tuple[IntelligenceEvent, ...] = (),
    ) -> tuple[SignalCandidate, ...]:
        spec = self._spec
        if market.symbol != spec.symbol or len(market.bars) < spec.required_bar_window:
            return ()
        if any(
            position.symbol == market.symbol and position.quantity > 0
            for position in account.positions
        ):
            return ()
        if self._funding_is_crowded(market.as_of):
            return ()

        current_close = market.bars[-1].close
        previous_close = market.bars[-1 - spec.momentum_lookback_bars].close
        momentum = current_close / previous_close - Decimal("1")
        regime_average = sum(
            (
                bar.close
                for bar in market.bars[-spec.regime_moving_average_bars :]
            ),
            Decimal("0"),
        ) / Decimal(spec.regime_moving_average_bars)
        regime_distance = current_close / regime_average - Decimal("1")
        if momentum <= 0 or regime_distance <= 0:
            return ()

        stop_price = market.ask - _average_true_range(
            market.bars,
            spec.atr_bars,
        ) * spec.stop_atr_multiple
        if stop_price <= 0 or stop_price >= market.ask:
            return ()

        return (
            SignalCandidate(
                candidate_id=stable_id(
                    "sig",
                    market.cycle_id,
                    spec.strategy_id,
                    spec.version,
                    market.symbol,
                ),
                cycle_id=market.cycle_id,
                producer_id=spec.strategy_id,
                producer_version=spec.version,
                strategy_family=spec.family,
                symbol=market.symbol,
                action=Action.OPEN,
                side=Side.BUY,
                horizon_minutes=spec.horizon_minutes,
                feature_refs=(features.feature_set_version,),
                entry=PriceCondition(order_type=OrderType.MARKET),
                stop_price=stop_price,
                valid_until=market.as_of
                + timedelta(minutes=spec.signal_validity_minutes),
                signal_observed_at=market.as_of,
                reference_price=market.ask,
                expected_edge_half_life_seconds=spec.expected_edge_half_life_seconds,
                raw_score=min(Decimal("1"), momentum, regime_distance),
                expected_gross_bps=Decimal("0"),
                calibration_ref=uncalibrated_ref(spec.version),
                unknowns=(EDGE_CALIBRATION_MISSING,),
            ),
        )

    def _funding_is_crowded(self, as_of: datetime) -> bool:
        """Use only rates visible by as_of; missing history rejects the trade."""

        spec = self._spec
        visible_count = bisect_right(self._funding_available_at, as_of)
        if visible_count < spec.required_funding_observations:
            return True
        smoothing = spec.funding_smoothing_observations
        reference_count = spec.funding_reference_observations
        current_start = visible_count - smoothing
        current_mean = self._funding_rolling_means[current_start]

        reference_start = current_start - reference_count
        # The reference distribution is built only from fully prior observations.
        reference_means = self._funding_rolling_means[
            reference_start : current_start - smoothing + 1
        ]
        threshold = _nearest_rank_percentile(
            reference_means,
            spec.funding_crowding_percentile,
        )
        return current_mean > threshold


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
    if candidate == FUNDING_FILTERED_DUAL_TREND_CANDIDATE:
        if funding_dataset is None:
            raise ValueError("资金费率过滤候选必须绑定冻结的资金费率数据集")
        if funding_dataset.manifest.symbol not in config.market_data.symbols:
            raise ValueError("资金费率品种必须在 MarketDataPolicy 中显式登记")
        spec = LongOnlyFundingFilteredDualTrendSpec(
            funding_dataset_id=funding_dataset.manifest.dataset_id,
            funding_observations_hash=funding_dataset.manifest.observations_hash,
            symbol=funding_dataset.manifest.symbol,
        )
        effective = config.model_copy(
            update={
                "market_data": config.market_data.model_copy(
                    update={
                        "version": "binance-public-daily-funding-research-v1",
                        "symbols": (spec.symbol,),
                        "interval": spec.interval,
                        "bar_window": spec.required_bar_window,
                    }
                ),
                "feature": config.feature.model_copy(
                    update={
                        "version": "daily-funding-feature-research-v1",
                        "volatility_window": spec.atr_bars,
                    }
                ),
                "frequency": config.frequency.model_copy(
                    update={
                        "version": "five-day-entry-funding-research-v1",
                        "cooldown_minutes": spec.cooldown_minutes,
                        "maximum_orders_per_day": 1,
                    }
                ),
            }
        )
        return effective, LongOnlyFundingFilteredDualTrendStrategy(
            spec,
            funding_dataset,
        )
    raise ValueError(f"未知或已退役的历史候选: {candidate}")


def _average_true_range(bars: tuple[MarketBar, ...], window: int) -> Decimal:
    selected = bars[-window:]
    previous_close = bars[-window - 1].close
    ranges: list[Decimal] = []
    for bar in selected:
        ranges.append(
            max(
                bar.high - bar.low,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
            )
        )
        previous_close = bar.close
    return sum(ranges, Decimal("0")) / Decimal(window)


def _nearest_rank_percentile(
    values: tuple[Decimal, ...],
    percentile: Decimal,
) -> Decimal:
    ordered = sorted(values)
    rank = int(
        (percentile * Decimal(len(ordered))).to_integral_value(
            rounding=ROUND_CEILING
        )
    )
    return ordered[max(0, rank - 1)]
