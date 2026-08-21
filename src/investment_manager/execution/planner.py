from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import Field, field_validator, model_validator

from investment_manager.execution.models import OrderType, Side
from investment_manager.forecast.models import ExposureDirection
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import (
    FrozenModel,
    Money,
    PositiveDecimal,
    floor_to_step,
)
from investment_manager.market.models import ExecutableQuote, InstrumentId
from investment_manager.portfolio.models import (
    PortfolioAccountSnapshot,
    SleevePosition,
    sleeve_gross_notional,
)
from investment_manager.risk.portfolio import (
    ApprovedPortfolioTarget,
    ApprovedSleeve,
)


class TradePlannerPolicy(FrozenModel):
    version: str = Field(min_length=1)
    managed_instruments: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def managed_instruments_must_be_unique_and_sorted(self):
        if tuple(sorted(set(self.managed_instruments))) != self.managed_instruments:
            raise ValueError("managed_instruments 必须唯一且排序")
        return self


class InstrumentExecutionSpec(FrozenModel):
    instrument: InstrumentId
    quantity_step: PositiveDecimal
    minimum_order_notional: Money


class SleeveTargetDelta(FrozenModel):
    sleeve_id: str = Field(min_length=1)
    current_gross_notional: Money
    desired_gross_notional: Money
    delta_gross_notional: Decimal

    @model_validator(mode="after")
    def delta_must_equal_desired_minus_current(self):
        if self.delta_gross_notional != (
            self.desired_gross_notional - self.current_gross_notional
        ):
            raise ValueError("SleeveTargetDelta 必须等于目标减当前 gross notional")
        return self


class PlanningOmission(FrozenModel):
    sleeve_id: str = Field(min_length=1)
    delta_gross_notional: Decimal
    reason_code: str = Field(min_length=1)


class PlannedLegTrade(FrozenModel):
    leg_id: str = Field(min_length=1)
    group_id: str = Field(min_length=1)
    approved_target_id: str = Field(min_length=1)
    cycle_id: str = Field(min_length=1)
    sleeve_id: str = Field(min_length=1)
    instrument: InstrumentId
    side: Side
    order_type: OrderType = OrderType.MARKET
    quantity: PositiveDecimal
    reference_price: PositiveDecimal
    quote_notional: PositiveDecimal
    reduce_only: bool
    valid_until: datetime

    _utc_valid_until = field_validator("valid_until")(require_utc)

    @model_validator(mode="after")
    def leg_trade_must_be_self_consistent(self):
        if self.quote_notional != (
            self.quantity
            * self.reference_price
            * self.instrument.contract_multiplier
        ):
            raise ValueError("PlannedLegTrade quote_notional 与数量价格不一致")
        expected_id = stable_id(
            "planned_leg",
            self.group_id,
            self.instrument.key,
            self.side.value,
            str(self.quantity),
            str(self.reduce_only),
        )
        if self.leg_id != expected_id:
            raise ValueError("PlannedLegTrade leg_id 与冻结内容不一致")
        return self


class PlannedTradeGroup(FrozenModel):
    group_id: str = Field(min_length=1)
    approved_target_id: str = Field(min_length=1)
    cycle_id: str = Field(min_length=1)
    sleeve_id: str = Field(min_length=1)
    planner_policy_version: str = Field(min_length=1)
    desired_gross_notional: Money
    maximum_unhedged_notional: Money
    maximum_unhedged_seconds: int = Field(gt=0)
    legs: tuple[PlannedLegTrade, ...] = Field(min_length=1)
    valid_until: datetime

    _utc_valid_until = field_validator("valid_until")(require_utc)

    @model_validator(mode="after")
    def group_identity_and_legs_must_be_consistent(self):
        expected_group_id = stable_id(
            "trade_group",
            self.approved_target_id,
            self.sleeve_id,
            self.planner_policy_version,
        )
        if self.group_id != expected_group_id:
            raise ValueError("PlannedTradeGroup group_id 不一致")
        if self.maximum_unhedged_notional > self.desired_gross_notional:
            raise ValueError("Trade group 未对冲上限不能超过目标 gross notional")
        leg_keys = tuple(item.instrument.key for item in self.legs)
        if tuple(sorted(set(leg_keys))) != leg_keys:
            raise ValueError("PlannedTradeGroup legs 必须按 Instrument 唯一且排序")
        if any(
            item.group_id != self.group_id
            or item.approved_target_id != self.approved_target_id
            or item.cycle_id != self.cycle_id
            or item.sleeve_id != self.sleeve_id
            or item.valid_until != self.valid_until
            for item in self.legs
        ):
            raise ValueError("PlannedTradeGroup 与 Leg 身份或有效期不一致")
        return self


class TradePlan(FrozenModel):
    plan_id: str = Field(min_length=1)
    approved_target_id: str = Field(min_length=1)
    cycle_id: str = Field(min_length=1)
    planner_policy_version: str = Field(min_length=1)
    created_at: datetime
    target_deltas: tuple[SleeveTargetDelta, ...]
    groups: tuple[PlannedTradeGroup, ...]
    omissions: tuple[PlanningOmission, ...]
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    _utc_created_at = field_validator("created_at")(require_utc)

    @model_validator(mode="after")
    def plan_order_and_identity_must_be_deterministic(self):
        delta_ids = tuple(item.sleeve_id for item in self.target_deltas)
        group_ids = tuple(item.sleeve_id for item in self.groups)
        omission_ids = tuple(item.sleeve_id for item in self.omissions)
        for values, label in (
            (delta_ids, "SleeveTargetDelta"),
            (group_ids, "PlannedTradeGroup"),
            (omission_ids, "PlanningOmission"),
        ):
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"{label} 必须按 sleeve_id 唯一且排序")
        payload = self.model_dump(mode="json", exclude={"plan_hash"})
        if self.plan_hash != content_hash(payload):
            raise ValueError("TradePlan plan_hash 与内容不一致")
        return self


class TradePlanner:
    """Translate each authorized Sleeve into one all-or-nothing planned group."""

    def __init__(self, policy: TradePlannerPolicy) -> None:
        self._policy = policy

    def plan(
        self,
        *,
        approved: ApprovedPortfolioTarget,
        account: PortfolioAccountSnapshot,
        quotes: tuple[ExecutableQuote, ...],
        specs: tuple[InstrumentExecutionSpec, ...],
        as_of: datetime,
    ) -> TradePlan:
        as_of = require_utc(as_of)
        if approved.valid_until <= as_of:
            raise ValueError("ApprovedPortfolioTarget 已过期")
        if approved.as_of != as_of:
            raise ValueError("TradePlanner 必须使用 Risk 批准时点")
        if content_hash(account) != approved.account_snapshot_hash:
            raise ValueError("TradePlanner account 与 Risk 批准快照不一致")
        quote_by_instrument = self._unique_quotes(quotes)
        spec_by_instrument = self._unique_specs(specs)
        approved_quote_hashes = set(approved.quote_hashes)
        current_by_sleeve = {item.sleeve_id: item for item in account.sleeves}

        deltas: list[SleeveTargetDelta] = []
        groups: list[PlannedTradeGroup] = []
        omissions: list[PlanningOmission] = []
        for sleeve in approved.sleeves:
            target_keys = tuple(
                item.instrument.key for item in sleeve.forecast_target.legs
            )
            if not set(target_keys).issubset(self._policy.managed_instruments):
                raise ValueError("ApprovedPortfolioTarget 包含未托管 Instrument")
            current = current_by_sleeve.get(sleeve.sleeve_id)
            current_gross = sleeve_gross_notional(
                current,
                quote_by_instrument=quote_by_instrument,
            )
            delta = SleeveTargetDelta(
                sleeve_id=sleeve.sleeve_id,
                current_gross_notional=current_gross,
                desired_gross_notional=sleeve.approved_gross_notional,
                delta_gross_notional=sleeve.approved_gross_notional - current_gross,
            )
            deltas.append(delta)
            if delta.delta_gross_notional == 0:
                continue
            if account.pending_execution_group_ids:
                omissions.append(
                    PlanningOmission(
                        sleeve_id=sleeve.sleeve_id,
                        delta_gross_notional=delta.delta_gross_notional,
                        reason_code="EXECUTION_GROUP_REQUIRES_RECONCILIATION",
                    )
                )
                continue
            group, reason = self._group(
                approved=approved,
                sleeve=sleeve,
                current=current,
                quote_by_instrument=quote_by_instrument,
                spec_by_instrument=spec_by_instrument,
                approved_quote_hashes=approved_quote_hashes,
            )
            if group is None:
                omissions.append(
                    PlanningOmission(
                        sleeve_id=sleeve.sleeve_id,
                        delta_gross_notional=delta.delta_gross_notional,
                        reason_code=reason,
                    )
                )
            else:
                groups.append(group)

        values = {
            "approved_target_id": approved.approved_target_id,
            "cycle_id": approved.cycle_id,
            "planner_policy_version": self._policy.version,
            "created_at": as_of,
            "target_deltas": tuple(deltas),
            "groups": tuple(groups),
            "omissions": tuple(omissions),
        }
        plan_id = stable_id(
            "trade_plan",
            approved.approved_target_id,
            self._policy.version,
            content_hash(values),
        )
        payload = {"plan_id": plan_id, **values}
        return TradePlan(**payload, plan_hash=content_hash(payload))

    def _group(
        self,
        *,
        approved: ApprovedPortfolioTarget,
        sleeve: ApprovedSleeve,
        current: SleevePosition | None,
        quote_by_instrument: dict[str, ExecutableQuote],
        spec_by_instrument: dict[str, InstrumentExecutionSpec],
        approved_quote_hashes: set[str],
    ) -> tuple[PlannedTradeGroup | None, str]:
        target_legs = sleeve.forecast_target.legs
        if any(
            leg.instrument.key not in quote_by_instrument
            or leg.instrument.key not in spec_by_instrument
            for leg in target_legs
        ):
            return None, "QUOTE_OR_EXECUTION_SPEC_MISSING"
        for leg in target_legs:
            if content_hash(quote_by_instrument[leg.instrument.key]) not in (
                approved_quote_hashes
            ):
                raise ValueError("交易报价与 Risk 批准快照不一致")

        group_id = stable_id(
            "trade_group",
            approved.approved_target_id,
            sleeve.sleeve_id,
            self._policy.version,
        )
        current_quantities = {
            item.instrument.key: item.quantity
            for item in current.legs
        } if current is not None else {}
        trades: list[PlannedLegTrade] = []
        opening_leg_below_minimum = False
        reducing_leg_below_minimum = False
        for leg in target_legs:
            quote = quote_by_instrument[leg.instrument.key]
            spec = spec_by_instrument[leg.instrument.key]
            sign = Decimal("1") if leg.direction == ExposureDirection.LONG else Decimal("-1")
            entry_price = quote.ask if sign > 0 else quote.bid
            desired_quantity = sign * floor_to_step(
                (
                    sleeve.approved_gross_notional
                    * leg.gross_weight
                    / entry_price
                    / leg.instrument.contract_multiplier
                ),
                spec.quantity_step,
            )
            current_quantity = current_quantities.get(leg.instrument.key, Decimal("0"))
            delta_quantity = desired_quantity - current_quantity
            if delta_quantity == 0:
                continue
            side = Side.BUY if delta_quantity > 0 else Side.SELL
            reference_price = quote.ask if side == Side.BUY else quote.bid
            quantity = floor_to_step(abs(delta_quantity), spec.quantity_step)
            quote_notional = (
                quantity
                * reference_price
                * leg.instrument.contract_multiplier
            )
            reducing = abs(desired_quantity) < abs(current_quantity)
            if quantity <= 0 or quote_notional < spec.minimum_order_notional:
                opening_leg_below_minimum |= not reducing
                reducing_leg_below_minimum |= reducing
                continue
            leg_id = stable_id(
                "planned_leg",
                group_id,
                leg.instrument.key,
                side.value,
                str(quantity),
                str(reducing),
            )
            trades.append(
                PlannedLegTrade(
                    leg_id=leg_id,
                    group_id=group_id,
                    approved_target_id=approved.approved_target_id,
                    cycle_id=approved.cycle_id,
                    sleeve_id=sleeve.sleeve_id,
                    instrument=leg.instrument,
                    side=side,
                    quantity=quantity,
                    reference_price=reference_price,
                    quote_notional=quote_notional,
                    reduce_only=reducing,
                    valid_until=approved.valid_until,
                )
            )
        if opening_leg_below_minimum:
            return None, "GROUP_NEW_RISK_LEG_BELOW_EXECUTION_MINIMUM"
        if reducing_leg_below_minimum:
            return None, "GROUP_REDUCTION_LEG_BELOW_EXECUTION_MINIMUM"
        if not trades:
            return None, "GROUP_DELTA_BELOW_EXECUTION_MINIMUM"
        return (
            PlannedTradeGroup(
                group_id=group_id,
                approved_target_id=approved.approved_target_id,
                cycle_id=approved.cycle_id,
                sleeve_id=sleeve.sleeve_id,
                planner_policy_version=self._policy.version,
                desired_gross_notional=sleeve.approved_gross_notional,
                maximum_unhedged_notional=sleeve.maximum_unhedged_notional,
                maximum_unhedged_seconds=sleeve.maximum_unhedged_seconds,
                legs=tuple(trades),
                valid_until=approved.valid_until,
            ),
            "GROUP_PLANNED",
        )

    @staticmethod
    def _unique_quotes(
        quotes: tuple[ExecutableQuote, ...],
    ) -> dict[str, ExecutableQuote]:
        keys = tuple(item.instrument.key for item in quotes)
        if tuple(sorted(set(keys))) != keys:
            raise ValueError("ExecutableQuote 必须按 Instrument 唯一且排序")
        return {item.instrument.key: item for item in quotes}

    @staticmethod
    def _unique_specs(
        specs: tuple[InstrumentExecutionSpec, ...],
    ) -> dict[str, InstrumentExecutionSpec]:
        keys = tuple(item.instrument.key for item in specs)
        if tuple(sorted(set(keys))) != keys:
            raise ValueError("InstrumentExecutionSpec 必须按 Instrument 唯一且排序")
        return {item.instrument.key: item for item in specs}
