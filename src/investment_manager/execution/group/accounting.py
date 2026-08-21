"""Deterministic Portfolio account projection from visible product order facts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from investment_manager.execution.group.models import (
    ExecutionGroup,
    ExecutionLegRole,
)
from investment_manager.execution.group.repository import ExecutionGroupStore
from investment_manager.execution.models import Side
from investment_manager.execution.venue.observation import (
    ProductOrderObservation,
    ProductOrderObservationStore,
)
from investment_manager.execution.venue.product import ProductOrder
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.market.models import (
    ExecutableQuote,
    InstrumentId,
    InstrumentProduct,
)
from investment_manager.market.perpetual.models import FundingSettlement
from investment_manager.portfolio.models import (
    InstrumentPosition,
    PortfolioAccountingTotals,
    PortfolioAccountSnapshot,
    SleevePosition,
)
from investment_manager.risk.portfolio import ApprovedSleeve, PortfolioRiskDecision


class PortfolioAccountHistory(Protocol):
    def latest_account(
        self,
        *,
        portfolio_id: str,
        as_of: datetime,
    ) -> PortfolioAccountSnapshot | None: ...


class ApprovedTargetReader(Protocol):
    def for_approved_targets(
        self,
        approved_target_ids: tuple[str, ...],
    ) -> dict[str, PortfolioRiskDecision]: ...


class FundingSettlementReader(Protocol):
    def funding_settlements(
        self,
        *,
        instrument: InstrumentId,
        start: datetime,
        end: datetime,
        visible_at: datetime,
    ) -> tuple[FundingSettlement, ...]: ...


@dataclass(slots=True)
class _PositionState:
    instrument: InstrumentId
    quantity: Decimal
    average_price: Decimal


class ProductAccountProjector:
    """Rebuild economics from facts; previous snapshots only carry daily/high-water baselines."""

    def __init__(
        self,
        *,
        portfolio_id: str,
        settlement_asset: str,
        initial_cash: Decimal,
    ) -> None:
        if not portfolio_id:
            raise ValueError("portfolio_id 不能为空")
        if not settlement_asset or initial_cash <= 0:
            raise ValueError("账户结算资产和初始现金必须有效")
        self._portfolio_id = portfolio_id
        self._settlement_asset = settlement_asset
        self._initial_cash = initial_cash

    @property
    def portfolio_id(self) -> str:
        return self._portfolio_id

    def project(
        self,
        *,
        cycle_id: str,
        as_of: datetime,
        groups: tuple[ExecutionGroup, ...],
        observation_history_by_group: Mapping[
            str, tuple[ProductOrderObservation, ...]
        ],
        funding_settlements: tuple[FundingSettlement, ...],
        approved_sleeves: tuple[ApprovedSleeve, ...],
        quotes: tuple[ExecutableQuote, ...],
        previous: PortfolioAccountSnapshot | None = None,
        reconciled: bool = True,
    ) -> PortfolioAccountSnapshot:
        as_of = require_utc(as_of)
        groups = self._groups(groups, as_of=as_of)
        sleeve_by_id = self._sleeves(approved_sleeves)
        quote_by_instrument = self._quotes(quotes, as_of=as_of)
        history = self._visible_order_history(
            groups,
            observations_by_group=observation_history_by_group,
            as_of=as_of,
        )
        orders = self._latest_orders(history)
        product_states: dict[str, _PositionState] = {}
        sleeve_states: dict[str, dict[str, _PositionState]] = {}
        cash = self._initial_cash
        group_by_id = {item.group_id: item for item in groups}
        for observation in orders:
            order = observation.order
            if order.filled_quantity <= 0:
                continue
            group = group_by_id[order.group_id]
            approved = sleeve_by_id.get(group.sleeve_id)
            if approved is None:
                raise ValueError("有成交的 ExecutionGroup 缺少 ApprovedSleeve 所有权")
            cash = self._apply_cash(cash, order, positions=product_states)
            self._apply_position(
                sleeve_states.setdefault(group.sleeve_id, {}),
                order,
            )
        funding_pnl = self._funding_pnl(
            history,
            settlements=funding_settlements,
            as_of=as_of,
        )
        cash += funding_pnl
        positions = self._positions(product_states)
        sleeves = self._sleeve_positions(
            sleeve_states,
            sleeve_by_id=sleeve_by_id,
        )
        equity = self._equity(
            cash,
            product_states=product_states,
            quote_by_instrument=quote_by_instrument,
        )
        if cash < 0:
            raise ValueError("产品账户投影产生负现金，拒绝生成权威快照")
        fee_cost = sum((item.order.fee for item in orders), Decimal("0"))
        net_pnl = equity - self._initial_cash
        accounting = PortfolioAccountingTotals(
            starting_equity=self._initial_cash,
            price_pnl=net_pnl - funding_pnl + fee_cost,
            funding_pnl=funding_pnl,
            fee_cost=fee_cost,
            execution_slippage_cost=self._execution_slippage_cost(
                groups=groups,
                orders=orders,
            ),
            compensation_loss=self._compensation_loss(
                groups=groups,
                orders=orders,
            ),
            net_pnl=net_pnl,
        )
        previous = self._previous(previous, as_of=as_of)
        if previous is None:
            daily_pnl = equity - self._initial_cash
        elif previous.as_of.date() == as_of.date():
            daily_pnl = previous.daily_pnl + equity - previous.equity
        else:
            # Attribute the overnight gap to the new UTC day; resetting to zero
            # would permanently drop the change between adjacent account facts.
            daily_pnl = equity - previous.equity
        equity_high_water = max(
            equity,
            previous.equity_high_water if previous is not None else self._initial_cash,
        )
        drawdown = (
            (equity_high_water - equity) / equity_high_water
            if equity_high_water > 0
            else Decimal("0")
        )
        pending = tuple(
            sorted(
                item.group_id for item in groups if not (item.terminal and item.updated_at <= as_of)
            )
        )
        payload = {
            "cycle_id": cycle_id,
            "portfolio_id": self._portfolio_id,
            "revision": previous.revision + 1 if previous is not None else 0,
            "as_of": as_of,
            "observed_at": as_of,
            "settlement_asset": self._settlement_asset,
            "cash_balance": cash,
            "equity": equity,
            "equity_high_water": equity_high_water,
            "daily_pnl": daily_pnl,
            "drawdown_fraction": drawdown,
            "accounting": accounting,
            "positions": positions,
            "sleeves": sleeves,
            "pending_execution_group_ids": pending,
            "kill_switch_active": previous.kill_switch_active if previous else False,
            "reconciled": reconciled,
        }
        return PortfolioAccountSnapshot(
            snapshot_id=stable_id("portfolio_account", content_hash(payload)),
            **payload,
        )

    @staticmethod
    def _execution_slippage_cost(
        *,
        groups: tuple[ExecutionGroup, ...],
        orders: tuple[ProductOrderObservation, ...],
    ) -> Decimal:
        leg_by_client = {
            leg.client_order_id: leg
            for group in groups
            for leg in (*group.target_legs, *group.compensation_legs)
        }
        cost = Decimal("0")
        for observation in orders:
            order = observation.order
            if order.filled_quantity <= 0:
                continue
            assert order.average_fill_price is not None
            leg = leg_by_client[order.client_order_id]
            price_cost = (
                order.average_fill_price - leg.reference_price
                if order.side == Side.BUY
                else leg.reference_price - order.average_fill_price
            )
            cost += (
                price_cost
                * order.filled_quantity
                * order.instrument.contract_multiplier
            )
        return cost

    @staticmethod
    def _compensation_loss(
        *,
        groups: tuple[ExecutionGroup, ...],
        orders: tuple[ProductOrderObservation, ...],
    ) -> Decimal:
        latest = {item.order.client_order_id: item.order for item in orders}
        loss = Decimal("0")
        for group in groups:
            target_by_planned = {
                item.planned_leg_id: item for item in group.target_legs
            }
            for leg in group.compensation_legs:
                compensation = latest.get(leg.client_order_id)
                target_leg = target_by_planned[leg.planned_leg_id]
                target = latest.get(target_leg.client_order_id)
                if (
                    compensation is None
                    or target is None
                    or compensation.filled_quantity <= 0
                    or target.filled_quantity <= 0
                ):
                    continue
                assert compensation.average_fill_price is not None
                assert target.average_fill_price is not None
                quantity = compensation.filled_quantity
                gross_loss = (
                    target.average_fill_price - compensation.average_fill_price
                    if target.side == Side.BUY
                    else compensation.average_fill_price - target.average_fill_price
                ) * quantity * target.instrument.contract_multiplier
                allocated_target_fee = target.fee * quantity / target.filled_quantity
                loss += max(
                    Decimal("0"),
                    gross_loss + allocated_target_fee + compensation.fee,
                )
        return loss

    @staticmethod
    def _groups(
        groups: tuple[ExecutionGroup, ...],
        *,
        as_of: datetime,
    ) -> tuple[ExecutionGroup, ...]:
        visible = tuple(item for item in groups if item.started_at <= as_of)
        ids = tuple(item.group_id for item in visible)
        if len(ids) != len(set(ids)):
            raise ValueError("ExecutionGroup 输入不得重复")
        return tuple(sorted(visible, key=lambda item: (item.started_at, item.group_id)))

    @staticmethod
    def _sleeves(sleeves: tuple[ApprovedSleeve, ...]) -> dict[str, ApprovedSleeve]:
        ids = tuple(item.sleeve_id for item in sleeves)
        if len(ids) != len(set(ids)):
            raise ValueError("ApprovedSleeve 输入不得重复")
        return {item.sleeve_id: item for item in sleeves}

    @staticmethod
    def _quotes(
        quotes: tuple[ExecutableQuote, ...],
        *,
        as_of: datetime,
    ) -> dict[str, ExecutableQuote]:
        keys = tuple(item.instrument.key for item in quotes)
        if tuple(sorted(set(keys))) != keys or any(item.as_of != as_of for item in quotes):
            raise ValueError("账户投影 quotes 必须按 Instrument 唯一排序并冻结在 as_of")
        return {item.instrument.key: item for item in quotes}

    @staticmethod
    def _visible_order_history(
        groups: tuple[ExecutionGroup, ...],
        *,
        observations_by_group: Mapping[str, tuple[ProductOrderObservation, ...]],
        as_of: datetime,
    ) -> tuple[ProductOrderObservation, ...]:
        leg_by_client = {}
        group_ids = {item.group_id for item in groups}
        if set(observations_by_group) != group_ids:
            raise ValueError("订单观察必须精确覆盖全部可见 ExecutionGroup")
        for group in groups:
            for leg in (*group.target_legs, *group.compensation_legs):
                leg_by_client[leg.client_order_id] = leg
        observations: list[ProductOrderObservation] = []
        for group_id, values in observations_by_group.items():
            if group_id not in group_ids:
                raise ValueError("订单观察引用不可见 ExecutionGroup")
            for observation in values:
                order = observation.order
                if (
                    observation.available_at > as_of
                    or order.group_id != group_id
                    or order.client_order_id not in leg_by_client
                ):
                    raise ValueError("产品订单观察超出点时或 Group/Leg 边界")
                leg = leg_by_client[order.client_order_id]
                if (
                    order.execution_leg_id != leg.execution_leg_id
                    or order.instrument != leg.instrument
                    or order.side != leg.side
                    or order.requested_quantity != leg.requested_quantity
                ):
                    raise ValueError("产品订单观察与 ExecutionLeg 合同不一致")
                observations.append(observation)
        ordered = tuple(
            sorted(
                observations,
                key=lambda item: (
                    item.available_at,
                    item.order.observed_at,
                    0
                    if leg_by_client[item.order.client_order_id].role == ExecutionLegRole.TARGET
                    else 1,
                    item.order.client_order_id,
                ),
            )
        )
        previous_by_client: dict[str, ProductOrder] = {}
        for observation in ordered:
            order = observation.order
            previous = previous_by_client.get(order.client_order_id)
            if previous is not None and (
                order.observed_at < previous.observed_at
                or order.filled_quantity < previous.filled_quantity
                or order.fee < previous.fee
            ):
                raise ValueError("产品订单观察的时间、累计成交或累计费用发生倒退")
            previous_by_client[order.client_order_id] = order
        return ordered

    @staticmethod
    def _latest_orders(
        history: tuple[ProductOrderObservation, ...],
    ) -> tuple[ProductOrderObservation, ...]:
        latest: dict[str, ProductOrderObservation] = {}
        for observation in history:
            latest[observation.order.client_order_id] = observation
        return tuple(
            sorted(
                latest.values(),
                key=lambda item: (
                    item.order.observed_at,
                    item.available_at,
                    item.order.client_order_id,
                ),
            )
        )

    @classmethod
    def _funding_pnl(
        cls,
        history: tuple[ProductOrderObservation, ...],
        *,
        settlements: tuple[FundingSettlement, ...],
        as_of: datetime,
    ) -> Decimal:
        settlement_keys = tuple(
            (item.funding_time, item.rate_type.value, item.settlement_id)
            for item in settlements
        )
        if tuple(sorted(set(settlement_keys))) != settlement_keys or any(
            item.observed_at > as_of or item.funding_time >= as_of
            for item in settlements
        ):
            raise ValueError("Funding 结算必须唯一、有序且在账户时点可见")
        fills = cls._incremental_fills(history)
        positions: dict[str, _PositionState] = {}
        cursor = 0
        pnl = Decimal("0")
        for settlement in settlements:
            while cursor < len(fills) and fills[cursor].observed_at <= settlement.funding_time:
                cls._apply_position(positions, fills[cursor])
                cursor += 1
            position = positions.get(settlement.instrument.key)
            if position is None:
                continue
            pnl -= (
                position.quantity
                * settlement.mark_price
                * settlement.instrument.contract_multiplier
                * settlement.funding_rate
            )
        return pnl

    @staticmethod
    def _incremental_fills(
        history: tuple[ProductOrderObservation, ...],
    ) -> tuple[ProductOrder, ...]:
        previous_by_client: dict[str, ProductOrder] = {}
        fills: list[ProductOrder] = []
        for observation in history:
            order = observation.order
            previous = previous_by_client.get(order.client_order_id)
            previous_quantity = (
                previous.filled_quantity if previous is not None else Decimal("0")
            )
            delta_quantity = order.filled_quantity - previous_quantity
            if delta_quantity > 0:
                assert order.average_fill_price is not None
                previous_notional = (
                    previous.filled_quantity * (previous.average_fill_price or Decimal("0"))
                    if previous is not None
                    else Decimal("0")
                )
                cumulative_notional = order.filled_quantity * order.average_fill_price
                delta_price = (cumulative_notional - previous_notional) / delta_quantity
                fills.append(
                    order.model_copy(
                        update={
                            "filled_quantity": delta_quantity,
                            "requested_quantity": delta_quantity,
                            "average_fill_price": delta_price,
                            "fee": order.fee - (previous.fee if previous is not None else 0),
                        }
                    )
                )
            previous_by_client[order.client_order_id] = order
        return tuple(
            sorted(
                fills,
                key=lambda item: (item.observed_at, item.client_order_id),
            )
        )

    def _apply_cash(
        self,
        cash: Decimal,
        order: ProductOrder,
        *,
        positions: dict[str, _PositionState],
    ) -> Decimal:
        assert order.average_fill_price is not None
        notional = (
            order.filled_quantity * order.average_fill_price * order.instrument.contract_multiplier
        )
        if order.instrument.product == InstrumentProduct.SPOT:
            cash += notional if order.side == Side.SELL else -notional
            self._apply_position(positions, order)
            return cash - order.fee
        realized = self._apply_position(positions, order)
        return cash + realized - order.fee

    @staticmethod
    def _apply_position(
        positions: dict[str, _PositionState],
        order: ProductOrder,
    ) -> Decimal:
        assert order.average_fill_price is not None
        key = order.instrument.key
        signed_fill = order.filled_quantity if order.side == Side.BUY else -order.filled_quantity
        current = positions.get(key)
        if current is None:
            if order.instrument.product == InstrumentProduct.SPOT and signed_fill < 0:
                raise ValueError("Spot 产品成交导致未建模负持仓")
            positions[key] = _PositionState(
                instrument=order.instrument,
                quantity=signed_fill,
                average_price=order.average_fill_price,
            )
            return Decimal("0")
        if current.quantity * signed_fill > 0:
            total = abs(current.quantity) + abs(signed_fill)
            current.average_price = (
                current.average_price * abs(current.quantity)
                + order.average_fill_price * abs(signed_fill)
            ) / total
            current.quantity += signed_fill
            return Decimal("0")
        closed = min(abs(current.quantity), abs(signed_fill))
        realized = (
            (order.average_fill_price - current.average_price)
            * closed
            * (Decimal("1") if current.quantity > 0 else Decimal("-1"))
            * order.instrument.contract_multiplier
        )
        remaining = current.quantity + signed_fill
        if remaining == 0:
            del positions[key]
        elif current.quantity * remaining > 0:
            current.quantity = remaining
        else:
            current.quantity = remaining
            current.average_price = order.average_fill_price
        if order.instrument.product == InstrumentProduct.SPOT and remaining < 0:
            raise ValueError("Spot 产品成交导致未建模负持仓")
        return realized

    @staticmethod
    def _positions(states: Mapping[str, _PositionState]) -> tuple[InstrumentPosition, ...]:
        return tuple(
            InstrumentPosition(
                instrument=item.instrument,
                quantity=item.quantity,
                average_price=item.average_price,
            )
            for _, item in sorted(states.items())
            if item.quantity != 0
        )

    def _sleeve_positions(
        self,
        states: Mapping[str, Mapping[str, _PositionState]],
        *,
        sleeve_by_id: Mapping[str, ApprovedSleeve],
    ) -> tuple[SleevePosition, ...]:
        values = []
        for sleeve_id, positions in sorted(states.items()):
            legs = self._positions(positions)
            if not legs:
                continue
            approved = sleeve_by_id[sleeve_id]
            values.append(
                SleevePosition(
                    sleeve_id=sleeve_id,
                    forecast_family=approved.forecast_family,
                    target=approved.forecast_target,
                    legs=legs,
                )
            )
        return tuple(values)

    @staticmethod
    def _equity(
        cash: Decimal,
        *,
        product_states: Mapping[str, _PositionState],
        quote_by_instrument: Mapping[str, ExecutableQuote],
    ) -> Decimal:
        equity = cash
        for key, position in product_states.items():
            quote = quote_by_instrument.get(key)
            if quote is None:
                raise ValueError("开放产品持仓缺少点时可成交报价")
            mark = quote.bid if position.quantity > 0 else quote.ask
            if position.instrument.product == InstrumentProduct.SPOT:
                equity += position.quantity * mark * position.instrument.contract_multiplier
            else:
                equity += (
                    (mark - position.average_price)
                    * position.quantity
                    * position.instrument.contract_multiplier
                )
        return equity

    def _previous(
        self,
        previous: PortfolioAccountSnapshot | None,
        *,
        as_of: datetime,
    ) -> PortfolioAccountSnapshot | None:
        if previous is None:
            return None
        if previous.portfolio_id != self._portfolio_id or previous.as_of > as_of:
            raise ValueError("previous PortfolioAccountSnapshot 不属于当前点时账户")
        return previous


class ProductAccountProjectionService:
    """Load one point-in-time fact set and delegate all economics to the pure projector."""

    def __init__(
        self,
        *,
        projector: ProductAccountProjector,
        groups: ExecutionGroupStore,
        observations: ProductOrderObservationStore,
        funding: FundingSettlementReader,
        risks: ApprovedTargetReader,
        accounts: PortfolioAccountHistory,
    ) -> None:
        self._projector = projector
        self._groups = groups
        self._observations = observations
        self._funding = funding
        self._risks = risks
        self._accounts = accounts

    @property
    def portfolio_id(self) -> str:
        return self._projector.portfolio_id

    def project(
        self,
        *,
        cycle_id: str,
        as_of: datetime,
        quotes: tuple[ExecutableQuote, ...],
        reconciled: bool = True,
    ) -> PortfolioAccountSnapshot:
        as_of = require_utc(as_of)
        groups = self._groups.visible(as_of=as_of)
        observation_history = self._observations.history_for_groups(
            tuple(group.group_id for group in groups),
            as_of=as_of,
        )
        decisions = self._risks.for_approved_targets(
            tuple(sorted({group.approved_target_id for group in groups}))
        )
        sleeves: dict[str, ApprovedSleeve] = {}
        for group in groups:
            decision = decisions.get(group.approved_target_id)
            approved = decision.approved_target if decision is not None else None
            if approved is None or approved.approved_target_id != group.approved_target_id:
                raise ValueError("ExecutionGroup 缺少冻结的 ApprovedPortfolioTarget")
            sleeve = next(
                (item for item in approved.sleeves if item.sleeve_id == group.sleeve_id),
                None,
            )
            if sleeve is None:
                raise ValueError("ExecutionGroup 缺少冻结的 ApprovedSleeve")
            existing = sleeves.get(sleeve.sleeve_id)
            if existing is not None and (
                existing.forecast_family != sleeve.forecast_family
                or existing.forecast_target != sleeve.forecast_target
            ):
                raise ValueError("相同 Sleeve identity 出现不同投资对象定义")
            sleeves[sleeve.sleeve_id] = sleeve
        previous = self._accounts.latest_account(
            portfolio_id=self._projector.portfolio_id,
            as_of=as_of,
        )
        perpetual_instruments = {
            leg.instrument.key: leg.instrument
            for group in groups
            for leg in (*group.target_legs, *group.compensation_legs)
            if leg.instrument.product != InstrumentProduct.SPOT
        }
        funding_settlements: list[FundingSettlement] = []
        if groups:
            start = min(item.started_at for item in groups)
            if start < as_of:
                for key in sorted(perpetual_instruments):
                    funding_settlements.extend(
                        self._funding.funding_settlements(
                            instrument=perpetual_instruments[key],
                            start=start,
                            end=as_of,
                            visible_at=as_of,
                        )
                    )
        ordered_settlements = tuple(
            sorted(
                funding_settlements,
                key=lambda item: (
                    item.funding_time,
                    item.rate_type.value,
                    item.settlement_id,
                ),
            )
        )
        return self._projector.project(
            cycle_id=cycle_id,
            as_of=as_of,
            groups=groups,
            observation_history_by_group=observation_history,
            funding_settlements=ordered_settlements,
            approved_sleeves=tuple(sleeves[key] for key in sorted(sleeves)),
            quotes=quotes,
            previous=previous,
            reconciled=reconciled,
        )
