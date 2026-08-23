"""Deterministic opportunity producers used by the capital path."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from investment_manager.forecast.models import (
    BaseForecast,
    DirectionalView,
    ExposureDirection,
    ForecastLeg,
    ForecastQuantityMode,
    ForecastReferencePrice,
    ForecastTarget,
)
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.market.models import InstrumentId, InstrumentProduct
from investment_manager.market.repository import MarketDataStore
from investment_manager.portfolio.policy import CashCarryProgramPolicy


@dataclass(frozen=True, slots=True)
class CashCarryForecastProducer:
    """Emit an experimental delta-neutral opportunity only when net edge is positive.

    The estimate is deliberately mechanical: executable short basis plus a
    conservative projection of visible funding history.  It is a Mock hypothesis,
    not a calibration claim; Portfolio enforces the separate authorization.
    """

    policy: CashCarryProgramPolicy
    market: MarketDataStore
    forecasts: SqlForecastStore
    spot: InstrumentId
    perpetual: InstrumentId
    minimum_entry_net_bps: Decimal

    def __post_init__(self) -> None:
        if self.spot.product != InstrumentProduct.SPOT:
            raise ValueError("CashCarry Program 的 spot Instrument 非现货")
        if self.perpetual.product == InstrumentProduct.SPOT:
            raise ValueError("CashCarry Program 的 perpetual Instrument 非衍生品")
        if (
            self.spot.symbol != self.perpetual.symbol
            or self.spot.base_asset != self.perpetual.base_asset
            or self.spot.quote_asset != self.perpetual.quote_asset
        ):
            raise ValueError("CashCarry Program 两腿必须属于同一交易对")

    def produce(self, *, as_of: datetime) -> BaseForecast | None:
        as_of = require_utc(as_of)
        state = self.market.latest_perpetual_state(
            instrument=self.perpetual,
            as_of=as_of,
        )
        if state is None:
            return None
        perpetual_quote = self.market.latest_perpetual_quote(
            instrument=self.perpetual,
            evaluation_at=as_of,
            visible_at=as_of,
        )
        if perpetual_quote is None:
            return None
        spot_quote = self.market.latest_spot_quote(
            instrument=self.spot,
            evaluation_at=perpetual_quote.observed_at,
            visible_at=as_of,
        )
        if spot_quote is None:
            return None
        window_start = as_of - timedelta(hours=self.policy.funding_lookback_hours)
        settlements = self.market.funding_settlements(
            instrument=self.perpetual,
            start=window_start,
            end=as_of,
            visible_at=as_of,
        )
        if len(settlements) < self.policy.minimum_funding_samples:
            return None
        rates_bps = tuple(item.funding_rate * Decimal("10000") for item in settlements)
        mean_funding_bps = sum(rates_bps, Decimal("0")) / Decimal(len(rates_bps))
        positive_fraction = Decimal(sum(rate > 0 for rate in rates_bps)) / Decimal(
            len(rates_bps)
        )
        projected_settlements = (
            Decimal(self.policy.horizon_hours)
            * Decimal(len(rates_bps))
            / Decimal(self.policy.funding_lookback_hours)
        )
        credited_mean = (
            mean_funding_bps * self.policy.funding_projection_haircut
            if mean_funding_bps > 0
            and positive_fraction >= self.policy.minimum_positive_funding_fraction
            else min(mean_funding_bps, Decimal("0"))
        )
        executable_basis_bps = (
            perpetual_quote.bid / spot_quote.ask - Decimal("1")
        ) * Decimal("10000")
        gross_edge_bps = executable_basis_bps + credited_mean * projected_settlements
        if (
            gross_edge_bps - self.policy.estimated_variable_cost_bps
            < self.minimum_entry_net_bps
        ):
            return None

        target = self._target()
        input_refs = tuple(
            sorted(
                {
                    state.state_id,
                    perpetual_quote.quote_id,
                    spot_quote.quote_id,
                    *(item.settlement_id for item in settlements),
                }
            )
        )
        forecast_id = stable_id(
            "base_forecast",
            self.policy.producer_id,
            self.policy.producer_version,
            target.target_id,
            as_of.isoformat(),
            content_hash(input_refs),
            str(gross_edge_bps),
        )
        existing = self.forecasts.forecast(forecast_id)
        if existing is not None:
            if not isinstance(existing, BaseForecast):
                raise ValueError("CashCarry Forecast 身份与已有类型冲突")
            return existing
        forecast = BaseForecast(
            forecast_id=forecast_id,
            producer_id=self.policy.producer_id,
            producer_version=self.policy.producer_version,
            forecast_family=self.policy.forecast_family,
            target=target,
            horizon_minutes=self.policy.horizon_hours * 60,
            direction=DirectionalView.UP,
            reference_prices=(
                ForecastReferencePrice(
                    instrument_id=self.spot.key,
                    price=spot_quote.ask,
                ),
                ForecastReferencePrice(
                    instrument_id=self.perpetual.key,
                    price=perpetual_quote.bid,
                ),
            ),
            observed_at=max(
                state.observed_at,
                spot_quote.observed_at,
                perpetual_quote.observed_at,
            ),
            available_at=as_of,
            valid_until=as_of + timedelta(minutes=self.policy.entry_validity_minutes),
            raw_score=gross_edge_bps,
            input_refs=input_refs,
            unknowns=(
                "EXPERIMENTAL_EDGE_NOT_CALIBRATED",
                "BASIS_CONVERGENCE_BY_HORIZON_ASSUMED",
            ),
        )
        self.forecasts.record(forecast)
        return forecast

    def _target(self) -> ForecastTarget:
        return ForecastTarget.create(
            (
                ForecastLeg(
                    instrument=self.spot,
                    direction=ExposureDirection.LONG,
                    gross_weight=Decimal("0.5"),
                ),
                ForecastLeg(
                    instrument=self.perpetual,
                    direction=ExposureDirection.SHORT,
                    gross_weight=Decimal("0.5"),
                ),
            ),
            quantity_mode=ForecastQuantityMode.SAME_BASE_QUANTITY,
        )
