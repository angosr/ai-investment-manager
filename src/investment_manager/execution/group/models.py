"""Immutable state for one recoverable multi-leg execution group."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from investment_manager.execution.models import Side
from investment_manager.execution.planning.planner import PlannedLegTrade, PlannedTradeGroup
from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.time import optional_utc, require_utc
from investment_manager.kernel.types import FrozenModel, Money, PositiveDecimal
from investment_manager.market.models import InstrumentId


class ExecutionGroupStatus(StrEnum):
    EXECUTING = "EXECUTING"
    RECOVERING = "RECOVERING"
    COMPENSATING = "COMPENSATING"
    HEDGED = "HEDGED"
    FLAT = "FLAT"

    @property
    def terminal(self) -> bool:
        return self in {self.HEDGED, self.FLAT}


class ExecutionLegRole(StrEnum):
    TARGET = "TARGET"
    COMPENSATION = "COMPENSATION"


class ExecutionLegStatus(StrEnum):
    PENDING = "PENDING"
    WORKING = "WORKING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"

    @property
    def terminal(self) -> bool:
        return self in {
            self.FILLED,
            self.CANCELED,
            self.REJECTED,
            self.EXPIRED,
        }


class ExecutionLeg(FrozenModel):
    execution_leg_id: str = Field(min_length=1)
    planned_leg_id: str = Field(min_length=1)
    group_id: str = Field(min_length=1)
    role: ExecutionLegRole
    attempt: int = Field(ge=0)
    client_order_id: str = Field(min_length=1, max_length=36)
    instrument: InstrumentId
    side: Side
    requested_quantity: PositiveDecimal
    reference_price: PositiveDecimal
    status: ExecutionLegStatus
    filled_quantity: Decimal = Field(ge=0)
    average_fill_price: PositiveDecimal | None = None
    fee: Money = Decimal("0")
    venue_order_id: str | None = None
    observed_at: datetime | None = None

    _utc_observed_at = field_validator("observed_at")(optional_utc)

    @model_validator(mode="after")
    def identity_and_fill_must_be_consistent(self):
        expected_leg_id = execution_leg_id(
            planned_leg_id=self.planned_leg_id,
            role=self.role,
            attempt=self.attempt,
        )
        if self.execution_leg_id != expected_leg_id:
            raise ValueError("ExecutionLeg identity 与 planned leg/role 不一致")
        if self.client_order_id != client_order_id(self.execution_leg_id):
            raise ValueError("ExecutionLeg client_order_id 不稳定")
        if self.filled_quantity > self.requested_quantity:
            raise ValueError("ExecutionLeg 成交数量不能超过请求数量")
        if (self.filled_quantity > 0) != (self.average_fill_price is not None):
            raise ValueError("ExecutionLeg 成交数量与均价必须同时存在")
        if self.status == ExecutionLegStatus.FILLED and (
            self.filled_quantity != self.requested_quantity
        ):
            raise ValueError("FILLED ExecutionLeg 必须全部成交")
        if (
            self.status
            in {
                ExecutionLegStatus.PENDING,
                ExecutionLegStatus.REJECTED,
            }
            and self.filled_quantity
        ):
            raise ValueError(f"{self.status} ExecutionLeg 不允许携带成交")
        if self.status == ExecutionLegStatus.PENDING and (
            self.venue_order_id is not None or self.observed_at is not None
        ):
            raise ValueError("PENDING ExecutionLeg 不应包含 Venue 事实")
        return self


class ExecutionGroup(FrozenModel):
    group_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    approved_target_id: str = Field(min_length=1)
    cycle_id: str = Field(min_length=1)
    sleeve_id: str = Field(min_length=1)
    status: ExecutionGroupStatus
    valid_until: datetime
    maximum_unhedged_notional: Money
    maximum_unhedged_seconds: int = Field(gt=0)
    target_legs: tuple[ExecutionLeg, ...] = Field(min_length=1)
    compensation_legs: tuple[ExecutionLeg, ...] = ()
    unhedged_since: datetime | None = None
    started_at: datetime
    updated_at: datetime
    revision: int = Field(ge=0)

    _utc_valid_until = field_validator("valid_until")(require_utc)
    _utc_unhedged_since = field_validator("unhedged_since")(optional_utc)
    _utc_started_at = field_validator("started_at")(require_utc)
    _utc_updated_at = field_validator("updated_at")(require_utc)

    @model_validator(mode="after")
    def group_state_must_be_self_consistent(self):
        if self.started_at > self.updated_at:
            raise ValueError("ExecutionGroup updated_at 不能早于 started_at")
        if self.unhedged_since is not None and not (
            self.started_at <= self.unhedged_since <= self.updated_at
        ):
            raise ValueError("ExecutionGroup unhedged_since 必须位于运行区间")
        for legs, role in (
            (self.target_legs, ExecutionLegRole.TARGET),
            (self.compensation_legs, ExecutionLegRole.COMPENSATION),
        ):
            ids = tuple(item.execution_leg_id for item in legs)
            if tuple(sorted(set(ids))) != ids:
                raise ValueError("ExecutionGroup legs 必须按 identity 唯一且排序")
            if any(item.group_id != self.group_id or item.role != role for item in legs):
                raise ValueError("ExecutionGroup leg group/role 不一致")
        if any(item.attempt != 0 for item in self.target_legs):
            raise ValueError("Target ExecutionLeg attempt 必须为 0")
        if any(item.attempt <= 0 for item in self.compensation_legs):
            raise ValueError("Compensation ExecutionLeg attempt 必须为正数")
        if self.compensation_legs:
            target_by_id = {item.planned_leg_id: item for item in self.target_legs}
            if {item.planned_leg_id for item in self.compensation_legs} - set(target_by_id):
                raise ValueError("补偿 Leg 必须引用本组 Target Leg")
            for leg in self.compensation_legs:
                target = target_by_id[leg.planned_leg_id]
                if (
                    leg.instrument != target.instrument
                    or leg.side == target.side
                    or leg.requested_quantity > target.filled_quantity
                    or leg.reference_price != target.reference_price
                ):
                    raise ValueError("补偿 Leg 必须精确反转 Target 已成交数量")
        if self.status == ExecutionGroupStatus.HEDGED:
            if not all(item.status == ExecutionLegStatus.FILLED for item in self.target_legs):
                raise ValueError("HEDGED group 的全部 Target Legs 必须成交")
            if self.compensation_legs:
                raise ValueError("HEDGED group 不得含补偿 Leg")
        if self.status == ExecutionGroupStatus.FLAT:
            if not all(
                item.status.terminal for item in (*self.target_legs, *self.compensation_legs)
            ):
                raise ValueError("FLAT group 的全部订单必须已确认终态")
            if self.residual_quantities:
                raise ValueError("FLAT group 不得包含本组残余数量")
        return self

    @property
    def terminal(self) -> bool:
        return self.status.terminal

    @property
    def residual_quantities(self) -> dict[str, Decimal]:
        quantities: dict[str, Decimal] = {}
        for leg in (*self.target_legs, *self.compensation_legs):
            signed = leg.filled_quantity if leg.side == Side.BUY else -leg.filled_quantity
            quantities[leg.instrument.key] = (
                quantities.get(leg.instrument.key, Decimal("0")) + signed
            )
        return {key: value for key, value in sorted(quantities.items()) if value != 0}

    @property
    def unhedged_notional(self) -> Decimal:
        """Gross unmatched progress across planned legs; one-leg groups have no mismatch."""

        progress = [item.filled_quantity / item.requested_quantity for item in self.target_legs]
        matched = min(progress)
        return sum(
            (fraction - matched)
            * leg.requested_quantity
            * (leg.average_fill_price or leg.reference_price)
            * leg.instrument.contract_multiplier
            for leg, fraction in zip(self.target_legs, progress, strict=True)
        )


def execution_leg_id(
    *,
    planned_leg_id: str,
    role: ExecutionLegRole,
    attempt: int,
) -> str:
    return stable_id("execution_leg", planned_leg_id, role.value, attempt)


def client_order_id(execution_leg_identity: str) -> str:
    return stable_id("group_order", execution_leg_identity)


def new_execution_group(
    *,
    plan_id: str,
    planned: PlannedTradeGroup,
    started_at: datetime,
) -> ExecutionGroup:
    started_at = require_utc(started_at)
    if started_at >= planned.valid_until:
        raise ValueError("不能启动已过期的 PlannedTradeGroup")
    target_legs = tuple(
        sorted(
            (_target_leg(item) for item in planned.legs),
            key=lambda item: item.execution_leg_id,
        )
    )
    return ExecutionGroup(
        group_id=planned.group_id,
        plan_id=plan_id,
        approved_target_id=planned.approved_target_id,
        cycle_id=planned.cycle_id,
        sleeve_id=planned.sleeve_id,
        status=ExecutionGroupStatus.EXECUTING,
        valid_until=planned.valid_until,
        maximum_unhedged_notional=planned.maximum_unhedged_notional,
        maximum_unhedged_seconds=planned.maximum_unhedged_seconds,
        target_legs=target_legs,
        started_at=started_at,
        updated_at=started_at,
        revision=0,
    )


def compensation_leg(
    target: ExecutionLeg,
    *,
    requested_quantity: Decimal,
    attempt: int,
) -> ExecutionLeg:
    if not target.status.terminal:
        raise ValueError("补偿前必须先确认 Target Leg 终态")
    if requested_quantity <= 0 or requested_quantity > target.filled_quantity:
        raise ValueError("零成交 Target Leg 不需要补偿")
    if attempt <= 0:
        raise ValueError("补偿 attempt 必须为正数")
    identity = execution_leg_id(
        planned_leg_id=target.planned_leg_id,
        role=ExecutionLegRole.COMPENSATION,
        attempt=attempt,
    )
    return ExecutionLeg(
        execution_leg_id=identity,
        planned_leg_id=target.planned_leg_id,
        group_id=target.group_id,
        role=ExecutionLegRole.COMPENSATION,
        attempt=attempt,
        client_order_id=client_order_id(identity),
        instrument=target.instrument,
        side=Side.SELL if target.side == Side.BUY else Side.BUY,
        requested_quantity=requested_quantity,
        reference_price=target.reference_price,
        status=ExecutionLegStatus.PENDING,
        filled_quantity=Decimal("0"),
    )


def _target_leg(planned: PlannedLegTrade) -> ExecutionLeg:
    identity = execution_leg_id(
        planned_leg_id=planned.leg_id,
        role=ExecutionLegRole.TARGET,
        attempt=0,
    )
    return ExecutionLeg(
        execution_leg_id=identity,
        planned_leg_id=planned.leg_id,
        group_id=planned.group_id,
        role=ExecutionLegRole.TARGET,
        attempt=0,
        client_order_id=client_order_id(identity),
        instrument=planned.instrument,
        side=planned.side,
        requested_quantity=planned.quantity,
        reference_price=planned.reference_price,
        status=ExecutionLegStatus.PENDING,
        filled_quantity=Decimal("0"),
    )
