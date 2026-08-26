"""Point-in-time product payoff projection for one Context economic forecast."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from investment_manager.execution.planning.planner import InstrumentExecutionSpec
from investment_manager.forecast.context.producer import ContextTargetStateProvider
from investment_manager.forecast.contracts import ForecastContract, ForecastPriceAnchor
from investment_manager.forecast.models import (
    ExposureDirection,
    ForecastLeg,
    ForecastTarget,
)
from investment_manager.forecast.product.models import (
    ProductPayoffProjection,
    ProductProjectionState,
    project_product_payoff,
)
from investment_manager.forecast.product.repository import SqlProductPayoffProjectionStore
from investment_manager.forecast.results import BaseForecast
from investment_manager.kernel.errors import PointInTimeInputUnavailable
from investment_manager.kernel.identity import content_hash
from investment_manager.kernel.time import require_utc
from investment_manager.market.models import InstrumentId, InstrumentProduct
from investment_manager.market.perpetual.models import FundingRateType
from investment_manager.market.repository import MarketDataStore
from investment_manager.portfolio.policy import ProductPayoffPolicy, SleeveRiskTemplate

_BPS = Decimal("10000")


@dataclass(frozen=True, slots=True)
class ContextProductPayoffProjector:
    """Build product candidates from market facts without another AI analysis."""

    policy: ProductPayoffPolicy
    contract: ForecastContract
    market: MarketDataStore
    target_states: ContextTargetStateProvider
    instruments: tuple[InstrumentId, ...]
    execution_specs: tuple[InstrumentExecutionSpec, ...]
    risk: SleeveRiskTemplate
    maximum_quote_age_seconds: int
    store: SqlProductPayoffProjectionStore

    def __post_init__(self) -> None:
        keys = tuple(item.key for item in self.instruments)
        spec_keys = tuple(item.instrument.key for item in self.execution_specs)
        if keys != self.policy.instrument_keys or spec_keys != keys:
            raise ValueError("Product payoff policy、Instrument 与 execution spec 不一致")
        if len(self.contract.target.legs) != 1:
            raise ValueError("Context product payoff 规范参考必须是单腿")
        reference = self.contract.target.legs[0].instrument
        if reference.key not in keys:
            raise ValueError("Context product payoff 缺少规范参考产品")
        if any(
            item.base_asset != reference.base_asset
            or item.quote_asset != reference.quote_asset
            or item.settlement_asset != reference.settlement_asset
            for item in self.instruments
        ):
            raise ValueError("Context product payoff products 不是同一线性经济暴露")
        if self.maximum_quote_age_seconds < 1:
            raise ValueError("Product payoff quote 最大年龄必须为正数")

    @property
    def candidate_instruments(self) -> tuple[InstrumentId, ...]:
        return self.instruments

    def for_source(
        self,
        source_forecast_id: str,
    ) -> tuple[ProductPayoffProjection, ...]:
        return self.store.for_source(source_forecast_id)

    def project(
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
        state = self.target_states.build(as_of=decision_at)
        spec_by_key = {item.instrument.key: item for item in self.execution_specs}
        projections: list[ProductPayoffProjection] = []
        for instrument in self.instruments:
            spec = spec_by_key[instrument.key]
            if instrument.product == InstrumentProduct.SPOT:
                projection = self._spot_projection(
                    forecast=forecast,
                    as_of=decision_at,
                    instrument=instrument,
                    spec=spec,
                )
                self.store.record(projection)
                projections.append(projection)
                continue
            projection_states = self._derivative_states(
                forecast=forecast,
                as_of=decision_at,
                instrument=instrument,
                spec=spec,
                target_state=state,
            )
            for projection_state in projection_states:
                projection = project_product_payoff(
                    contract=self.contract,
                    forecast=forecast,
                    state=projection_state,
                    economic_exposure_id=self.policy.economic_exposure_id,
                    projection_version=self.policy.version,
                )
                self.store.record(projection)
                projections.append(projection)
        return tuple(sorted(projections, key=lambda item: item.target.target_id))

    def _spot_projection(
        self,
        *,
        forecast: BaseForecast,
        as_of: datetime,
        instrument: InstrumentId,
        spec: InstrumentExecutionSpec,
    ) -> ProductPayoffProjection:
        quote = self.market.latest_spot_quote(
            instrument=instrument,
            evaluation_at=as_of,
            visible_at=as_of,
        )
        if quote is None or self._age_seconds(quote.observed_at, as_of) >= (
            self.maximum_quote_age_seconds
        ):
            raise PointInTimeInputUnavailable("Product payoff 缺少新鲜 Spot 入场报价")
        valid_until = quote.observed_at + timedelta(
            seconds=self.maximum_quote_age_seconds
        )
        projection_state = ProductProjectionState(
            target=ForecastTarget.single_long(instrument),
            entry_anchor=ForecastPriceAnchor(
                instrument_id=instrument.key,
                price=quote.ask,
                observed_at=quote.observed_at,
                available_at=as_of,
                quote_ref=quote.quote_id,
            ),
            valid_until=valid_until,
            expected_exit_basis_bps=Decimal("0"),
            expected_funding_bps=Decimal("0"),
            mapping_uncertainty_bps=Decimal("0"),
            initial_margin_fraction=Decimal("1"),
            product_rule_refs=(content_hash(spec),),
            input_refs=(quote.quote_id,),
        )
        return project_product_payoff(
            contract=self.contract,
            forecast=forecast,
            state=projection_state,
            economic_exposure_id=self.policy.economic_exposure_id,
            projection_version=self.policy.version,
        )

    def _derivative_states(
        self,
        *,
        forecast: BaseForecast,
        as_of: datetime,
        instrument: InstrumentId,
        spec: InstrumentExecutionSpec,
        target_state,
    ) -> tuple[ProductProjectionState, ...]:
        quote = self.market.latest_perpetual_quote(
            instrument=instrument,
            evaluation_at=as_of,
            visible_at=as_of,
        )
        market_state = self.market.latest_perpetual_state(
            instrument=instrument,
            as_of=as_of,
        )
        rules = self.market.latest_perpetual_product_rules(
            instrument=instrument,
            as_of=as_of,
        )
        derivative = next(
            (
                item
                for item in target_state.derivative_states
                if item.asset == instrument.base_asset
                and item.market_symbol == instrument.symbol
            ),
            None,
        )
        if quote is None or market_state is None or rules is None or derivative is None:
            return ()
        if max(
            self._age_seconds(quote.exchange_time, as_of),
            self._age_seconds(market_state.exchange_time, as_of),
        ) >= self.maximum_quote_age_seconds:
            return ()
        if self._age_seconds(rules.observed_at, as_of) >= (
            self.policy.maximum_rule_age_seconds
        ):
            return ()
        if rules.status != "TRADING":
            return ()
        if (
            spec.quantity_step != rules.market_quantity_step
            or spec.minimum_order_notional != rules.minimum_notional
        ):
            return ()
        funding_rule_refs: tuple[str, ...] = ()
        next_funding_time = market_state.next_funding_time
        interval_hours = rules.funding_interval_hours
        if (
            next_funding_time <= forecast.economic_horizon_end
            and interval_hours is None
        ):
            inferred = self._inferred_funding_interval(
                instrument=instrument,
                as_of=as_of,
            )
            if inferred is None:
                return ()
            interval_hours, funding_rule_refs = inferred
        if next_funding_time <= as_of:
            assert interval_hours is not None
            interval = timedelta(hours=interval_hours)
            elapsed_intervals = int((as_of - next_funding_time) // interval) + 1
            next_funding_time += interval * elapsed_intervals
        if next_funding_time > forecast.economic_horizon_end:
            settlement_count = 0
        else:
            assert interval_hours is not None
            settlement_count = self._funding_settlement_count(
                next_funding_at=next_funding_time,
                horizon_end=forecast.economic_horizon_end,
                interval_hours=interval_hours,
            )
        expected_rate = (
            derivative.trailing_funding_rate_mean_bps
            if derivative.trailing_funding_rate_mean_bps is not None
            else derivative.last_funding_rate_bps
        )
        if (
            rules.adjusted_funding_rate_cap is not None
            and rules.adjusted_funding_rate_floor is not None
        ):
            cap_bps = rules.adjusted_funding_rate_cap * _BPS
            floor_bps = rules.adjusted_funding_rate_floor * _BPS
            if not floor_bps <= expected_rate <= cap_bps:
                return ()
        expected_funding = expected_rate * settlement_count
        funding_scale = (
            derivative.trailing_funding_rate_stddev_bps
            if derivative.trailing_funding_rate_stddev_bps is not None
            else abs(derivative.last_funding_rate_bps)
        )
        reference_scale = max(
            derivative.spot_mid_range_bps or Decimal("0"),
            abs(derivative.reference_spot_mid_deviation_bps or Decimal("0")),
        )
        entry_mid = (quote.ask + quote.bid) / Decimal("2")
        tick_scale = rules.tick_size / entry_mid * _BPS
        mapping_uncertainty = max(
            tick_scale,
            abs(derivative.mark_index_premium_bps)
            + derivative.perpetual_spread_bps / Decimal("2")
            + funding_scale * settlement_count
            + reference_scale,
        )
        valid_until = min(
            quote.exchange_time + timedelta(seconds=self.maximum_quote_age_seconds),
            market_state.exchange_time
            + timedelta(seconds=self.maximum_quote_age_seconds),
        )
        common = {
            "expected_exit_basis_bps": Decimal("0"),
            "expected_funding_bps": expected_funding,
            "mapping_uncertainty_bps": mapping_uncertainty,
            "initial_margin_fraction": self.risk.derivative_initial_margin_fraction,
            "valid_until": valid_until,
            "product_rule_refs": tuple(sorted((rules.rules_id, content_hash(spec)))),
            "input_refs": tuple(
                sorted(
                    {
                        quote.quote_id,
                        market_state.state_id,
                        derivative.evidence_ref,
                        rules.rules_id,
                        *funding_rule_refs,
                        *target_state.input_refs,
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
                    price=(
                        quote.ask
                        if direction == ExposureDirection.LONG
                        else quote.bid
                    ),
                    observed_at=quote.exchange_time,
                    available_at=as_of,
                    quote_ref=quote.quote_id,
                ),
                **common,
            )
            for direction in (ExposureDirection.LONG, ExposureDirection.SHORT)
        )

    @staticmethod
    def _funding_settlement_count(
        *,
        next_funding_at,
        horizon_end,
        interval_hours: int,
    ) -> int:
        if next_funding_at > horizon_end:
            return 0
        interval = timedelta(hours=interval_hours)
        return int((horizon_end - next_funding_at) // interval) + 1

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
        first, previous, latest = settlements[-3:]
        gaps = (
            int((previous.funding_time - first.funding_time).total_seconds()),
            int((latest.funding_time - previous.funding_time).total_seconds()),
        )
        if gaps[0] != gaps[1] or gaps[1] <= 0 or gaps[1] % 3600:
            return None
        interval_hours = gaps[1] // 3600
        if not 1 <= interval_hours <= 24:
            return None
        return interval_hours, (
            first.settlement_id,
            previous.settlement_id,
            latest.settlement_id,
        )

    @staticmethod
    def _age_seconds(observed_at, as_of) -> float:
        return max(0.0, (as_of - observed_at).total_seconds())


__all__ = ["ContextProductPayoffProjector"]
