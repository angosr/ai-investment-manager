from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import Field, model_validator

from investment_manager.execution.planning.planner import InstrumentExecutionSpec
from investment_manager.forecast.models import ExposureDirection, ForecastTarget
from investment_manager.forecast.product.models import ProductPayoffProjection
from investment_manager.forecast.results import BaseForecast, CalibratedForecast, Forecast
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel, Money, UnitInterval
from investment_manager.market.models import ExecutableQuote
from investment_manager.portfolio.models import (
    CandidateCapitalAuthorization,
    PortfolioAccountSnapshot,
    PortfolioCandidateEvaluation,
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
    cost_model_version: str = Field(default="executable-state-transition-v1", min_length=1)
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
    payoff_projection: ProductPayoffProjection | None = None
    payoff_projection_current: bool = True
    capital_authorization: CandidateCapitalAuthorization | None = None

    @model_validator(mode="after")
    def forecast_permission_must_be_explicit(self):
        if isinstance(self.forecast, BaseForecast):
            permission = self.capital_authorization
            if permission is None or (
                permission.producer_id != self.forecast.producer_id
                or permission.producer_behavior_id != self.forecast.producer_behavior_id
                or permission.outcome_family_id != self.forecast.outcome_family_id
            ):
                raise ValueError("BaseForecast 必须精确绑定 candidate capital authorization")
        elif self.capital_authorization is not None:
            raise ValueError("CalibratedForecast 不得使用 candidate capital authorization")
        projection = self.payoff_projection
        if projection is None and not self.payoff_projection_current:
            raise ValueError("缺少 Product payoff projection 时不能声明产品输入失效")
        if projection is not None and (
            not isinstance(self.forecast, BaseForecast)
            or projection.source_forecast_id != self.forecast.forecast_id
            or projection.source_contract_id != self.forecast.contract_id
            or projection.projected_at < self.forecast.available_at
            or projection.source_entry_valid_until != self.forecast.valid_until
            or projection.evaluation_at != self.forecast.economic_horizon_end
        ):
            raise ValueError("Product payoff projection 与源 Forecast 不一致")
        return self

    @property
    def target(self) -> ForecastTarget:
        return (
            self.payoff_projection.target
            if self.payoff_projection is not None
            else self.forecast.target
        )

    @property
    def available_at(self) -> datetime:
        return (
            self.payoff_projection.projected_at
            if self.payoff_projection is not None
            else self.forecast.available_at
        )

    @property
    def valid_until(self) -> datetime:
        return (
            self.payoff_projection.valid_until
            if self.payoff_projection is not None
            else self.forecast.economic_horizon_end
        )

    @property
    def new_exposure_valid_until(self) -> datetime:
        return (
            min(
                self.payoff_projection.valid_until,
                self.payoff_projection.source_entry_valid_until,
            )
            if self.payoff_projection is not None
            else self.forecast.valid_until
        )

    @property
    def economic_exposure_key(self) -> str:
        return (
            self.payoff_projection.economic_exposure_id
            if self.payoff_projection is not None
            else self.sleeve_id
        )


def remaining_target_gross_bps(
    forecast: Forecast,
    *,
    payoff_projection: ProductPayoffProjection | None = None,
    current_gross_notional: Decimal,
    evaluation_gross_notional: Decimal,
    quote_by_instrument: dict[str, ExecutableQuote],
    as_of: datetime,
) -> Decimal:
    """Reprice retained exposure and new exposure from their executable sides."""

    require_utc(as_of)
    if current_gross_notional < 0 or evaluation_gross_notional <= 0:
        raise ValueError("Forecast 重估金额必须为正且当前金额不能为负")
    forecast_gross_bps = (
        payoff_projection.conservative_gross_bps
        if payoff_projection is not None
        else forecast.expected_gross_bps
        if isinstance(forecast, BaseForecast)
        else forecast.conservative_gross_bps
    )
    target = payoff_projection.target if payoff_projection is not None else forecast.target
    cutoff = (
        {payoff_projection.entry_anchor.instrument_id: payoff_projection.entry_anchor.price}
        if payoff_projection is not None
        else {item.instrument_id: item.price for item in forecast.cutoff_prices}
    )
    realized_to_entry_bps = Decimal("0")
    retained_fraction = min(current_gross_notional, evaluation_gross_notional) / (
        evaluation_gross_notional
    )
    added_fraction = Decimal("1") - retained_fraction
    for leg in target.legs:
        quote = quote_by_instrument[leg.instrument.key]
        new_entry_price = quote.ask if leg.direction == ExposureDirection.LONG else quote.bid
        retained_price = quote.bid if leg.direction == ExposureDirection.LONG else quote.ask
        decision_basis_price = (
            retained_fraction * retained_price + added_fraction * new_entry_price
        )
        sign = Decimal("1") if leg.direction == ExposureDirection.LONG else Decimal("-1")
        realized_to_entry_bps += (
            sign
            * leg.gross_weight
            * (decision_basis_price / cutoff[leg.instrument.key] - Decimal("1"))
            * Decimal("10000")
        )
    return forecast_gross_bps - realized_to_entry_bps


def estimate_state_transition_cost(
    *,
    policy: PortfolioDecisionPolicy,
    forecast: Forecast,
    payoff_projection: ProductPayoffProjection | None = None,
    current_gross_notional: Decimal,
    target_gross_notional: Decimal,
    evaluation_gross_notional: Decimal,
    quote_by_instrument: dict[str, ExecutableQuote],
    spec_by_instrument: dict[str, InstrumentExecutionSpec],
) -> PortfolioCostEstimate:
    """Estimate only future costs from the reconciled state to one target."""

    if min(current_gross_notional, target_gross_notional) < 0:
        raise ValueError("Portfolio 当前与目标金额不能为负")
    if evaluation_gross_notional <= 0:
        raise ValueError("Portfolio 成本评估金额必须为正")

    fee_bps = Decimal("0")
    exit_spread_bps = Decimal("0")
    depth_slippage_bps = Decimal("0")
    refs = []
    added_notional = max(target_gross_notional - current_gross_notional, Decimal("0"))
    reduced_notional = max(current_gross_notional - target_gross_notional, Decimal("0"))
    target = payoff_projection.target if payoff_projection is not None else forecast.target
    for leg in target.legs:
        quote = quote_by_instrument[leg.instrument.key]
        spec = spec_by_instrument[leg.instrument.key]
        refs.append(quote.source_quote_id)
        fee_bps += (
            leg.gross_weight
            * (added_notional + reduced_notional + target_gross_notional)
            / evaluation_gross_notional
            * spec.fee_bps
        )
        half_spread = (quote.ask - quote.bid) / (quote.ask + quote.bid) * Decimal("10000")
        exit_spread_bps += (
            leg.gross_weight
            * target_gross_notional
            / evaluation_gross_notional
            * half_spread
            * policy.exit_spread_multiplier
        )
        entry_price = quote.ask if leg.direction == ExposureDirection.LONG else quote.bid
        entry_quantity = (
            quote.ask_quantity if leg.direction == ExposureDirection.LONG else quote.bid_quantity
        )
        exit_price = quote.bid if leg.direction == ExposureDirection.LONG else quote.ask
        exit_quantity = (
            quote.bid_quantity if leg.direction == ExposureDirection.LONG else quote.ask_quantity
        )
        entry_depth = entry_price * entry_quantity * leg.instrument.contract_multiplier
        exit_depth = exit_price * exit_quantity * leg.instrument.contract_multiplier
        depth_slippage_bps += _depth_cost_bps(
            sleeve_notional=added_notional,
            evaluation_gross_notional=evaluation_gross_notional,
            leg_weight=leg.gross_weight,
            depth_notional=entry_depth,
            half_spread_bps=half_spread,
            multiplier=policy.depth_slippage_multiplier,
        )
        for sleeve_notional in (reduced_notional, target_gross_notional):
            depth_slippage_bps += _depth_cost_bps(
                sleeve_notional=sleeve_notional,
                evaluation_gross_notional=evaluation_gross_notional,
                leg_weight=leg.gross_weight,
                depth_notional=exit_depth,
                half_spread_bps=half_spread,
                multiplier=policy.depth_slippage_multiplier,
            )
    return PortfolioCostEstimate(
        model_version=policy.cost_model_version,
        gross_notional=evaluation_gross_notional,
        fee_bps=fee_bps,
        exit_spread_bps=exit_spread_bps,
        depth_slippage_bps=depth_slippage_bps,
        total_bps=fee_bps + exit_spread_bps + depth_slippage_bps,
        quote_refs=tuple(sorted(refs)),
    )


def _depth_cost_bps(
    *,
    sleeve_notional: Decimal,
    evaluation_gross_notional: Decimal,
    leg_weight: Decimal,
    depth_notional: Decimal,
    half_spread_bps: Decimal,
    multiplier: Decimal,
) -> Decimal:
    leg_notional = sleeve_notional * leg_weight
    if leg_notional <= depth_notional:
        return Decimal("0")
    return (
        leg_notional
        / evaluation_gross_notional
        * half_spread_bps
        * multiplier
        * (leg_notional / depth_notional - Decimal("1"))
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
            leg.instrument.key for item in sleeves for leg in item.target.legs
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
                forecast_target_id=item.target.target_id,
            )
            if item.sleeve_id != expected_id:
                raise ValueError("PortfolioSleeveInput sleeve_id 与 Forecast 不一致")

        single_limit = account.equity * self._policy.maximum_single_sleeve_fraction
        candidate_notional = {
            item.sleeve_id: self._candidate_notional(
                item,
                allocation_limit=min(
                    single_limit,
                    self._allocation_limit(item, equity=account.equity),
                ),
                current_notional=current_by_sleeve[item.sleeve_id],
                as_of=as_of,
            )
            for item in sleeves
        }
        candidate_evaluations = {
            item.sleeve_id: self._evaluate_candidate(
                item,
                quote_by_instrument=quote_by_instrument,
                spec_by_instrument=spec_by_instrument,
                as_of=as_of,
                current_notional=current_by_sleeve[item.sleeve_id],
                evaluation_notional=candidate_notional[item.sleeve_id],
            )
            for item in sleeves
        }
        eligible = tuple(
            sorted(
                (
                    item
                    for item in sleeves
                    if candidate_evaluations[item.sleeve_id].eligible
                ),
                key=lambda item: (
                    -candidate_evaluations[item.sleeve_id].decision_net_bps,
                    0 if isinstance(item.forecast, CalibratedForecast) else 1,
                    item.sleeve_id,
                ),
            )
        )
        item_by_sleeve = {item.sleeve_id: item for item in sleeves}
        current_expression_by_exposure: dict[str, str] = {}
        for item in sleeves:
            if current_by_sleeve[item.sleeve_id] <= 0:
                continue
            existing = current_expression_by_exposure.get(item.economic_exposure_key)
            if existing is not None and existing != item.sleeve_id:
                raise ValueError("同一经济暴露同时存在多个产品持仓")
            current_expression_by_exposure[item.economic_exposure_key] = item.sleeve_id
        selected_expression_ids: set[str] = set()
        duplicate_expression_ids: set[str] = set()
        switch_exit_ids: set[str] = set()
        eligible_by_exposure: dict[str, list[PortfolioSleeveInput]] = {}
        for item in eligible:
            eligible_by_exposure.setdefault(item.economic_exposure_key, []).append(item)
        for exposure in sorted({item.economic_exposure_key for item in sleeves}):
            candidates = eligible_by_exposure.get(exposure, [])
            current_id = current_expression_by_exposure.get(exposure)
            if current_id is None:
                if candidates:
                    selected_expression_ids.add(candidates[0].sleeve_id)
                    duplicate_expression_ids.update(
                        item.sleeve_id for item in candidates[1:]
                    )
                continue
            current = item_by_sleeve[current_id]
            current_evaluation = candidate_evaluations[current_id]
            if not current_evaluation.eligible:
                duplicate_expression_ids.update(item.sleeve_id for item in candidates)
                continue
            best = candidates[0]
            if best.sleeve_id == current_id:
                selected_expression_ids.add(current_id)
                duplicate_expression_ids.update(
                    item.sleeve_id for item in candidates[1:]
                )
                continue
            immediate_exit = self._cost(
                current,
                current_notional=current_by_sleeve[current_id],
                target_notional=Decimal("0"),
                evaluation_notional=current_by_sleeve[current_id],
                quote_by_instrument=quote_by_instrument,
                spec_by_instrument=spec_by_instrument,
            )
            if (
                candidate_evaluations[best.sleeve_id].decision_net_bps
                - immediate_exit.total_bps
                > current_evaluation.decision_net_bps
            ):
                switch_exit_ids.add(current_id)
                duplicate_expression_ids.update(item.sleeve_id for item in candidates)
            else:
                selected_expression_ids.add(current_id)
                duplicate_expression_ids.update(
                    item.sleeve_id
                    for item in candidates
                    if item.sleeve_id != current_id
                )
        capacity_candidates = [
            item for item in eligible if item.sleeve_id in selected_expression_ids
        ]
        desired_by_sleeve: dict[str, Decimal] = {}
        remaining_capacity = account.equity * self._policy.maximum_total_exposure_fraction
        for item in capacity_candidates:
            desired = min(candidate_notional[item.sleeve_id], remaining_capacity)
            if desired <= 0:
                break
            desired_by_sleeve[item.sleeve_id] = desired
            remaining_capacity -= desired

        eligible_ids = set(desired_by_sleeve)
        targets = tuple(
            self._target(
                item,
                current_notional=current_by_sleeve[item.sleeve_id],
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
                    else "PRODUCT_SWITCH_EXIT_FIRST"
                    if item.sleeve_id in switch_exit_ids
                    else "FORECAST_INVALID_CASH"
                    if not candidate_evaluations[item.sleeve_id].forecast_current
                    else "EXPOSURE_CAPACITY_EXHAUSTED_CASH"
                    if candidate_evaluations[item.sleeve_id].eligible
                    and item.sleeve_id not in duplicate_expression_ids
                    else "ALTERNATIVE_PRODUCT_NOT_SELECTED"
                    if item.sleeve_id in duplicate_expression_ids
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
            and not candidate_evaluations[item.sleeve_id].forecast_current
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
                    current_notional=current_by_sleeve[item.sleeve_id],
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

        final_desired = {item.sleeve_id: item.desired_gross_notional for item in targets}
        capacity_selected_ids = set() if below_rebalance_minimum else eligible_ids
        frozen_candidate_evaluations = tuple(
            self._freeze_allocation_result(
                candidate_evaluations[item.sleeve_id],
                desired_notional=final_desired.get(item.sleeve_id, Decimal("0")),
                capacity_selected=item.sleeve_id in capacity_selected_ids,
                alternative_product_not_selected=(
                    item.sleeve_id in duplicate_expression_ids
                ),
                product_switch_exit=(item.sleeve_id in switch_exit_ids),
                rebalance_preserved=(
                    below_rebalance_minimum
                    and current_by_sleeve[item.sleeve_id] > 0
                ),
            )
            for item in sorted(sleeves, key=lambda value: value.sleeve_id)
        )

        reason_codes: set[str] = set()
        if below_rebalance_minimum:
            reason_codes.add("REBALANCE_BELOW_MINIMUM")
        else:
            if eligible_ids:
                reason_codes.add("POSITIVE_NET_EDGE_SELECTED")
            if not eligible_ids and duplicate_expression_ids:
                reason_codes.add("CASH_SELECTED_FOR_PRODUCT_TRANSITION")
            elif not eligible_ids:
                reason_codes.add("CASH_SELECTED_NO_POSITIVE_NET_EDGE")
        if invalid_holding_exit:
            reason_codes.add("EXPIRED_FORECAST_EXIT")
        if duplicate_expression_ids:
            reason_codes.add("ALTERNATIVE_PRODUCT_EXPRESSION_REJECTED")
        if switch_exit_ids and not below_rebalance_minimum:
            reason_codes.add("PRODUCT_SWITCH_EXIT_FIRST")

        valid_until = as_of + timedelta(minutes=self._policy.target_validity_minutes)
        if decision_valid_until is not None:
            valid_until = min(valid_until, decision_valid_until)
        if eligible_ids:
            valid_until = min(
                valid_until,
                *(
                    self._target_support_until(
                        item,
                        current_notional=current_by_sleeve[item.sleeve_id],
                        desired_notional=desired_by_sleeve[item.sleeve_id],
                    )
                    for item in sleeves
                    if item.sleeve_id in eligible_ids
                ),
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
            "considered_forecast_ids": tuple(
                sorted({item.forecast.forecast_id for item in sleeves})
            ),
            "candidate_evaluations": [
                item.model_dump(mode="json") for item in frozen_candidate_evaluations
            ],
            "quotes": [item.model_dump(mode="json") for item in quotes],
            "sleeves": [item.model_dump(mode="json") for item in targets],
            "reason_codes": tuple(sorted(reason_codes)),
        }
        return PortfolioTarget(
            target_id=stable_id("portfolio_target", content_hash(payload)),
            **payload,
        )

    def _evaluate_candidate(
        self,
        item: PortfolioSleeveInput,
        *,
        quote_by_instrument: dict[str, ExecutableQuote],
        spec_by_instrument: dict[str, InstrumentExecutionSpec],
        as_of: datetime,
        current_notional: Decimal,
        evaluation_notional: Decimal,
    ) -> PortfolioCandidateEvaluation:
        forecast_current, validity_reason_codes, validity_evidence_refs = (
            self._forecast_validity(
                item,
                as_of=as_of,
                current_notional=current_notional,
                evaluation_notional=evaluation_notional,
            )
        )
        gross = remaining_target_gross_bps(
            item.forecast,
            payoff_projection=item.payoff_projection,
            current_gross_notional=current_notional,
            evaluation_gross_notional=evaluation_notional,
            quote_by_instrument=quote_by_instrument,
            as_of=as_of,
        )
        cost = self._cost(
            item,
            current_notional=current_notional,
            target_notional=evaluation_notional,
            evaluation_notional=evaluation_notional,
            quote_by_instrument=quote_by_instrument,
            spec_by_instrument=spec_by_instrument,
        )
        if isinstance(item.forecast, CalibratedForecast):
            threshold = self._policy.minimum_conservative_net_bps
            edge_basis = PortfolioEdgeBasis.CALIBRATED_CONSERVATIVE
        else:
            permission = item.capital_authorization
            assert permission is not None
            threshold = (
                permission.minimum_hold_net_bps
                if current_notional > 0
                else permission.minimum_entry_net_bps
            )
            edge_basis = PortfolioEdgeBasis.EXPERIMENTAL_HYPOTHESIS
        net = gross - cost.total_bps
        eligible = forecast_current and net >= threshold
        return PortfolioCandidateEvaluation(
            sleeve_id=item.sleeve_id,
            forecast_id=item.forecast.forecast_id,
            payoff_projection_id=(
                item.payoff_projection.projection_id
                if item.payoff_projection is not None
                else None
            ),
            edge_basis=edge_basis,
            current_gross_notional=current_notional,
            evaluation_gross_notional=evaluation_notional,
            desired_gross_notional=Decimal("0"),
            forecast_current=forecast_current,
            decision_gross_bps=gross,
            cost=cost,
            decision_net_bps=net,
            minimum_net_bps=threshold,
            eligible=eligible,
            validity_reason_codes=validity_reason_codes,
            validity_evidence_refs=validity_evidence_refs,
            reason_codes=(
                "ELIGIBLE_FOR_ALLOCATION"
                if eligible
                else "FORECAST_INVALID_CASH"
                if not forecast_current
                else "NON_POSITIVE_NET_EDGE_CASH",
            ),
        )

    @staticmethod
    def _freeze_allocation_result(
        candidate: PortfolioCandidateEvaluation,
        *,
        desired_notional: Decimal,
        capacity_selected: bool,
        alternative_product_not_selected: bool,
        product_switch_exit: bool,
        rebalance_preserved: bool,
    ) -> PortfolioCandidateEvaluation:
        if rebalance_preserved:
            reason = "REBALANCE_BELOW_MINIMUM_CURRENT_TARGET"
        elif capacity_selected:
            reason = "POSITIVE_NET_EDGE_SELECTED"
        elif product_switch_exit:
            reason = "PRODUCT_SWITCH_EXIT_FIRST"
        elif alternative_product_not_selected:
            reason = "ALTERNATIVE_PRODUCT_NOT_SELECTED"
        elif candidate.eligible:
            reason = "EXPOSURE_CAPACITY_EXHAUSTED_CASH"
        elif not candidate.forecast_current:
            reason = "FORECAST_INVALID_CASH"
        else:
            reason = "NON_POSITIVE_NET_EDGE_CASH"
        return PortfolioCandidateEvaluation.model_validate(
            {
                **candidate.model_dump(mode="python"),
                "desired_gross_notional": desired_notional,
                "reason_codes": (reason,),
            }
        )

    @staticmethod
    def _forecast_validity(
        item: PortfolioSleeveInput,
        *,
        as_of: datetime,
        current_notional: Decimal,
        evaluation_notional: Decimal,
    ) -> tuple[bool, tuple[str, ...], tuple[str, ...]]:
        if not item.payoff_projection_current:
            projection = item.payoff_projection
            assert projection is not None
            return (
                False,
                ("PRODUCT_PAYOFF_INPUT_INVALID",),
                (projection.projection_id,),
            )
        retaining_only = (
            current_notional > 0 and evaluation_notional <= current_notional
        )
        support_until = (
            item.valid_until if retaining_only else item.new_exposure_valid_until
        )
        if not (item.available_at <= as_of < support_until):
            return False, ("FORECAST_TIME_WINDOW_INVALID",), ()
        return True, (), ()

    @staticmethod
    def _candidate_notional(
        item: PortfolioSleeveInput,
        *,
        allocation_limit: Decimal,
        current_notional: Decimal,
        as_of: datetime,
    ) -> Decimal:
        if current_notional > 0 and as_of >= item.new_exposure_valid_until:
            return min(allocation_limit, current_notional)
        return allocation_limit

    @staticmethod
    def _target_support_until(
        item: PortfolioSleeveInput,
        *,
        current_notional: Decimal,
        desired_notional: Decimal,
    ) -> datetime:
        if current_notional > 0 and desired_notional <= current_notional:
            return item.valid_until
        return item.new_exposure_valid_until

    def _target(
        self,
        item: PortfolioSleeveInput,
        *,
        current_notional: Decimal,
        desired_notional: Decimal,
        evaluation_notional: Decimal,
        quote_by_instrument: dict[str, ExecutableQuote],
        spec_by_instrument: dict[str, InstrumentExecutionSpec],
        as_of: datetime,
        allocation_reason: str,
    ) -> SleeveTarget:
        gross = (
            remaining_target_gross_bps(
                item.forecast,
                payoff_projection=item.payoff_projection,
                current_gross_notional=current_notional,
                evaluation_gross_notional=evaluation_notional,
                quote_by_instrument=quote_by_instrument,
                as_of=as_of,
            )
            if desired_notional > 0
            else Decimal("0")
        )
        cost = self._cost(
            item,
            current_notional=current_notional,
            target_notional=desired_notional,
            evaluation_notional=evaluation_notional,
            quote_by_instrument=quote_by_instrument,
            spec_by_instrument=spec_by_instrument,
        )
        return SleeveTarget(
            sleeve_id=item.sleeve_id,
            forecast_family=item.forecast.outcome_family_id,
            forecast_target=item.target,
            desired_gross_notional=desired_notional,
            forecast_ids=(item.forecast.forecast_id,),
            payoff_projection_id=(
                item.payoff_projection.projection_id
                if item.payoff_projection is not None
                else None
            ),
            edge_basis=(
                PortfolioEdgeBasis.EXPERIMENTAL_HYPOTHESIS
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
                        *(
                            (f"PAYOFF_PROJECTION:{item.payoff_projection.projection_id}",)
                            if item.payoff_projection is not None
                            else ()
                        ),
                        allocation_reason,
                    )
                )
            ),
        )

    def _cost(
        self,
        item: PortfolioSleeveInput,
        *,
        current_notional: Decimal,
        target_notional: Decimal,
        evaluation_notional: Decimal,
        quote_by_instrument: dict[str, ExecutableQuote],
        spec_by_instrument: dict[str, InstrumentExecutionSpec],
    ) -> PortfolioCostEstimate:
        return estimate_state_transition_cost(
            policy=self._policy,
            forecast=item.forecast,
            payoff_projection=item.payoff_projection,
            current_gross_notional=current_notional,
            target_gross_notional=target_notional,
            evaluation_gross_notional=evaluation_notional,
            quote_by_instrument=quote_by_instrument,
            spec_by_instrument=spec_by_instrument,
        )

    @staticmethod
    def _allocation_limit(item: PortfolioSleeveInput, *, equity: Decimal) -> Decimal:
        if item.capital_authorization is None:
            return equity
        return equity * item.capital_authorization.maximum_allocation_fraction

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
