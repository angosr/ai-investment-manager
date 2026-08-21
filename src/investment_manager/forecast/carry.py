from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from investment_manager.forecast.models import (
    BaseForecast,
    DirectionalView,
    ExposureDirection,
    ForecastLeg,
    ForecastReferencePrice,
    ForecastTarget,
)
from investment_manager.forecast.policy import CarryForecastPolicy
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.market.models import InstrumentId, InstrumentProduct
from investment_manager.market.repository import MarketDataStore

_BPS = Decimal("10000")


@dataclass(slots=True)
class CarryForecastProducer:
    """Create one daily BTC spot/perpetual carry hypothesis without capital authority."""

    policy: CarryForecastPolicy
    market: MarketDataStore
    store: SqlForecastStore
    maximum_spot_age_seconds: int
    maximum_perpetual_age_seconds: int
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    _cached_slot_start: datetime | None = field(default=None, init=False)
    _cached_forecast: BaseForecast | None = field(default=None, init=False)

    def produce(self, *, as_of: datetime) -> BaseForecast | None:
        if not self.policy.enabled:
            return None
        requested_at = require_utc(as_of)
        slot_start, _ = self._slot(requested_at)
        if self._cached_slot_start == slot_start:
            return self._cached_forecast
        target = self._target()
        forecast_id = stable_id(
            "base_forecast",
            self.policy.producer_id,
            self.policy.version,
            target.target_id,
            slot_start,
        )
        existing = self.store.forecast(forecast_id)
        if existing is not None:
            if not isinstance(existing, BaseForecast):
                raise ValueError("Carry Forecast slot 已被非 BaseForecast 占用")
            self._cached_slot_start = slot_start
            self._cached_forecast = existing
            return existing

        available_at = max(require_utc(self.clock()), requested_at)
        spot, perpetual = (item.instrument for item in target.legs)
        spot_quote = self.market.latest_spot_quote(
            instrument=spot,
            evaluation_at=available_at,
            visible_at=available_at,
        )
        perpetual_quote = self.market.latest_perpetual_quote(
            instrument=perpetual,
            evaluation_at=available_at,
            visible_at=available_at,
        )
        state = self.market.latest_perpetual_state(
            instrument=perpetual,
            as_of=available_at,
        )
        if spot_quote is None or perpetual_quote is None or state is None:
            raise ValueError("Carry Forecast 缺少 Spot/Perpetual 点时行情")
        if not self._fresh(
            observed_at=spot_quote.observed_at,
            expected_at=available_at,
            maximum_age_seconds=self.maximum_spot_age_seconds,
        ):
            raise ValueError("Carry Forecast Spot 报价过期")
        if not all(
            self._fresh(
                observed_at=item,
                expected_at=available_at,
                maximum_age_seconds=self.maximum_perpetual_age_seconds,
            )
            for item in (perpetual_quote.exchange_time, state.exchange_time)
        ):
            raise ValueError("Carry Forecast Perpetual 行情过期")

        spot_entry = spot_quote.ask
        perpetual_entry = perpetual_quote.bid
        expected_gross_bps = self._expected_gross_bps(
            spot_entry=spot_entry,
            perpetual_entry=perpetual_entry,
            mark_price=state.mark_price,
            last_funding_rate=state.last_funding_rate,
        )
        direction = (
            DirectionalView.UP
            if expected_gross_bps > 0
            else DirectionalView.DOWN
            if expected_gross_bps < 0
            else DirectionalView.UNCERTAIN
        )
        forecast = BaseForecast(
            forecast_id=forecast_id,
            producer_id=self.policy.producer_id,
            producer_version=self.policy.version,
            forecast_family=self.policy.forecast_family,
            target=target,
            horizon_minutes=self.policy.horizon_minutes,
            direction=direction,
            reference_prices=(
                ForecastReferencePrice(
                    instrument_id=spot.key,
                    price=spot_entry,
                ),
                ForecastReferencePrice(
                    instrument_id=perpetual.key,
                    price=perpetual_entry,
                ),
            ),
            observed_at=max(
                spot_quote.observed_at,
                perpetual_quote.observed_at,
                state.observed_at,
            ),
            available_at=available_at,
            valid_until=available_at + timedelta(minutes=self.policy.production_interval_minutes),
            raw_score=expected_gross_bps,
            input_refs=tuple(
                sorted(
                    (
                        spot_quote.quote_id,
                        perpetual_quote.quote_id,
                        state.state_id,
                    )
                )
            ),
            unknowns=(
                "FUTURE_FUNDING_RATE_PATH",
                "EXIT_BASIS_AND_SPREAD",
                "VENUE_AND_MARGIN_STRESS",
            ),
        )
        self.store.record(forecast)
        self._cached_slot_start = slot_start
        self._cached_forecast = forecast
        return forecast

    def _target(self) -> ForecastTarget:
        spot = InstrumentId.binance_spot(
            symbol=self.policy.symbol,
            base_asset=self.policy.base_asset,
            quote_asset=self.policy.quote_asset,
        )
        perpetual = InstrumentId(
            product=InstrumentProduct.USD_M_PERPETUAL,
            symbol=self.policy.symbol,
            base_asset=self.policy.base_asset,
            quote_asset=self.policy.quote_asset,
            settlement_asset=self.policy.quote_asset,
        )
        return ForecastTarget.create(
            (
                ForecastLeg(
                    instrument=spot,
                    direction=ExposureDirection.LONG,
                    gross_weight=Decimal("0.5"),
                ),
                ForecastLeg(
                    instrument=perpetual,
                    direction=ExposureDirection.SHORT,
                    gross_weight=Decimal("0.5"),
                ),
            )
        )

    def _expected_gross_bps(
        self,
        *,
        spot_entry: Decimal,
        perpetual_entry: Decimal,
        mark_price: Decimal,
        last_funding_rate: Decimal,
    ) -> Decimal:
        funding_periods = Decimal(self.policy.horizon_minutes) / Decimal(
            self.policy.funding_interval_hours * 60
        )
        projected_funding = (
            Decimal("0.5")
            * last_funding_rate
            * (mark_price / perpetual_entry)
            * funding_periods
            * _BPS
        )
        basis_convergence = Decimal("0.5") * (Decimal("1") - spot_entry / perpetual_entry) * _BPS
        return projected_funding + basis_convergence

    def _slot(self, as_of: datetime) -> tuple[datetime, datetime]:
        seconds = self.policy.production_interval_minutes * 60
        slot_start = datetime.fromtimestamp(
            int(as_of.timestamp()) // seconds * seconds,
            tz=UTC,
        )
        return slot_start, slot_start + timedelta(seconds=seconds)

    @staticmethod
    def _fresh(
        *,
        observed_at: datetime,
        expected_at: datetime,
        maximum_age_seconds: int,
    ) -> bool:
        age = expected_at - observed_at
        return timedelta(0) <= age <= timedelta(seconds=maximum_age_seconds)
