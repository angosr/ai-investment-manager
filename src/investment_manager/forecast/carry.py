from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from investment_manager.forecast.models import (
    BaseForecast,
    CalibratedForecast,
    DirectionalView,
    ExposureDirection,
    ForecastLeg,
    ForecastQuantityMode,
    ForecastReferencePrice,
    ForecastRole,
    ForecastTarget,
)
from investment_manager.forecast.policy import CarryEvidencePolicy, CarryForecastPolicy
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.market.models import InstrumentId, InstrumentProduct
from investment_manager.market.repository import MarketDataStore

_BPS = Decimal("10000")


@dataclass(slots=True)
class CarryForecastProducer:
    """Create one monthly BTC spot/perpetual carry hypothesis without capital authority."""

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
        slot_start, slot_end = self._slot(requested_at)
        available_at = max(require_utc(self.clock()), requested_at)
        entry_window_end = slot_start + timedelta(
            minutes=self.policy.maximum_monthly_entry_delay_minutes
        )
        if available_at >= entry_window_end:
            # A persisted monthly forecast is evidence for the frozen decision,
            # not permission to catch up later in the month.
            return None
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
            valid_until=slot_end,
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
            ),
            quantity_mode=ForecastQuantityMode.SAME_BASE_QUANTITY,
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
        slot_start = as_of.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if slot_start.month == 12:
            slot_end = slot_start.replace(year=slot_start.year + 1, month=1)
        else:
            slot_end = slot_start.replace(month=slot_start.month + 1)
        return slot_start, slot_end

    @staticmethod
    def _fresh(
        *,
        observed_at: datetime,
        expected_at: datetime,
        maximum_age_seconds: int,
    ) -> bool:
        age = expected_at - observed_at
        return timedelta(0) <= age <= timedelta(seconds=maximum_age_seconds)


@dataclass(slots=True)
class ReleasedCarryForecastProducer:
    """Project a governed Shadow carry evaluation into the shared Forecast ledger."""

    base: CarryForecastProducer
    evidence: CarryEvidencePolicy
    store: SqlForecastStore

    def produce(self, *, as_of: datetime) -> CalibratedForecast | None:
        base = self.base.produce(as_of=as_of)
        if base is None:
            return None
        if not self.evidence.valid_from <= base.available_at < self.evidence.valid_until:
            return None
        horizon_fraction = Decimal(base.horizon_minutes) / Decimal("525960")
        expected_net_bps = (
            self.evidence.expected_annualized_net_fraction
            * horizon_fraction
            * _BPS
        )
        conservative_net_bps = (
            self.evidence.conservative_annualized_net_fraction
            * horizon_fraction
            * _BPS
        )
        payload = {
            "role": ForecastRole.PROGRAM_BASE,
            "producer_id": base.producer_id,
            "producer_version": f"{base.producer_version}:{self.evidence.version}",
            "forecast_family": base.forecast_family,
            "target": base.target,
            "horizon_minutes": base.horizon_minutes,
            "direction": DirectionalView.UP,
            "reference_prices": base.reference_prices,
            "expected_edge_half_life_seconds": (
                self.base.policy.expected_edge_half_life_seconds
            ),
            "available_at": base.available_at,
            "valid_until": base.valid_until,
            "base_forecast_id": base.forecast_id,
            "expected_gross_bps": (
                expected_net_bps + self.evidence.round_trip_cost_bps
            ),
            "conservative_gross_bps": (
                conservative_net_bps + self.evidence.round_trip_cost_bps
            ),
            "dispersion_bps": expected_net_bps - conservative_net_bps,
            "calibration_ref": self.evidence.source_evaluation_id,
            "calibration_sample_size": self.evidence.independent_sample_count,
            "non_overlapping_sample_size": self.evidence.independent_sample_count,
            "input_refs": tuple(
                sorted(
                    (
                        base.forecast_id,
                        self.evidence.source_evaluation_id,
                        self.evidence.source_result_hash,
                    )
                )
            ),
        }
        forecast = CalibratedForecast(
            forecast_id=stable_id("calibrated_forecast", content_hash(payload)),
            **payload,
        )
        self.store.record(forecast)
        return forecast
