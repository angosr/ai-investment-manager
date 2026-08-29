"""Point-in-time mapping from one economic Forecast to legal perpetual products."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise

from investment_manager.execution.planning.planner import InstrumentExecutionSpec
from investment_manager.forecast.contracts import ForecastContract, ForecastPriceAnchor
from investment_manager.forecast.models import ExposureDirection, ForecastLeg, ForecastTarget
from investment_manager.forecast.product.evaluation import ProductPayoffMappingIdentity
from investment_manager.forecast.product.models import (
    ProductPayoffProjection,
    ProductProjectionState,
    project_product_payoff,
)
from investment_manager.forecast.product.repository import (
    SqlProductPayoffProjectionStore,
)
from investment_manager.forecast.results import BaseForecast
from investment_manager.kernel.errors import PointInTimeInputUnavailable
from investment_manager.kernel.identity import content_hash
from investment_manager.kernel.time import require_utc
from investment_manager.market.models import InstrumentId, InstrumentProduct
from investment_manager.market.perpetual.models import FundingRateType
from investment_manager.market.repository import MarketDataStore
from investment_manager.portfolio.policy import (
    CapitalPolicy,
    ProductPayoffPolicy,
    SleeveRiskTemplate,
)

_BPS = Decimal("10000")


@dataclass(frozen=True, slots=True)
class PointInTimeProductPayoffProjector:
    """Build long/short payoff distributions from facts visible at decision time."""

    policy: ProductPayoffPolicy
    contract: ForecastContract
    market: MarketDataStore
    instruments: tuple[InstrumentId, ...]
    execution_specs: tuple[InstrumentExecutionSpec, ...]
    risk: SleeveRiskTemplate
    maximum_quote_age_seconds: int
    funding_lookback_hours: int

    def __post_init__(self) -> None:
        keys = tuple(item.key for item in self.instruments)
        spec_keys = tuple(item.instrument.key for item in self.execution_specs)
        if keys != self.policy.instrument_keys or spec_keys != keys:
            raise ValueError("Product payoff policy、Instrument 与 execution spec 不一致")
        if len(self.contract.target.legs) != 1:
            raise ValueError("Product payoff 规范参考必须是单腿")
        reference = self.contract.target.legs[0].instrument
        if reference.product != InstrumentProduct.SPOT:
            raise ValueError("现役 Product payoff 必须使用 Spot 经济参考")
        if any(
            item.product == InstrumentProduct.SPOT
            or item.base_asset != reference.base_asset
            or item.quote_asset != reference.quote_asset
            or item.settlement_asset != reference.settlement_asset
            for item in self.instruments
        ):
            raise ValueError("Product payoff products 不是同一线性永续经济暴露")
        if self.maximum_quote_age_seconds < 1 or not 8 <= self.funding_lookback_hours <= 720:
            raise ValueError("Product payoff 行情年龄或 Funding 窗口非法")

    @property
    def candidate_instruments(self) -> tuple[InstrumentId, ...]:
        return self.instruments

    @property
    def mapping_cohort_id(self) -> str:
        return ProductPayoffMappingIdentity(
            economic_exposure_id=self.policy.economic_exposure_id,
            projection_version=self.policy.version,
            instrument_keys=self.policy.instrument_keys,
            maximum_rule_age_seconds=self.policy.maximum_rule_age_seconds,
        ).cohort_id

    def build_for_replay(
        self,
        forecast: BaseForecast,
        *,
        as_of: datetime,
    ) -> tuple[ProductPayoffProjection, ...] | None:
        return self.build(forecast, as_of=as_of)

    def build(
        self,
        forecast: BaseForecast,
        *,
        as_of: datetime,
    ) -> tuple[ProductPayoffProjection, ...]:
        if forecast.contract_id != self.contract.contract_id:
            raise ValueError("Product payoff 收到错误 ForecastContract")
        decision_at = require_utc(as_of)
        if not forecast.available_at <= decision_at < forecast.economic_horizon_end:
            raise ValueError("Product payoff 决策时点超出经济 Forecast 支持范围")
        spec_by_key = {item.instrument.key: item for item in self.execution_specs}
        projections = [
            project_product_payoff(
                contract=self.contract,
                forecast=forecast,
                state=state,
                economic_exposure_id=self.policy.economic_exposure_id,
                projection_version=self.policy.version,
                mapping_cohort_id=self.mapping_cohort_id,
            )
            for instrument in self.instruments
            for state in self._states(
                forecast=forecast,
                as_of=decision_at,
                instrument=instrument,
                spec=spec_by_key[instrument.key],
            )
        ]
        return tuple(sorted(projections, key=lambda item: item.target.target_id))

    def _states(
        self,
        *,
        forecast: BaseForecast,
        as_of: datetime,
        instrument: InstrumentId,
        spec: InstrumentExecutionSpec,
    ) -> tuple[ProductProjectionState, ...]:
        reference = self.contract.target.legs[0].instrument
        quote = self.market.latest_perpetual_quote(
            instrument=instrument,
            evaluation_at=as_of,
            visible_at=as_of,
        )
        market_state = self.market.latest_perpetual_state(instrument=instrument, as_of=as_of)
        rules = self.market.latest_perpetual_product_rules(instrument=instrument, as_of=as_of)
        if quote is None or market_state is None or rules is None:
            return ()
        spot = self.market.latest_spot_quote(
            instrument=reference,
            evaluation_at=quote.observed_at,
            visible_at=as_of,
        )
        if spot is None:
            return ()
        if (
            max(
                self._age(quote.exchange_time, as_of),
                self._age(market_state.exchange_time, as_of),
                self._age(spot.observed_at, as_of),
            )
            >= self.maximum_quote_age_seconds
        ):
            return ()
        if self._age(rules.observed_at, as_of) >= self.policy.maximum_rule_age_seconds:
            return ()
        if rules.status != "TRADING" or (
            spec.quantity_step != rules.market_quantity_step
            or spec.minimum_order_notional != rules.minimum_notional
        ):
            return ()

        interval = rules.funding_interval_hours
        funding_refs: tuple[str, ...] = ()
        if market_state.next_funding_time <= forecast.economic_horizon_end and interval is None:
            inferred = self._inferred_funding_interval(instrument=instrument, as_of=as_of)
            if inferred is None:
                return ()
            interval, funding_refs = inferred
        next_funding = market_state.next_funding_time
        if interval is not None and next_funding <= as_of:
            step = timedelta(hours=interval)
            next_funding += step * (int((as_of - next_funding) // step) + 1)
        settlement_count = (
            0
            if next_funding > forecast.economic_horizon_end
            else self._funding_settlement_count(
                next_funding_at=next_funding,
                horizon_end=forecast.economic_horizon_end,
                interval_hours=interval,
            )
        )
        rates, observed_funding_refs = self._funding_rates(instrument=instrument, as_of=as_of)
        expected_rate = (
            sum(rates, Decimal("0")) / Decimal(len(rates))
            if rates
            else market_state.last_funding_rate * _BPS
        )
        if rules.adjusted_funding_rate_cap is not None:
            expected_rate = min(expected_rate, rules.adjusted_funding_rate_cap * _BPS)
        if rules.adjusted_funding_rate_floor is not None:
            expected_rate = max(expected_rate, rules.adjusted_funding_rate_floor * _BPS)
        funding_scale = self._standard_deviation(rates) if rates else abs(expected_rate)

        product_mid = (quote.bid + quote.ask) / Decimal("2")
        spot_mid = (spot.bid + spot.ask) / Decimal("2")
        persistent_basis = (product_mid / spot_mid - Decimal("1")) * _BPS
        product_spread = (quote.ask - quote.bid) / product_mid * _BPS
        mark_premium = (market_state.mark_price / market_state.index_price - Decimal("1")) * _BPS
        mapping_uncertainty = max(
            rules.tick_size / product_mid * _BPS,
            max(abs(persistent_basis), abs(mark_premium))
            + product_spread / Decimal("2")
            + funding_scale * settlement_count,
        )
        valid_until = min(
            quote.exchange_time + timedelta(seconds=self.maximum_quote_age_seconds),
            market_state.exchange_time + timedelta(seconds=self.maximum_quote_age_seconds),
            spot.observed_at + timedelta(seconds=self.maximum_quote_age_seconds),
            rules.observed_at + timedelta(seconds=self.policy.maximum_rule_age_seconds),
        )
        common = {
            "valid_until": valid_until,
            "expected_exit_basis_bps": persistent_basis,
            "expected_funding_bps": expected_rate * settlement_count,
            "mapping_uncertainty_bps": mapping_uncertainty,
            "initial_margin_fraction": self.risk.derivative_initial_margin_fraction,
            "product_rule_refs": tuple(sorted((rules.rules_id, content_hash(spec)))),
            "input_refs": tuple(
                sorted(
                    {
                        quote.quote_id,
                        market_state.state_id,
                        spot.quote_id,
                        rules.rules_id,
                        *funding_refs,
                        *observed_funding_refs,
                    }
                )
            ),
        }
        return tuple(
            ProductProjectionState(
                target=ForecastTarget.create(
                    (
                        ForecastLeg(
                            instrument=instrument,
                            direction=direction,
                            gross_weight=Decimal("1"),
                        ),
                    )
                ),
                entry_anchor=ForecastPriceAnchor(
                    instrument_id=instrument.key,
                    price=quote.ask if direction == ExposureDirection.LONG else quote.bid,
                    observed_at=quote.exchange_time,
                    available_at=as_of,
                    quote_ref=quote.quote_id,
                ),
                **common,
            )
            for direction in (ExposureDirection.LONG, ExposureDirection.SHORT)
        )

    def _funding_rates(
        self,
        *,
        instrument: InstrumentId,
        as_of: datetime,
    ) -> tuple[tuple[Decimal, ...], tuple[str, ...]]:
        settlements = tuple(
            item
            for item in self.market.funding_settlements(
                instrument=instrument,
                start=as_of - timedelta(hours=self.funding_lookback_hours),
                end=as_of,
                visible_at=as_of,
            )
            if item.rate_type == FundingRateType.REGULAR
        )
        return (
            tuple(item.funding_rate * _BPS for item in settlements),
            tuple(item.settlement_id for item in settlements),
        )

    def _inferred_funding_interval(
        self,
        *,
        instrument: InstrumentId,
        as_of: datetime,
    ) -> tuple[int, tuple[str, ...]] | None:
        settlements = tuple(
            item
            for item in self.market.funding_settlements(
                instrument=instrument,
                start=as_of - timedelta(hours=72),
                end=as_of,
                visible_at=as_of,
            )
            if item.rate_type == FundingRateType.REGULAR
        )
        if len(settlements) < 3:
            return None
        selected = settlements[-3:]
        gaps = tuple(
            int((later.funding_time - earlier.funding_time).total_seconds())
            for earlier, later in pairwise(selected)
        )
        if len(set(gaps)) != 1 or gaps[0] <= 0 or gaps[0] % 3600:
            return None
        hours = gaps[0] // 3600
        if not 1 <= hours <= 24:
            return None
        return hours, tuple(item.settlement_id for item in selected)

    @staticmethod
    def _funding_settlement_count(
        *,
        next_funding_at: datetime,
        horizon_end: datetime,
        interval_hours: int | None,
    ) -> int:
        if interval_hours is None:
            raise PointInTimeInputUnavailable("Product payoff 缺少 Funding interval")
        return int((horizon_end - next_funding_at) // timedelta(hours=interval_hours)) + 1

    @staticmethod
    def _standard_deviation(values: tuple[Decimal, ...]) -> Decimal:
        if not values:
            return Decimal("0")
        mean = sum(values, Decimal("0")) / Decimal(len(values))
        return (
            sum(((item - mean) ** 2 for item in values), Decimal("0")) / Decimal(len(values))
        ).sqrt()

    @staticmethod
    def _age(observed_at: datetime, as_of: datetime) -> float:
        return max(0.0, (as_of - observed_at).total_seconds())


@dataclass(frozen=True, slots=True)
class RecordedProductPayoffProjector:
    """Persist deterministic projections used by the official capital account."""

    builder: PointInTimeProductPayoffProjector
    store: SqlProductPayoffProjectionStore

    @property
    def candidate_instruments(self) -> tuple[InstrumentId, ...]:
        return self.builder.candidate_instruments

    def project(
        self,
        forecast: BaseForecast,
        *,
        as_of: datetime,
    ) -> tuple[ProductPayoffProjection, ...]:
        projections = self.builder.build(forecast, as_of=as_of)
        for projection in projections:
            self.store.record(projection)
        return projections

    def for_source(self, source_forecast_id: str) -> tuple[ProductPayoffProjection, ...]:
        return self.store.for_source(source_forecast_id)


def build_point_in_time_product_payoff_projector(
    *,
    capital_policy: CapitalPolicy,
    contract: ForecastContract,
    market: MarketDataStore,
    funding_lookback_hours: int,
) -> PointInTimeProductPayoffProjector:
    """Build the one legal product mapping for an economic forecast contract."""

    if len(contract.target.legs) != 1:
        raise PointInTimeInputUnavailable("资本映射缺少单腿 ForecastContract")
    reference = contract.target.legs[0].instrument
    specs_by_key = {item.instrument.key: item for item in capital_policy.execution_specs}
    policies = tuple(
        policy
        for policy in capital_policy.product_payoff_policies
        if all(
            key in specs_by_key
            and specs_by_key[key].instrument.base_asset == reference.base_asset
            and specs_by_key[key].instrument.quote_asset == reference.quote_asset
            and specs_by_key[key].instrument.settlement_asset == reference.settlement_asset
            for key in policy.instrument_keys
        )
    )
    if len(policies) != 1:
        raise PointInTimeInputUnavailable("资本映射缺少唯一产品政策")
    policy = policies[0]
    specs = tuple(specs_by_key[key] for key in policy.instrument_keys)
    return PointInTimeProductPayoffProjector(
        policy=policy,
        contract=contract,
        market=market,
        instruments=tuple(item.instrument for item in specs),
        execution_specs=specs,
        risk=capital_policy.sleeve_risk,
        maximum_quote_age_seconds=capital_policy.risk.maximum_quote_age_seconds,
        funding_lookback_hours=funding_lookback_hours,
    )


__all__ = [
    "PointInTimeProductPayoffProjector",
    "RecordedProductPayoffProjector",
    "build_point_in_time_product_payoff_projector",
]
