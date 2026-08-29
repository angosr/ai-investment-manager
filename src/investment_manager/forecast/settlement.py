from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise

from investment_manager.forecast.contracts import (
    ForecastContract,
    ForecastDecisionSlot,
)
from investment_manager.forecast.models import ExposureDirection, ForecastLeg
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.forecast.results import (
    ForecastLegOutcome,
    ForecastOutcome,
    ForecastOutcomeStatus,
)
from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.market.models import InstrumentProduct
from investment_manager.market.repository import MarketDataStore

_BPS = Decimal("10000")


@dataclass(frozen=True, slots=True)
class ForecastSettlementResult:
    settled: int = 0
    outcome_unavailable: int = 0
    pending: int = 0


class MarketFactsIncomplete(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ForecastPayoffResolver:
    """Resolve one product leg from point-in-time executable market facts."""

    market: MarketDataStore
    maximum_spot_age_seconds: int
    maximum_perpetual_age_seconds: int
    maximum_funding_gap_hours: int

    def leg_outcome(
        self,
        *,
        leg: ForecastLeg,
        reference_price: Decimal,
        outcome_start_at: datetime,
        evaluation_at: datetime,
        settled_at: datetime,
        settlement_rule: str = "cutoff-executable-v1",
    ) -> ForecastLegOutcome:
        if settlement_rule == "spot-midpoint-return-v1":
            if leg.instrument.product != InstrumentProduct.SPOT:
                raise ValueError("Spot midpoint Outcome 不接受衍生品 Leg")
            exit_price = self.spot_midpoint(
                leg=leg,
                evaluation_at=evaluation_at,
                visible_at=settled_at,
            )
        else:
            exit_price = self.executable_exit_price(
                leg=leg,
                evaluation_at=evaluation_at,
                visible_at=settled_at,
            )
        sign = Decimal("1") if leg.direction == ExposureDirection.LONG else Decimal("-1")
        price_return = (
            sign
            * leg.gross_weight
            * (exit_price / reference_price - Decimal("1"))
            * _BPS
        )
        funding_return = Decimal("0")
        funding_ids: tuple[str, ...] = ()
        if leg.instrument.product != InstrumentProduct.SPOT:
            settlements = self.market.funding_settlements(
                instrument=leg.instrument,
                start=outcome_start_at,
                end=evaluation_at,
                visible_at=settled_at,
            )
            self.require_complete_funding(
                start=outcome_start_at,
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

    def spot_midpoint(
        self,
        *,
        leg: ForecastLeg,
        evaluation_at: datetime,
        visible_at: datetime,
    ) -> Decimal:
        quote = self.market.latest_spot_quote(
            instrument=leg.instrument,
            evaluation_at=evaluation_at,
            visible_at=visible_at,
        )
        if quote is None or not self.fresh(
            observed_at=quote.observed_at,
            expected_at=evaluation_at,
            maximum_age_seconds=self.maximum_spot_age_seconds,
        ):
            raise MarketFactsIncomplete
        return (quote.bid + quote.ask) / 2

    def executable_reference_price(
        self,
        *,
        leg: ForecastLeg,
        outcome_start_at: datetime,
        visible_at: datetime,
    ) -> Decimal:
        if leg.instrument.product == InstrumentProduct.SPOT:
            quote = self.market.latest_spot_quote(
                instrument=leg.instrument,
                evaluation_at=outcome_start_at,
                visible_at=visible_at,
            )
            if quote is None or not self.fresh(
                observed_at=quote.observed_at,
                expected_at=outcome_start_at,
                maximum_age_seconds=self.maximum_spot_age_seconds,
            ):
                raise MarketFactsIncomplete
            return quote.ask if leg.direction == ExposureDirection.LONG else quote.bid
        quote = self.market.latest_perpetual_quote(
            instrument=leg.instrument,
            evaluation_at=outcome_start_at,
            visible_at=visible_at,
        )
        if quote is None or not self.fresh(
            observed_at=quote.exchange_time,
            expected_at=outcome_start_at,
            maximum_age_seconds=self.maximum_perpetual_age_seconds,
        ):
            raise MarketFactsIncomplete
        return quote.ask if leg.direction == ExposureDirection.LONG else quote.bid

    def executable_exit_price(
        self,
        *,
        leg: ForecastLeg,
        evaluation_at: datetime,
        visible_at: datetime,
    ) -> Decimal:
        if leg.instrument.product == InstrumentProduct.SPOT:
            quote = self.market.latest_spot_quote(
                instrument=leg.instrument,
                evaluation_at=evaluation_at,
                visible_at=visible_at,
            )
            if quote is None or not self.fresh(
                observed_at=quote.observed_at,
                expected_at=evaluation_at,
                maximum_age_seconds=self.maximum_spot_age_seconds,
            ):
                raise MarketFactsIncomplete
            return quote.bid if leg.direction == ExposureDirection.LONG else quote.ask
        quote = self.market.latest_perpetual_quote(
            instrument=leg.instrument,
            evaluation_at=evaluation_at,
            visible_at=visible_at,
        )
        if quote is None or not self.fresh(
            observed_at=quote.exchange_time,
            expected_at=evaluation_at,
            maximum_age_seconds=self.maximum_perpetual_age_seconds,
        ):
            raise MarketFactsIncomplete
        return quote.bid if leg.direction == ExposureDirection.LONG else quote.ask

    @staticmethod
    def fresh(
        *,
        observed_at: datetime,
        expected_at: datetime,
        maximum_age_seconds: int,
    ) -> bool:
        age = expected_at - observed_at
        return timedelta(0) <= age <= timedelta(seconds=maximum_age_seconds)

    def require_complete_funding(
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
            raise MarketFactsIncomplete


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
        settled = unavailable = pending = 0
        for contract, slot in self.store.pending_slots(
            evaluation_version=self.evaluation_version,
            limit=self.batch_size,
            due_at=now,
        ):
            try:
                outcome = self._outcome(
                    contract=contract,
                    slot=slot,
                    settled_at=now,
                )
            except MarketFactsIncomplete:
                if now - slot.evaluation_at < timedelta(minutes=self.settlement_grace_minutes):
                    pending += 1
                    continue
                outcome = self._unavailable(slot=slot, settled_at=now)
            inserted = int(self.store.record_outcome(outcome))
            settled += inserted * int(outcome.status == ForecastOutcomeStatus.SETTLED)
            unavailable += inserted * int(
                outcome.status == ForecastOutcomeStatus.OUTCOME_UNAVAILABLE
            )
        return ForecastSettlementResult(
            settled=settled,
            outcome_unavailable=unavailable,
            pending=pending,
        )

    def _outcome(
        self,
        *,
        contract: ForecastContract,
        slot: ForecastDecisionSlot,
        settled_at: datetime,
    ) -> ForecastOutcome:
        economic_start = slot.outcome_start_at or slot.information_cutoff_at
        cutoff_references = {item.instrument_id: item.price for item in slot.cutoff_prices}
        if slot.outcome_start_at is None and any(
            leg.instrument.key not in cutoff_references for leg in contract.target.legs
        ):
            raise MarketFactsIncomplete
        legs = tuple(
            self._leg_outcome(
                leg=leg,
                reference_price=(
                    cutoff_references[leg.instrument.key]
                    if slot.outcome_start_at is None
                    else self._executable_reference_price(
                        leg=leg,
                        outcome_start_at=economic_start,
                        visible_at=settled_at,
                    )
                ),
                outcome_start_at=economic_start,
                evaluation_at=slot.evaluation_at,
                settled_at=settled_at,
                settlement_rule=contract.settlement_rule,
            )
            for leg in contract.target.legs
        )
        gross_return = sum(
            (item.price_return_bps + item.funding_return_bps for item in legs),
            Decimal("0"),
        )
        bucket = next(
            item
            for item in contract.outcome_buckets
            if (item.lower_bps is None or gross_return >= item.lower_bps)
            and (item.upper_bps is None or gross_return < item.upper_bps)
        )
        return ForecastOutcome(
            **self._common(slot=slot, settled_at=settled_at),
            status=ForecastOutcomeStatus.SETTLED,
            legs=legs,
            gross_target_return_bps=gross_return,
            realized_bucket_id=bucket.bucket_id,
            reason_code="GROSS_TARGET_RETURN_AVAILABLE",
        )

    def _leg_outcome(
        self,
        *,
        leg: ForecastLeg,
        reference_price: Decimal,
        outcome_start_at: datetime,
        evaluation_at: datetime,
        settled_at: datetime,
        settlement_rule: str,
    ) -> ForecastLegOutcome:
        return self._resolver().leg_outcome(
            leg=leg,
            reference_price=reference_price,
            outcome_start_at=outcome_start_at,
            evaluation_at=evaluation_at,
            settled_at=settled_at,
            settlement_rule=settlement_rule,
        )

    def _executable_reference_price(
        self,
        *,
        leg: ForecastLeg,
        outcome_start_at: datetime,
        visible_at: datetime,
    ) -> Decimal:
        return self._resolver().executable_reference_price(
            leg=leg,
            outcome_start_at=outcome_start_at,
            visible_at=visible_at,
        )

    def _resolver(self) -> ForecastPayoffResolver:
        return ForecastPayoffResolver(
            market=self.market,
            maximum_spot_age_seconds=self.maximum_spot_age_seconds,
            maximum_perpetual_age_seconds=self.maximum_perpetual_age_seconds,
            maximum_funding_gap_hours=self.maximum_funding_gap_hours,
        )

    def _unavailable(
        self,
        *,
        slot: ForecastDecisionSlot,
        settled_at: datetime,
    ) -> ForecastOutcome:
        return ForecastOutcome(
            **self._common(slot=slot, settled_at=settled_at),
            status=ForecastOutcomeStatus.OUTCOME_UNAVAILABLE,
            reason_code="POINT_IN_TIME_MARKET_OR_FUNDING_FACTS_INCOMPLETE",
        )

    def _common(
        self,
        *,
        slot: ForecastDecisionSlot,
        settled_at: datetime,
    ) -> dict[str, object]:
        return {
            "outcome_id": stable_id(
                "forecast_outcome",
                slot.slot_id,
                self.evaluation_version,
            ),
            "contract_id": slot.contract_id,
            "decision_slot_id": slot.slot_id,
            "evaluation_version": self.evaluation_version,
            "information_cutoff_at": slot.information_cutoff_at,
            "outcome_start_at": slot.outcome_start_at,
            "evaluation_at": slot.evaluation_at,
            "settled_at": settled_at,
        }
