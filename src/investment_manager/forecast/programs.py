"""Deterministic, continuously evaluated Program Forecast sources."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import NormalDist

from investment_manager.forecast.contract_repository import SqlForecastContractStore
from investment_manager.forecast.contracts import (
    ForecastContract,
    ForecastDecisionSlot,
    ForecastNoEstimate,
    ForecastNoEstimateReason,
    ForecastPriceAnchor,
    ForecastProducerBinding,
    ForecastProducerKind,
)
from investment_manager.forecast.models import (
    ExposureDirection,
    ForecastLeg,
    ForecastQuantityMode,
    ForecastTarget,
)
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.forecast.results import (
    BaseForecast,
    ForecastBucketProbability,
)
from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.market.models import InstrumentId, InstrumentProduct
from investment_manager.market.repository import MarketDataStore
from investment_manager.portfolio.policy import CashCarryProgramPolicy

ForecastProductionResult = BaseForecast | ForecastNoEstimate


def cash_carry_target(
    *,
    spot: InstrumentId,
    perpetual: InstrumentId,
) -> ForecastTarget:
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


@dataclass(frozen=True, slots=True)
class CashCarryForecastProducer:
    """Estimate the contract payoff on every slot; Portfolio alone decides entry."""

    policy: CashCarryProgramPolicy
    contract: ForecastContract
    binding: ForecastProducerBinding
    market: MarketDataStore
    contracts: SqlForecastContractStore
    forecasts: SqlForecastStore
    spot: InstrumentId
    perpetual: InstrumentId

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
        if self.binding.producer_kind != ForecastProducerKind.PROGRAM:
            raise ValueError("CashCarry 必须绑定 PROGRAM Producer")
        if self.binding.contract_id != self.contract.contract_id:
            raise ValueError("CashCarry ProducerBinding 与 ForecastContract 不一致")
        expected_legs = (
            (self.spot.key, ExposureDirection.LONG),
            (self.perpetual.key, ExposureDirection.SHORT),
        )
        observed_legs = tuple(
            (item.instrument.key, item.direction) for item in self.contract.target.legs
        )
        if observed_legs != expected_legs:
            raise ValueError("CashCarry ForecastContract 必须是现货多头/永续空头")

    def produce(self, *, as_of: datetime) -> ForecastProductionResult:
        slot_as_of = require_utc(as_of)
        self.contracts.record_contract(self.contract)
        self.contracts.record_binding(self.binding)
        slot_id = ForecastDecisionSlot.identity_for(self.contract.contract_id, slot_as_of)
        existing = self.forecasts.result_for_behavior(
            decision_slot_id=slot_id,
            producer_behavior_id=self.binding.producer_behavior_id,
        )
        if existing is not None:
            return existing
        absence_id = stable_id(
            "forecast_no_estimate",
            slot_id,
            self.binding.producer_behavior_id,
        )
        existing_absence = self.contracts.no_estimate(absence_id)
        if existing_absence is not None:
            return existing_absence

        state = self.market.latest_perpetual_state(
            instrument=self.perpetual,
            as_of=slot_as_of,
        )
        perpetual_quote = self.market.latest_perpetual_quote(
            instrument=self.perpetual,
            evaluation_at=slot_as_of,
            visible_at=slot_as_of,
        )
        spot_quote = (
            self.market.latest_spot_quote(
                instrument=self.spot,
                evaluation_at=perpetual_quote.observed_at,
                visible_at=slot_as_of,
            )
            if perpetual_quote is not None
            else None
        )
        cutoff_prices = self._price_anchors(
            anchored_at=slot_as_of,
            spot_quote=spot_quote,
            perpetual_quote=perpetual_quote,
        )
        slot = self.contracts.slot(slot_id)
        if slot is None:
            slot = ForecastDecisionSlot.create(
                self.contract,
                slot_as_of=slot_as_of,
                cutoff_prices=cutoff_prices,
            )
            self.contracts.record_slot(slot)
        elif slot.cutoff_prices != cutoff_prices:
            raise ValueError("CashCarry decision slot 已绑定不同 cutoff prices")

        if state is None or perpetual_quote is None or spot_quote is None:
            return self._no_estimate(
                slot=slot,
                reason=ForecastNoEstimateReason.MARKET_INPUT_INVALID,
                completed_at=slot_as_of,
                input_refs=tuple(
                    sorted(
                        {
                            *(item.quote_ref for item in cutoff_prices),
                            *((state.state_id,) if state is not None else ()),
                        }
                    )
                ),
            )

        window_start = slot_as_of - timedelta(hours=self.policy.funding_lookback_hours)
        settlements = self.market.funding_settlements(
            instrument=self.perpetual,
            start=window_start,
            end=slot_as_of,
            visible_at=slot_as_of,
        )
        if len(settlements) < self.policy.minimum_funding_samples:
            return self._no_estimate(
                slot=slot,
                reason=ForecastNoEstimateReason.REQUIRED_FEATURE_MISSING,
                completed_at=slot_as_of,
                input_refs=tuple(
                    sorted(
                        {
                            state.state_id,
                            *(item.quote_ref for item in cutoff_prices),
                            *(item.settlement_id for item in settlements),
                        }
                    )
                ),
                detail="INSUFFICIENT_POINT_IN_TIME_FUNDING_HISTORY",
            )

        input_refs = tuple(
            sorted(
                {
                    state.state_id,
                    *(item.quote_ref for item in cutoff_prices),
                    *(item.settlement_id for item in settlements),
                }
            )
        )
        gross_center_bps = self._gross_center_bps(
            spot_ask=spot_quote.ask,
            perpetual_bid=perpetual_quote.bid,
            funding_rates=tuple(item.funding_rate for item in settlements),
        )
        probabilities = self._probabilities(gross_center_bps)
        expected_gross_bps = sum(
            (
                probability.probability * bucket.representative_bps
                for probability, bucket in zip(
                    probabilities,
                    self.contract.outcome_buckets,
                    strict=True,
                )
            ),
            Decimal("0"),
        )
        available_at = slot_as_of
        valid_until = min(
            slot.evaluation_at,
            available_at + timedelta(minutes=self.contract.validity_minutes),
        )
        forecast = BaseForecast(
            forecast_id=stable_id(
                "base_forecast",
                slot.slot_id,
                self.binding.producer_behavior_id,
            ),
            contract_id=self.contract.contract_id,
            decision_slot_id=slot.slot_id,
            producer_id=self.binding.producer_id,
            producer_behavior_id=self.binding.producer_behavior_id,
            outcome_family_id=self.contract.outcome_family_id,
            target=self.contract.target,
            horizon_minutes=self.contract.horizon_minutes,
            cutoff_prices=slot.cutoff_prices,
            entry_prices=tuple(
                item.model_copy(update={"available_at": available_at})
                for item in slot.cutoff_prices
            ),
            information_cutoff_at=slot.information_cutoff_at,
            input_observed_at=max(
                state.observed_at,
                spot_quote.observed_at,
                perpetual_quote.observed_at,
                *(item.observed_at for item in settlements),
            ),
            available_at=available_at,
            valid_until=valid_until,
            outcome_probabilities=probabilities,
            expected_gross_bps=expected_gross_bps,
            input_refs=input_refs,
        )
        self.forecasts.record(forecast)
        return forecast

    def _gross_center_bps(
        self,
        *,
        spot_ask: Decimal,
        perpetual_bid: Decimal,
        funding_rates: tuple[Decimal, ...],
    ) -> Decimal:
        rates_bps = tuple(item * Decimal("10000") for item in funding_rates)
        mean_funding_bps = sum(rates_bps, Decimal("0")) / Decimal(len(rates_bps))
        positive_fraction = Decimal(sum(rate > 0 for rate in rates_bps)) / Decimal(len(rates_bps))
        projected_settlements = (
            Decimal(self.contract.horizon_minutes)
            * Decimal(len(rates_bps))
            / Decimal(self.policy.funding_lookback_hours * 60)
        )
        credited_mean = (
            mean_funding_bps * self.policy.funding_projection_haircut
            if mean_funding_bps > 0
            and positive_fraction >= self.policy.minimum_positive_funding_fraction
            else min(mean_funding_bps, Decimal("0"))
        )
        executable_basis_bps = (perpetual_bid / spot_ask - Decimal("1")) * Decimal("10000")
        return executable_basis_bps + credited_mean * projected_settlements

    def _probabilities(
        self,
        expected_bps: Decimal,
    ) -> tuple[ForecastBucketProbability, ...]:
        distribution = NormalDist(
            mu=float(expected_bps),
            sigma=float(self.policy.forecast_dispersion_bps),
        )
        values: list[Decimal] = []
        cumulative = Decimal("0")
        for index, bucket in enumerate(self.contract.outcome_buckets):
            if index == len(self.contract.outcome_buckets) - 1:
                probability = Decimal("1") - cumulative
            else:
                assert bucket.upper_bps is not None
                upper = Decimal(str(distribution.cdf(float(bucket.upper_bps))))
                probability = upper - cumulative
                cumulative = upper
            values.append(max(Decimal("0"), min(Decimal("1"), probability)))
        correction = Decimal("1") - sum(values, Decimal("0"))
        values[-1] += correction
        return tuple(
            ForecastBucketProbability(
                bucket_id=bucket.bucket_id,
                probability=probability,
            )
            for bucket, probability in zip(
                self.contract.outcome_buckets,
                values,
                strict=True,
            )
        )

    def _price_anchors(
        self,
        *,
        anchored_at: datetime,
        spot_quote,
        perpetual_quote,
    ) -> tuple[ForecastPriceAnchor, ...]:
        if spot_quote is None or perpetual_quote is None:
            return ()
        return (
            ForecastPriceAnchor(
                instrument_id=self.spot.key,
                price=spot_quote.ask,
                observed_at=spot_quote.observed_at,
                available_at=anchored_at,
                quote_ref=spot_quote.quote_id,
            ),
            ForecastPriceAnchor(
                instrument_id=self.perpetual.key,
                price=perpetual_quote.bid,
                observed_at=perpetual_quote.exchange_time,
                available_at=anchored_at,
                quote_ref=perpetual_quote.quote_id,
            ),
        )

    def _no_estimate(
        self,
        *,
        slot: ForecastDecisionSlot,
        reason: ForecastNoEstimateReason,
        completed_at: datetime,
        input_refs: tuple[str, ...],
        detail: str | None = None,
    ) -> ForecastNoEstimate:
        result = ForecastNoEstimate(
            result_id=stable_id(
                "forecast_no_estimate",
                slot.slot_id,
                self.binding.producer_behavior_id,
            ),
            slot_id=slot.slot_id,
            contract_id=self.contract.contract_id,
            producer_kind=self.binding.producer_kind,
            producer_id=self.binding.producer_id,
            producer_behavior_id=self.binding.producer_behavior_id,
            reason=reason,
            information_cutoff_at=slot.information_cutoff_at,
            attempted_at=slot.slot_as_of,
            completed_at=completed_at,
            input_refs=input_refs,
            detail=detail,
        )
        self.contracts.record_no_estimate(result)
        return result
