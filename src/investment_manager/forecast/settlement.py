from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise

from investment_manager.forecast.models import (
    BaseForecast,
    CalibratedForecast,
    DirectionalView,
    ExposureDirection,
    ForecastLeg,
    ForecastLegOutcome,
    ForecastOutcome,
    ForecastOutcomeStatus,
)
from investment_manager.forecast.repository import (
    Forecast,
    SqlForecastStore,
    forecast_kind,
)
from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.market.models import InstrumentProduct
from investment_manager.market.repository import MarketDataStore

_BPS = Decimal("10000")


@dataclass(frozen=True, slots=True)
class ForecastSettlementResult:
    settled: int = 0
    abstained: int = 0
    unscorable: int = 0
    pending: int = 0


class _MarketFactsIncomplete(Exception):
    pass


@dataclass(slots=True)
class ForecastOutcomeSettler:
    market: MarketDataStore
    store: SqlForecastStore
    evaluation_version: str
    maximum_spot_age_seconds: int
    maximum_perpetual_age_seconds: int
    maximum_funding_gap_hours: int
    settlement_grace_minutes: int
    batch_size: int = 100

    def settle(self, *, as_of: datetime) -> ForecastSettlementResult:
        now = require_utc(as_of)
        settled = abstained = unscorable = pending = 0
        for forecast in self.store.pending(
            evaluation_version=self.evaluation_version,
            limit=self.batch_size,
            due_at=now,
        ):
            evaluation_at = forecast.available_at + timedelta(minutes=forecast.horizon_minutes)
            try:
                outcome = self._outcome(forecast=forecast, settled_at=now)
            except _MarketFactsIncomplete:
                if now - evaluation_at < timedelta(minutes=self.settlement_grace_minutes):
                    pending += 1
                    continue
                outcome = self._unscorable(forecast=forecast, settled_at=now)
            inserted = int(self.store.record_outcome(outcome))
            settled += inserted * int(outcome.status == ForecastOutcomeStatus.SETTLED)
            abstained += inserted * int(outcome.status == ForecastOutcomeStatus.ABSTAINED)
            unscorable += inserted * int(outcome.status == ForecastOutcomeStatus.UNSCORABLE)
        return ForecastSettlementResult(
            settled=settled,
            abstained=abstained,
            unscorable=unscorable,
            pending=pending,
        )

    def _outcome(
        self,
        *,
        forecast: Forecast,
        settled_at: datetime,
    ) -> ForecastOutcome:
        references = {item.instrument_id: item.price for item in forecast.reference_prices}
        evaluation_at = forecast.available_at + timedelta(minutes=forecast.horizon_minutes)
        legs = tuple(
            self._leg_outcome(
                leg=leg,
                reference_price=references[leg.instrument.key],
                available_at=forecast.available_at,
                evaluation_at=evaluation_at,
                settled_at=settled_at,
            )
            for leg in forecast.target.legs
        )
        gross_return = sum(
            (item.price_return_bps + item.funding_return_bps for item in legs),
            Decimal("0"),
        )
        common = self._common(forecast=forecast, settled_at=settled_at)
        if forecast.direction == DirectionalView.UNCERTAIN:
            return ForecastOutcome(
                **common,
                status=ForecastOutcomeStatus.ABSTAINED,
                legs=legs,
                gross_target_return_bps=gross_return,
                reason_code="FORECAST_ABSTAINED",
            )
        directional_return = (
            gross_return if forecast.direction == DirectionalView.UP else -gross_return
        )
        return ForecastOutcome(
            **common,
            status=ForecastOutcomeStatus.SETTLED,
            legs=legs,
            gross_target_return_bps=gross_return,
            directional_return_bps=directional_return,
            reason_code="GROSS_TARGET_RETURN_AVAILABLE",
        )

    def _leg_outcome(
        self,
        *,
        leg: ForecastLeg,
        reference_price: Decimal,
        available_at: datetime,
        evaluation_at: datetime,
        settled_at: datetime,
    ) -> ForecastLegOutcome:
        entry_price, _ = self._executable_price(
            leg=leg,
            evaluation_at=available_at,
            visible_at=available_at,
            entering=True,
        )
        if entry_price != reference_price:
            raise _MarketFactsIncomplete
        exit_price, _ = self._executable_price(
            leg=leg,
            evaluation_at=evaluation_at,
            visible_at=settled_at,
            entering=False,
        )
        sign = Decimal("1") if leg.direction == ExposureDirection.LONG else Decimal("-1")
        price_return = (
            sign * leg.gross_weight * (exit_price / reference_price - Decimal("1")) * _BPS
        )
        funding_return = Decimal("0")
        funding_ids: tuple[str, ...] = ()
        if leg.instrument.product != InstrumentProduct.SPOT:
            settlements = self.market.funding_settlements(
                instrument=leg.instrument,
                start=available_at,
                end=evaluation_at,
                visible_at=settled_at,
            )
            self._require_complete_funding(
                start=available_at,
                end=evaluation_at,
                settlement_times=tuple(item.funding_time for item in settlements),
            )
            funding_return = sum(
                (
                    -sign
                    * leg.gross_weight
                    * item.funding_rate
                    * (item.mark_price / reference_price)
                    * _BPS
                    for item in settlements
                ),
                Decimal("0"),
            )
            funding_ids = tuple(item.settlement_id for item in settlements)
        return ForecastLegOutcome(
            instrument_id=leg.instrument.key,
            direction=leg.direction,
            gross_weight=leg.gross_weight,
            reference_price=reference_price,
            exit_price=exit_price,
            price_return_bps=price_return,
            funding_return_bps=funding_return,
            funding_settlement_ids=funding_ids,
        )

    def _executable_price(
        self,
        *,
        leg: ForecastLeg,
        evaluation_at: datetime,
        visible_at: datetime,
        entering: bool,
    ) -> tuple[Decimal, datetime]:
        if leg.instrument.product == InstrumentProduct.SPOT:
            quote = self.market.latest_spot_quote(
                instrument=leg.instrument,
                evaluation_at=evaluation_at,
                visible_at=visible_at,
            )
            if quote is None or not self._fresh(
                observed_at=quote.observed_at,
                expected_at=evaluation_at,
                maximum_age_seconds=self.maximum_spot_age_seconds,
            ):
                raise _MarketFactsIncomplete
            price = self._side_price(
                bid=quote.bid,
                ask=quote.ask,
                direction=leg.direction,
                entering=entering,
            )
            return price, quote.observed_at
        quote = self.market.latest_perpetual_quote(
            instrument=leg.instrument,
            evaluation_at=evaluation_at,
            visible_at=visible_at,
        )
        if quote is None or not self._fresh(
            observed_at=quote.exchange_time,
            expected_at=evaluation_at,
            maximum_age_seconds=self.maximum_perpetual_age_seconds,
        ):
            raise _MarketFactsIncomplete
        price = self._side_price(
            bid=quote.bid,
            ask=quote.ask,
            direction=leg.direction,
            entering=entering,
        )
        return price, quote.exchange_time

    @staticmethod
    def _side_price(
        *,
        bid: Decimal,
        ask: Decimal,
        direction: ExposureDirection,
        entering: bool,
    ) -> Decimal:
        if direction == ExposureDirection.LONG:
            return ask if entering else bid
        return bid if entering else ask

    @staticmethod
    def _fresh(
        *,
        observed_at: datetime,
        expected_at: datetime,
        maximum_age_seconds: int,
    ) -> bool:
        age = expected_at - observed_at
        return timedelta(0) <= age <= timedelta(seconds=maximum_age_seconds)

    def _require_complete_funding(
        self,
        *,
        start: datetime,
        end: datetime,
        settlement_times: tuple[datetime, ...],
    ) -> None:
        maximum_gap = timedelta(hours=self.maximum_funding_gap_hours)
        if end - start < maximum_gap and not settlement_times:
            return
        points = (start, *settlement_times, end)
        if any(right - left > maximum_gap for left, right in pairwise(points)):
            raise _MarketFactsIncomplete

    def _unscorable(
        self,
        *,
        forecast: Forecast,
        settled_at: datetime,
    ) -> ForecastOutcome:
        return ForecastOutcome(
            **self._common(forecast=forecast, settled_at=settled_at),
            status=ForecastOutcomeStatus.UNSCORABLE,
            reason_code="POINT_IN_TIME_MARKET_OR_FUNDING_FACTS_INCOMPLETE",
        )

    def _common(
        self,
        *,
        forecast: BaseForecast | CalibratedForecast,
        settled_at: datetime,
    ) -> dict[str, object]:
        return {
            "outcome_id": stable_id(
                "forecast_outcome",
                forecast.forecast_id,
                self.evaluation_version,
            ),
            "forecast_id": forecast.forecast_id,
            "forecast_kind": forecast_kind(forecast),
            "producer_id": forecast.producer_id,
            "producer_version": forecast.producer_version,
            "target_id": forecast.target.target_id,
            "direction": forecast.direction,
            "horizon_minutes": forecast.horizon_minutes,
            "evaluation_version": self.evaluation_version,
            "available_at": forecast.available_at,
            "evaluation_at": forecast.available_at + timedelta(minutes=forecast.horizon_minutes),
            "settled_at": settled_at,
        }
