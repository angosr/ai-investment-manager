from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import Field, model_validator

from investment_manager.execution.planning.planner import InstrumentExecutionSpec
from investment_manager.forecast.models import ExposureDirection
from investment_manager.forecast.results import BaseForecast, CalibratedForecast, Forecast
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel, Money, UnitInterval
from investment_manager.market.models import ExecutableQuote
from investment_manager.portfolio.models import (
    MockCandidateAuthorization,
    PortfolioAccountSnapshot,
    PortfolioCostEstimate,
    PortfolioEdgeBasis,
    PortfolioTarget,
    SleeveTarget,
    sleeve_gross_notional,
)


class PortfolioDecisionPolicy(FrozenModel):
    version: str = Field(min_length=1)
    portfolio_id: str = Field(min_length=1)
    enabled: bool = False
    minimum_conservative_net_bps: Decimal = Field(default=Decimal("5"), ge=0)
    maximum_total_exposure_fraction: UnitInterval = Decimal("0.50")
    maximum_single_sleeve_fraction: UnitInterval = Decimal("0.30")
    minimum_rebalance_notional: Money = Decimal("25")
    target_validity_minutes: int = Field(default=30, ge=1, le=1_440)
    cost_model_version: str = Field(default="executable-round-trip-v1", min_length=1)
    exit_spread_multiplier: Decimal = Field(default=Decimal("1"), ge=1)
    depth_slippage_multiplier: Decimal = Field(default=Decimal("1"), ge=0)

    @model_validator(mode="after")
    def policy_must_be_bounded(self):
        if self.maximum_single_sleeve_fraction <= 0:
            raise ValueError("单 Sleeve 暴露上限必须为正数")
        return self


class PortfolioSleeveInput(FrozenModel):
    sleeve_id: str = Field(min_length=1)
    forecast: Forecast
    mock_authorization: MockCandidateAuthorization | None = None

    @model_validator(mode="after")
    def forecast_permission_must_be_explicit(self):
        if isinstance(self.forecast, BaseForecast):
            permission = self.mock_authorization
            if permission is None or (
                permission.producer_id != self.forecast.producer_id
                or permission.producer_behavior_id != self.forecast.producer_behavior_id
                or permission.outcome_family_id != self.forecast.outcome_family_id
            ):
                raise ValueError("BaseForecast 必须精确绑定 Mock candidate authorization")
        elif self.mock_authorization is not None:
            raise ValueError("CalibratedForecast 不得使用 Mock candidate authorization")
        return self


def remaining_forecast_gross_bps(
    forecast: Forecast,
    *,
    quote_by_instrument: dict[str, ExecutableQuote],
    as_of: datetime,
) -> Decimal:
    """Reprice a cutoff-based payoff to the current executable entry, with both signs."""

    require_utc(as_of)
    forecast_gross_bps = (
        forecast.expected_gross_bps
        if isinstance(forecast, BaseForecast)
        else forecast.conservative_gross_bps
    )
    cutoff = {item.instrument_id: item.price for item in forecast.cutoff_prices}
    realized_to_entry_bps = Decimal("0")
    for leg in forecast.target.legs:
        quote = quote_by_instrument[leg.instrument.key]
        entry_price = quote.ask if leg.direction == ExposureDirection.LONG else quote.bid
        sign = Decimal("1") if leg.direction == ExposureDirection.LONG else Decimal("-1")
        realized_to_entry_bps += (
            sign
            * leg.gross_weight
            * (entry_price / cutoff[leg.instrument.key] - Decimal("1"))
            * Decimal("10000")
        )
    return forecast_gross_bps - realized_to_entry_bps


def estimate_round_trip_cost(
    *,
    policy: PortfolioDecisionPolicy,
    forecast: Forecast,
    gross_notional: Decimal,
    quote_by_instrument: dict[str, ExecutableQuote],
    spec_by_instrument: dict[str, InstrumentExecutionSpec],
) -> PortfolioCostEstimate:
    """Apply the single authoritative fee/spread/depth model at decision time."""

    fee_bps = Decimal("0")
    exit_spread_bps = Decimal("0")
    depth_slippage_bps = Decimal("0")
    refs = []
    for leg in forecast.target.legs:
        quote = quote_by_instrument[leg.instrument.key]
        spec = spec_by_instrument[leg.instrument.key]
        refs.append(quote.source_quote_id)
        fee_bps += leg.gross_weight * Decimal("2") * spec.fee_bps
        half_spread = (quote.ask - quote.bid) / (quote.ask + quote.bid) * Decimal("10000")
        exit_spread_bps += leg.gross_weight * half_spread * policy.exit_spread_multiplier
        side_price = quote.ask if leg.direction == ExposureDirection.LONG else quote.bid
        side_quantity = (
            quote.ask_quantity if leg.direction == ExposureDirection.LONG else quote.bid_quantity
        )
        depth_notional = side_price * side_quantity * leg.instrument.contract_multiplier
        desired_leg_notional = gross_notional * leg.gross_weight
        if desired_leg_notional > depth_notional:
            depth_slippage_bps += (
                leg.gross_weight
                * half_spread
                * policy.depth_slippage_multiplier
                * (desired_leg_notional / depth_notional - Decimal("1"))
            )
    return PortfolioCostEstimate(
        model_version=policy.cost_model_version,
        gross_notional=gross_notional,
        fee_bps=fee_bps,
        exit_spread_bps=exit_spread_bps,
        depth_slippage_bps=depth_slippage_bps,
        total_bps=fee_bps + exit_spread_bps + depth_slippage_bps,
        quote_refs=tuple(sorted(refs)),
    )


class PortfolioDecisionEngine:
    """Sole owner of cash comparison, executable costs, sizing and target validity."""

    def __init__(self, policy: PortfolioDecisionPolicy) -> None:
        self._policy = policy

    def decide(
        self,
        *,
        cycle_id: str,
        as_of: datetime,
        account: PortfolioAccountSnapshot,
        sleeves: tuple[PortfolioSleeveInput, ...],
        quotes: tuple[ExecutableQuote, ...],
        execution_specs: tuple[InstrumentExecutionSpec, ...],
        decision_valid_until: datetime | None = None,
    ) -> PortfolioTarget | None:
        if not self._policy.enabled:
            return None
        as_of = require_utc(as_of)
        if decision_valid_until is not None:
            decision_valid_until = require_utc(decision_valid_until)
            if decision_valid_until <= as_of:
                raise ValueError("Portfolio 决策截止时间必须晚于 as_of")
        self._validate_account_and_inputs(
            cycle_id=cycle_id,
            as_of=as_of,
            account=account,
            sleeves=sleeves,
        )
        if account.equity <= 0:
            return None
        quote_by_instrument = self._quotes(quotes=quotes, as_of=as_of)
        spec_by_instrument = self._specs(execution_specs)
        account_by_sleeve = {item.sleeve_id: item for item in account.sleeves}
        if set(account_by_sleeve) - {item.sleeve_id for item in sleeves}:
            raise ValueError("Portfolio 输入必须显式覆盖全部当前 Sleeve")
        required_quote_keys = {
            leg.instrument.key for item in sleeves for leg in item.forecast.target.legs
        }
        if set(quote_by_instrument) != required_quote_keys:
            raise ValueError("ExecutableQuote 必须精确覆盖 Portfolio Sleeve Instruments")
        if not required_quote_keys.issubset(spec_by_instrument):
            raise ValueError("ExecutionSpec 必须覆盖 Portfolio Sleeve Instruments")

        current_by_sleeve = {
            item.sleeve_id: sleeve_gross_notional(
                account_by_sleeve.get(item.sleeve_id),
                quote_by_instrument=quote_by_instrument,
            )
            for item in sleeves
        }
        for item in sleeves:
            expected_id = SleeveTarget.identity_for(
                portfolio_id=self._policy.portfolio_id,
                forecast_family=item.forecast.outcome_family_id,
                forecast_target_id=item.forecast.target.target_id,
            )
            if item.sleeve_id != expected_id:
                raise ValueError("PortfolioSleeveInput sleeve_id 与 Forecast 不一致")

        single_limit = account.equity * self._policy.maximum_single_sleeve_fraction
        candidate_notional = {
            item.sleeve_id: min(
                single_limit,
                self._allocation_limit(item, equity=account.equity),
            )
            for item in sleeves
        }
        eligible = tuple(
            sorted(
                (
                    item
                    for item in sleeves
                    if self._is_eligible(
                        item,
                        quote_by_instrument=quote_by_instrument,
                        spec_by_instrument=spec_by_instrument,
                        as_of=as_of,
                        current_notional=current_by_sleeve[item.sleeve_id],
                        evaluation_notional=candidate_notional[item.sleeve_id],
                    )
                ),
                key=lambda item: (
                    -self._net_edge(
                        item,
                        quote_by_instrument=quote_by_instrument,
                        spec_by_instrument=spec_by_instrument,
                        as_of=as_of,
                        gross_notional=candidate_notional[item.sleeve_id],
                    ),
                    0 if isinstance(item.forecast, CalibratedForecast) else 1,
                    item.sleeve_id,
                ),
            )
        )
        desired_by_sleeve: dict[str, Decimal] = {}
        remaining_capacity = account.equity * self._policy.maximum_total_exposure_fraction
        for item in eligible:
            desired = min(candidate_notional[item.sleeve_id], remaining_capacity)
            if desired <= 0:
                break
            desired_by_sleeve[item.sleeve_id] = desired
            remaining_capacity -= desired

        eligible_ids = set(desired_by_sleeve)
        targets = tuple(
            self._target(
                item,
                desired_notional=desired_by_sleeve.get(item.sleeve_id, Decimal("0")),
                evaluation_notional=(
                    desired_by_sleeve.get(item.sleeve_id)
                    or current_by_sleeve[item.sleeve_id]
                    or candidate_notional[item.sleeve_id]
                ),
                quote_by_instrument=quote_by_instrument,
                spec_by_instrument=spec_by_instrument,
                as_of=as_of,
                allocation_reason=(
                    "POSITIVE_NET_EDGE_SELECTED"
                    if item.sleeve_id in eligible_ids
                    else "FORECAST_INVALID_CASH"
                    if not self._forecast_is_current(item.forecast, as_of=as_of)
                    else "NON_POSITIVE_NET_EDGE_CASH"
                ),
            )
            for item in sleeves
            if item.sleeve_id in eligible_ids or current_by_sleeve[item.sleeve_id] > 0
        )
        desired_frozen = {item.sleeve_id: item.desired_gross_notional for item in targets}
        turnover = sum(
            abs(
                desired_frozen.get(item.sleeve_id, Decimal("0")) - current_by_sleeve[item.sleeve_id]
            )
            for item in sleeves
        )
        invalid_holding_exit = any(
            current_by_sleeve[item.sleeve_id] > 0
            and not self._forecast_is_current(item.forecast, as_of=as_of)
            for item in sleeves
        )
        below_rebalance_minimum = (
            not invalid_holding_exit
            and Decimal("0") < turnover < self._policy.minimum_rebalance_notional
        )
        if below_rebalance_minimum:
            targets = tuple(
                self._target(
                    item,
                    desired_notional=current_by_sleeve[item.sleeve_id],
                    evaluation_notional=current_by_sleeve[item.sleeve_id],
                    quote_by_instrument=quote_by_instrument,
                    spec_by_instrument=spec_by_instrument,
                    as_of=as_of,
                    allocation_reason="REBALANCE_BELOW_MINIMUM_CURRENT_TARGET",
                )
                for item in sleeves
                if current_by_sleeve[item.sleeve_id] > 0
            )

        reason_codes: set[str] = set()
        if eligible_ids:
            reason_codes.add("POSITIVE_NET_EDGE_SELECTED")
        if not eligible_ids:
            reason_codes.add("CASH_SELECTED_NO_POSITIVE_NET_EDGE")
        if invalid_holding_exit:
            reason_codes.add("EXPIRED_FORECAST_EXIT")
        if below_rebalance_minimum:
            reason_codes.add("REBALANCE_BELOW_MINIMUM")

        valid_until = as_of + timedelta(minutes=self._policy.target_validity_minutes)
        if decision_valid_until is not None:
            valid_until = min(valid_until, decision_valid_until)
        if eligible_ids:
            valid_until = min(
                valid_until,
                *(item.forecast.valid_until for item in sleeves if item.sleeve_id in eligible_ids),
            )
        payload = {
            "cycle_id": cycle_id,
            "portfolio_id": self._policy.portfolio_id,
            "policy_version": self._policy.version,
            "as_of": as_of.isoformat(),
            "valid_until": valid_until.isoformat(),
            "reference_equity": account.equity,
            "account_snapshot_id": account.snapshot_id,
            "account_snapshot_hash": content_hash(account),
            "considered_forecast_ids": tuple(sorted(item.forecast.forecast_id for item in sleeves)),
            "quotes": [item.model_dump(mode="json") for item in quotes],
            "sleeves": [item.model_dump(mode="json") for item in targets],
            "reason_codes": tuple(sorted(reason_codes)),
        }
        return PortfolioTarget(
            target_id=stable_id("portfolio_target", content_hash(payload)),
            **payload,
        )

    def force_cash(
        self,
        *,
        cycle_id: str,
        as_of: datetime,
        account: PortfolioAccountSnapshot,
        sleeves: tuple[PortfolioSleeveInput, ...],
        quotes: tuple[ExecutableQuote, ...],
        execution_specs: tuple[InstrumentExecutionSpec, ...],
        reason_codes: tuple[str, ...],
    ) -> PortfolioTarget:
        as_of = require_utc(as_of)
        if (
            account.cycle_id != cycle_id
            or account.as_of != as_of
            or account.portfolio_id != self._policy.portfolio_id
            or account.equity <= 0
        ):
            raise ValueError("Risk exit account 与 cycle/as_of/portfolio 不一致")
        quote_by_instrument = self._quotes(quotes=quotes, as_of=as_of)
        spec_by_instrument = self._specs(execution_specs)
        inputs = {item.sleeve_id: item for item in sleeves}
        current = {item.sleeve_id: item for item in account.sleeves}
        if set(inputs) != set(current) or len(inputs) != len(sleeves):
            raise ValueError("Risk exit 必须精确覆盖全部当前 Sleeve")
        targets = []
        for sleeve_id, position in sorted(current.items()):
            item = inputs[sleeve_id]
            if (
                item.forecast.target != position.target
                or item.forecast.outcome_family_id != position.forecast_family
            ):
                raise ValueError("Risk exit Forecast 与当前 Sleeve 身份不一致")
            current_notional = sleeve_gross_notional(
                position,
                quote_by_instrument=quote_by_instrument,
            )
            targets.append(
                self._target(
                    item,
                    desired_notional=Decimal("0"),
                    evaluation_notional=current_notional,
                    quote_by_instrument=quote_by_instrument,
                    spec_by_instrument=spec_by_instrument,
                    as_of=as_of,
                    allocation_reason="PROGRAMMATIC_RISK_EXIT",
                )
            )
        payload = {
            "cycle_id": cycle_id,
            "portfolio_id": self._policy.portfolio_id,
            "policy_version": self._policy.version,
            "as_of": as_of,
            "valid_until": as_of + timedelta(minutes=self._policy.target_validity_minutes),
            "reference_equity": account.equity,
            "account_snapshot_id": account.snapshot_id,
            "account_snapshot_hash": content_hash(account),
            "considered_forecast_ids": tuple(sorted(item.forecast.forecast_id for item in sleeves)),
            "quotes": quotes,
            "sleeves": tuple(targets),
            "reason_codes": tuple(sorted({"PROGRAMMATIC_RISK_EXIT", *reason_codes})),
        }
        return PortfolioTarget(
            target_id=stable_id("portfolio_target", content_hash(payload)),
            **payload,
        )

    def _is_eligible(
        self,
        item: PortfolioSleeveInput,
        *,
        quote_by_instrument: dict[str, ExecutableQuote],
        spec_by_instrument: dict[str, InstrumentExecutionSpec],
        as_of: datetime,
        current_notional: Decimal,
        evaluation_notional: Decimal,
    ) -> bool:
        if not self._forecast_is_current(item.forecast, as_of=as_of):
            return False
        net_edge = self._net_edge(
            item,
            quote_by_instrument=quote_by_instrument,
            spec_by_instrument=spec_by_instrument,
            as_of=as_of,
            gross_notional=evaluation_notional,
        )
        if isinstance(item.forecast, CalibratedForecast):
            return net_edge >= self._policy.minimum_conservative_net_bps
        permission = item.mock_authorization
        assert permission is not None
        threshold = (
            permission.minimum_hold_net_bps
            if current_notional > 0
            else permission.minimum_entry_net_bps
        )
        return net_edge >= threshold

    @staticmethod
    def _forecast_is_current(forecast: Forecast, *, as_of: datetime) -> bool:
        return forecast.available_at <= as_of < forecast.valid_until

    def _net_edge(
        self,
        item: PortfolioSleeveInput,
        *,
        quote_by_instrument: dict[str, ExecutableQuote],
        spec_by_instrument: dict[str, InstrumentExecutionSpec],
        as_of: datetime,
        gross_notional: Decimal,
    ) -> Decimal:
        return (
            remaining_forecast_gross_bps(
                item.forecast,
                quote_by_instrument=quote_by_instrument,
                as_of=as_of,
            )
            - self._cost(
                item,
                gross_notional=gross_notional,
                quote_by_instrument=quote_by_instrument,
                spec_by_instrument=spec_by_instrument,
            ).total_bps
        )

    def _target(
        self,
        item: PortfolioSleeveInput,
        *,
        desired_notional: Decimal,
        evaluation_notional: Decimal,
        quote_by_instrument: dict[str, ExecutableQuote],
        spec_by_instrument: dict[str, InstrumentExecutionSpec],
        as_of: datetime,
        allocation_reason: str,
    ) -> SleeveTarget:
        gross = remaining_forecast_gross_bps(
            item.forecast,
            quote_by_instrument=quote_by_instrument,
            as_of=as_of,
        )
        cost = self._cost(
            item,
            gross_notional=evaluation_notional,
            quote_by_instrument=quote_by_instrument,
            spec_by_instrument=spec_by_instrument,
        )
        return SleeveTarget(
            sleeve_id=item.sleeve_id,
            forecast_family=item.forecast.outcome_family_id,
            forecast_target=item.forecast.target,
            desired_gross_notional=desired_notional,
            forecast_ids=(item.forecast.forecast_id,),
            edge_basis=(
                PortfolioEdgeBasis.MOCK_HYPOTHESIS
                if isinstance(item.forecast, BaseForecast)
                else PortfolioEdgeBasis.CALIBRATED_CONSERVATIVE
            ),
            decision_gross_bps=gross,
            cost=cost,
            decision_net_bps=gross - cost.total_bps,
            reason_codes=tuple(
                sorted(
                    (
                        f"FORECAST_PRODUCER:{item.forecast.producer_id}",
                        allocation_reason,
                    )
                )
            ),
        )

    def _cost(
        self,
        item: PortfolioSleeveInput,
        *,
        gross_notional: Decimal,
        quote_by_instrument: dict[str, ExecutableQuote],
        spec_by_instrument: dict[str, InstrumentExecutionSpec],
    ) -> PortfolioCostEstimate:
        return estimate_round_trip_cost(
            policy=self._policy,
            forecast=item.forecast,
            gross_notional=gross_notional,
            quote_by_instrument=quote_by_instrument,
            spec_by_instrument=spec_by_instrument,
        )

    @staticmethod
    def _allocation_limit(item: PortfolioSleeveInput, *, equity: Decimal) -> Decimal:
        if item.mock_authorization is None:
            return equity
        return equity * item.mock_authorization.maximum_allocation_fraction

    @staticmethod
    def _quotes(
        *, quotes: tuple[ExecutableQuote, ...], as_of: datetime
    ) -> dict[str, ExecutableQuote]:
        keys = tuple(item.instrument.key for item in quotes)
        if tuple(sorted(set(keys))) != keys:
            raise ValueError("ExecutableQuote 必须按 Instrument 唯一且排序")
        if any(item.as_of != as_of for item in quotes):
            raise ValueError("ExecutableQuote 必须冻结在 Portfolio as_of")
        return {item.instrument.key: item for item in quotes}

    @staticmethod
    def _specs(
        specs: tuple[InstrumentExecutionSpec, ...],
    ) -> dict[str, InstrumentExecutionSpec]:
        keys = tuple(item.instrument.key for item in specs)
        if tuple(sorted(set(keys))) != keys:
            raise ValueError("ExecutionSpec 必须按 Instrument 唯一且排序")
        return {item.instrument.key: item for item in specs}

    def _validate_account_and_inputs(
        self,
        *,
        cycle_id: str,
        as_of: datetime,
        account: PortfolioAccountSnapshot,
        sleeves: tuple[PortfolioSleeveInput, ...],
    ) -> None:
        sleeve_ids = tuple(item.sleeve_id for item in sleeves)
        if tuple(sorted(set(sleeve_ids))) != sleeve_ids:
            raise ValueError("PortfolioSleeveInput 必须按 sleeve_id 唯一且排序")
        if (
            account.cycle_id != cycle_id
            or account.as_of != as_of
            or account.portfolio_id != self._policy.portfolio_id
        ):
            raise ValueError("Portfolio account 与 cycle/as_of/portfolio 不一致")
