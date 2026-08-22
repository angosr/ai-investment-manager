"""Point-in-time projection of Capital facts for the preregistered evaluator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from itertools import pairwise

from sqlalchemy import select
from sqlalchemy.engine import Engine

from investment_manager.execution.group.models import ExecutionGroup
from investment_manager.execution.planning.planner import TradePlan
from investment_manager.execution.tables import execution_groups, trade_plans
from investment_manager.execution.venue.observation import (
    ProductOrderObservation,
    SqlProductOrderObservationStore,
)
from investment_manager.execution.venue.product import ProductOrder
from investment_manager.forecast.models import BaseForecast, ForecastKind
from investment_manager.forecast.tables import forecasts
from investment_manager.governance.evaluation.capital import (
    CapitalLedgerProjection,
    CapitalShadowEvaluationSpec,
    capital_behavior_hash,
)
from investment_manager.kernel.identity import content_hash
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import floor_to_step
from investment_manager.market.models import InstrumentProduct, MarketQuote
from investment_manager.market.perpetual.models import PerpetualQuote
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.platform.time import database_utc
from investment_manager.portfolio.models import (
    CapitalCycleRecord,
    PortfolioPerformanceInterval,
    PortfolioTarget,
)
from investment_manager.portfolio.tables import (
    capital_cycle_records,
    portfolio_performance_intervals,
    portfolio_targets,
)
from investment_manager.risk.portfolio import (
    PortfolioHoldingRiskReview,
    PortfolioRiskDecision,
)
from investment_manager.risk.tables import (
    portfolio_holding_risk_reviews,
    portfolio_risk_decisions,
)
from investment_manager.scheduling.tables import analysis_trigger_batches
from investment_manager.settings import AppConfig


@dataclass(frozen=True, slots=True)
class _MarketPoint:
    at: datetime
    spot: MarketQuote
    perpetual: PerpetualQuote


@dataclass(frozen=True, slots=True)
class _AccountPoint:
    snapshot_id: str
    revision: int
    at: datetime
    equity: Decimal


class SqlCapitalLedgerProjector:
    """Read one frozen release cohort without mutating its business ledgers."""

    def __init__(self, engine: Engine, config: AppConfig) -> None:
        self._engine = engine
        self._config = config
        self._market = SqlMarketDataStore(engine)
        self._observations = SqlProductOrderObservationStore(engine)

    def project(
        self,
        *,
        spec: CapitalShadowEvaluationSpec,
        projected_at: datetime,
    ) -> CapitalLedgerProjection:
        projected = require_utc(projected_at)
        if projected < spec.observation_end:
            raise ValueError("Capital ledger 只能在完整观察窗口结束后投影")
        if (
            content_hash(self._config) != spec.release_configuration_hash
            or capital_behavior_hash(self._config) != spec.capital_behavior_hash
        ):
            raise ValueError("Capital ledger 配置与预登记 Release 行为不一致")
        records = self._cycle_records(spec)
        intervals = self._performance_intervals(spec)
        source_ids = {item.record_id for item in records}
        source_ids.update(item.interval_id for item in intervals)
        monthly_returns, starting, ending = self._monthly_returns(spec, intervals)
        attribution = self._attribution(intervals)
        decision_source_ids, dynamic_forecasts = self._validate_decision_chain(
            records,
        )
        source_ids.update(decision_source_ids)
        batch_ids, complete_months = self._decision_completeness(spec, records)
        source_ids.update(batch_ids)
        (
            groups,
            group_source_ids,
            maximum_unhedged,
            maximum_recovery,
        ) = self._execution_groups(records, projected_at=projected)
        source_ids.update(group_source_ids)
        margin_buffer, risk_source_ids = self._minimum_margin_buffer(spec, records)
        source_ids.update(risk_source_ids)
        counterfactual, market_source_ids = self._calendar_counterfactual(
            spec,
            records,
        )
        source_ids.update(market_source_ids)
        forecast_months = len(
            {(item.available_at.year, item.available_at.month) for item in dynamic_forecasts}
        )
        maximum_drawdown = self._maximum_drawdown(intervals, starting)
        unresolved = sum(not item.terminal for item in groups)
        late_entries = self._late_entry_count(records)
        duplicate_groups = len(groups) - len({(item.plan_id, item.sleeve_id) for item in groups})
        return CapitalLedgerProjection.create(
            plan_id=spec.plan_id,
            projected_at=projected,
            source_ids=tuple(source_ids),
            monthly_net_return_fractions=monthly_returns,
            forecast_available_months=forecast_months,
            decision_complete_months=complete_months,
            late_entry_count=late_entries,
            duplicate_execution_group_count=duplicate_groups,
            unresolved_execution_group_count=unresolved,
            maximum_unhedged_seconds=maximum_unhedged,
            maximum_group_recovery_seconds=maximum_recovery,
            starting_equity=starting,
            ending_equity=ending,
            price_pnl=attribution[0],
            funding_pnl=attribution[1],
            fee_cost=attribution[2],
            execution_slippage_cost=attribution[3],
            compensation_loss=attribution[4],
            net_pnl=ending - starting,
            maximum_drawdown_fraction=maximum_drawdown,
            minimum_margin_buffer_fraction=margin_buffer,
            source_counterfactual_annualized_return_fraction=counterfactual,
        )

    def _cycle_records(
        self,
        spec: CapitalShadowEvaluationSpec,
    ) -> tuple[CapitalCycleRecord, ...]:
        with self._engine.connect() as connection:
            payloads = connection.execute(
                select(capital_cycle_records.c.payload)
                .where(
                    capital_cycle_records.c.portfolio_id == spec.portfolio_id,
                    capital_cycle_records.c.pipeline_id == self._config.pipeline.version,
                    capital_cycle_records.c.evaluated_at >= spec.observation_start,
                    capital_cycle_records.c.evaluated_at < spec.observation_end,
                )
                .order_by(
                    capital_cycle_records.c.evaluated_at,
                    capital_cycle_records.c.record_id,
                )
            ).scalars()
            return tuple(CapitalCycleRecord.model_validate(item) for item in payloads)

    def _performance_intervals(
        self,
        spec: CapitalShadowEvaluationSpec,
    ) -> tuple[PortfolioPerformanceInterval, ...]:
        with self._engine.connect() as connection:
            payloads = connection.execute(
                select(portfolio_performance_intervals.c.payload)
                .where(
                    portfolio_performance_intervals.c.portfolio_id == spec.portfolio_id,
                    portfolio_performance_intervals.c.end_as_of >= spec.observation_start,
                    portfolio_performance_intervals.c.start_as_of <= spec.observation_end,
                )
                .order_by(
                    portfolio_performance_intervals.c.end_revision,
                    portfolio_performance_intervals.c.interval_id,
                )
            ).scalars()
            values = tuple(PortfolioPerformanceInterval.model_validate(item) for item in payloads)
        if not values:
            raise ValueError("Capital ledger 观察窗口没有费用后绩效区间")
        points = _account_points(values)
        start = _boundary_account_point(points, spec.observation_start)
        end = _boundary_account_point(points, spec.observation_end)
        selected = tuple(
            item
            for item in values
            if item.start_revision >= start.revision and item.end_revision <= end.revision
        )
        if (
            not selected
            or selected[0].start_snapshot_id != start.snapshot_id
            or selected[-1].end_snapshot_id != end.snapshot_id
            or any(
                current.end_snapshot_id != following.start_snapshot_id
                for current, following in pairwise(selected)
            )
        ):
            raise ValueError("Capital ledger 绩效区间未形成精确窗口连续链")
        return selected

    @staticmethod
    def _monthly_returns(
        spec: CapitalShadowEvaluationSpec,
        intervals: tuple[PortfolioPerformanceInterval, ...],
    ) -> tuple[tuple[Decimal, ...], Decimal, Decimal]:
        points = _account_points(intervals)
        windows = _calendar_month_windows(spec.observation_start, spec.observation_end)
        boundaries = {value for window in windows for value in window}
        boundary_points = {
            boundary: _boundary_account_point(points, boundary) for boundary in boundaries
        }
        monthly = tuple(
            boundary_points[end].equity / boundary_points[start].equity - Decimal("1")
            for start, end in windows
        )
        starting = boundary_points[spec.observation_start].equity
        ending = boundary_points[spec.observation_end].equity
        compounded = starting
        for value in monthly:
            compounded *= Decimal("1") + value
        if compounded != ending:
            raise ValueError("Capital ledger 月度收益无法与连续权益核对")
        if starting != spec.starting_equity:
            raise ValueError("Capital ledger 窗口起始权益与预登记合同不一致")
        return monthly, starting, ending

    @staticmethod
    def _attribution(
        intervals: tuple[PortfolioPerformanceInterval, ...],
    ) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
        if any(item.attribution is None for item in intervals):
            raise ValueError("Capital ledger 绩效区间缺少费用归因")
        attributions = tuple(item.attribution for item in intervals)
        assert all(item is not None for item in attributions)
        return (
            sum((item.price_pnl for item in attributions if item), Decimal("0")),
            sum((item.funding_pnl for item in attributions if item), Decimal("0")),
            sum((item.fee_cost for item in attributions if item), Decimal("0")),
            sum(
                (item.execution_slippage_cost for item in attributions if item),
                Decimal("0"),
            ),
            sum(
                (item.compensation_loss for item in attributions if item),
                Decimal("0"),
            ),
        )

    def _validate_decision_chain(
        self,
        records: tuple[CapitalCycleRecord, ...],
    ) -> tuple[set[str], tuple[BaseForecast, ...]]:
        target_ids = {item.target_id for item in records if item.target_id}
        forecast_ids = {value for item in records for value in item.forecast_ids}
        source_ids: set[str] = set()
        with self._engine.connect() as connection:
            target_rows = (
                connection.execute(
                    select(portfolio_targets.c.target_id, portfolio_targets.c.payload).where(
                        portfolio_targets.c.target_id.in_(target_ids)
                    )
                ).all()
                if target_ids
                else ()
            )
            loaded_targets = {
                row.target_id: PortfolioTarget.model_validate(row.payload) for row in target_rows
            }
            if set(loaded_targets) != target_ids:
                raise ValueError("Capital ledger CycleRecord 缺少 PortfolioTarget")
            if any(
                item.policy_version != self._config.capital.decision.version
                for item in loaded_targets.values()
            ):
                raise ValueError("Capital ledger PortfolioTarget policy 身份不一致")
            source_ids.update(loaded_targets)
            if any(
                loaded_targets[item.target_id].cycle_id != item.decision_cycle_id
                for item in records
                if item.target_id is not None
            ):
                raise ValueError("Capital ledger CycleRecord 与 PortfolioTarget 周期不一致")
            risk_rows = (
                connection.execute(
                    select(
                        portfolio_risk_decisions.c.decision_id,
                        portfolio_risk_decisions.c.target_id,
                        portfolio_risk_decisions.c.payload,
                    ).where(portfolio_risk_decisions.c.target_id.in_(target_ids))
                ).all()
                if target_ids
                else ()
            )
            loaded_risks = {
                row.target_id: PortfolioRiskDecision.model_validate(row.payload)
                for row in risk_rows
            }
            if set(loaded_risks) != target_ids:
                raise ValueError("Capital ledger PortfolioTarget 缺少 RiskDecision")
            if any(
                item.policy_version != self._config.capital.risk.version
                for item in loaded_risks.values()
            ):
                raise ValueError("Capital ledger RiskDecision policy 身份不一致")
            source_ids.update(item.decision_id for item in loaded_risks.values())
            forecast_rows = (
                connection.execute(
                    select(forecasts.c.forecast_id, forecasts.c.kind, forecasts.c.payload).where(
                        forecasts.c.forecast_id.in_(forecast_ids)
                    )
                ).all()
                if forecast_ids
                else ()
            )
            if {row.forecast_id for row in forecast_rows} != forecast_ids:
                raise ValueError("Capital ledger CycleRecord 缺少 Forecast")
            loaded_forecasts = []
            dynamic = self._config.dynamic_carry_forecast
            for row in forecast_rows:
                if row.kind != ForecastKind.BASE.value:
                    raise ValueError("Dynamic cohort 不得混入 CalibratedForecast")
                forecast = BaseForecast.model_validate(row.payload)
                if (
                    forecast.producer_id != dynamic.producer_id
                    or forecast.producer_version != dynamic.version
                    or forecast.forecast_family != dynamic.forecast_family
                ):
                    raise ValueError("Dynamic cohort Forecast producer 身份不一致")
                loaded_forecasts.append(forecast)
                source_ids.add(row.forecast_id)
        return source_ids, tuple(loaded_forecasts)

    def _decision_completeness(
        self,
        spec: CapitalShadowEvaluationSpec,
        records: tuple[CapitalCycleRecord, ...],
    ) -> tuple[set[str], int]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(
                    analysis_trigger_batches.c.batch_id,
                    analysis_trigger_batches.c.batched_at,
                ).where(
                    analysis_trigger_batches.c.pipeline_id == self._config.pipeline.version,
                    analysis_trigger_batches.c.batched_at >= spec.observation_start,
                    analysis_trigger_batches.c.batched_at < spec.observation_end,
                )
            ).all()
        recorded = {item.trigger_batch_id for item in records if item.trigger_batch_id is not None}
        known = {row.batch_id for row in rows}
        if not recorded.issubset(known):
            raise ValueError("Capital ledger CycleRecord 引用了窗口外 TriggerBatch")
        complete = 0
        for start, end in _calendar_month_windows(
            spec.observation_start,
            spec.observation_end,
        ):
            month = {row.batch_id for row in rows if start <= database_utc(row.batched_at) < end}
            if month and month.issubset(recorded):
                complete += 1
        return known, complete

    def _execution_groups(
        self,
        records: tuple[CapitalCycleRecord, ...],
        *,
        projected_at: datetime,
    ) -> tuple[tuple[ExecutionGroup, ...], set[str], int, int]:
        cycles = {item.decision_cycle_id for item in records}
        target_ids = {item.target_id for item in records if item.target_id is not None}
        with self._engine.connect() as connection:
            risk_rows = (
                connection.execute(
                    select(portfolio_risk_decisions.c.payload).where(
                        portfolio_risk_decisions.c.target_id.in_(target_ids)
                    )
                ).scalars()
                if target_ids
                else ()
            )
            approved_cycles = {
                risk.approved_target.cycle_id
                for payload in risk_rows
                if (risk := PortfolioRiskDecision.model_validate(payload)).approved_target
                is not None
            }
            plan_rows = (
                connection.execute(
                    select(trade_plans.c.plan_id, trade_plans.c.payload).where(
                        trade_plans.c.cycle_id.in_(cycles)
                    )
                ).all()
                if cycles
                else ()
            )
            plans = tuple(TradePlan.model_validate(row.payload) for row in plan_rows)
            if any(
                item.planner_policy_version != self._config.capital.planner.version
                for item in plans
            ):
                raise ValueError("Capital ledger TradePlan policy 身份不一致")
            if {item.cycle_id for item in plans} != approved_cycles:
                raise ValueError("Capital ledger 已批准 RiskDecision 缺少唯一 TradePlan")
            plan_ids = {item.plan_id for item in plans}
            planned_group_ids = {group.group_id for plan in plans for group in plan.groups}
            group_rows = (
                connection.execute(
                    select(execution_groups.c.group_id, execution_groups.c.payload).where(
                        execution_groups.c.plan_id.in_(plan_ids)
                    )
                ).all()
                if plan_ids
                else ()
            )
            groups = tuple(ExecutionGroup.model_validate(row.payload) for row in group_rows)
            group_ids = {item.group_id for item in groups}
            if group_ids != planned_group_ids:
                raise ValueError("Capital ledger TradePlan 与 ExecutionGroup 集合不一致")
        histories = self._observations.history_for_groups(
            tuple(sorted(group_ids)),
            as_of=projected_at,
        )
        observation_ids = {
            item.observation_id for observations in histories.values() for item in observations
        }
        maximum_unhedged = max(
            (
                _maximum_group_unhedged_seconds(
                    group,
                    histories[group.group_id],
                    projected_at=projected_at,
                )
                for group in groups
            ),
            default=0,
        )
        maximum_recovery = max(
            (
                int(
                    (
                        (group.updated_at if group.terminal else projected_at) - group.started_at
                    ).total_seconds()
                )
                for group in groups
            ),
            default=0,
        )
        source_ids = {*plan_ids, *group_ids, *observation_ids}
        return groups, source_ids, maximum_unhedged, maximum_recovery

    def _minimum_margin_buffer(
        self,
        spec: CapitalShadowEvaluationSpec,
        records: tuple[CapitalCycleRecord, ...],
    ) -> tuple[Decimal, set[str]]:
        target_ids = {item.target_id for item in records if item.target_id is not None}
        with self._engine.connect() as connection:
            risk_rows = (
                connection.execute(
                    select(
                        portfolio_risk_decisions.c.decision_id,
                        portfolio_risk_decisions.c.payload,
                    ).where(portfolio_risk_decisions.c.target_id.in_(target_ids))
                ).all()
                if target_ids
                else ()
            )
            review_rows = connection.execute(
                select(
                    portfolio_holding_risk_reviews.c.review_id,
                    portfolio_holding_risk_reviews.c.payload,
                ).where(
                    portfolio_holding_risk_reviews.c.portfolio_id == spec.portfolio_id,
                    portfolio_holding_risk_reviews.c.reviewed_at >= spec.observation_start,
                    portfolio_holding_risk_reviews.c.reviewed_at < spec.observation_end,
                )
            ).all()
        decisions = tuple(
            PortfolioRiskDecision.model_validate(row.payload) for row in risk_rows
        )
        reviews = tuple(
            PortfolioHoldingRiskReview.model_validate(row.payload)
            for row in review_rows
        )
        if any(
            review.policy_version != self._config.capital.risk.version
            for review in reviews
        ):
            raise ValueError("Capital ledger HoldingRiskReview policy 身份不一致")
        rules = [
            rule
            for decision in decisions
            for rule in decision.rule_results
        ] + [
            rule
            for review in reviews
            for rule in review.rule_results
        ]
        buffers = []
        maximum_fraction = self._config.capital.risk.maximum_margin_fraction
        for rule in rules:
            if (
                rule.reason_code
                not in {
                    "MARGIN_WITHIN_LIMIT",
                    "MARGIN_LIMIT_EXCEEDED",
                }
                or rule.observed is None
                or rule.limit is None
            ):
                continue
            equity = Decimal(rule.limit) / maximum_fraction
            buffers.append(Decimal("1") - Decimal(rule.observed) / equity)
        return (
            min(buffers, default=Decimal("1")),
            {
                *(row.decision_id for row in risk_rows),
                *(row.review_id for row in review_rows),
            },
        )

    def _late_entry_count(
        self,
        records: tuple[CapitalCycleRecord, ...],
    ) -> int:
        ids = {value for item in records if item.target_id for value in item.forecast_ids}
        if not ids:
            return 0
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(forecasts.c.forecast_id, forecasts.c.valid_until).where(
                    forecasts.c.forecast_id.in_(ids)
                )
            ).all()
        valid_until = {row.forecast_id: database_utc(row.valid_until) for row in rows}
        return sum(
            item.evaluated_at >= valid_until[forecast_id]
            for item in records
            if item.target_id
            for forecast_id in item.forecast_ids
        )

    @staticmethod
    def _maximum_drawdown(
        intervals: tuple[PortfolioPerformanceInterval, ...],
        starting: Decimal,
    ) -> Decimal:
        peak = starting
        drawdown = Decimal("0")
        for item in intervals:
            peak = max(peak, item.end_equity)
            drawdown = max(drawdown, Decimal("1") - item.end_equity / peak)
        return drawdown

    def _calendar_counterfactual(
        self,
        spec: CapitalShadowEvaluationSpec,
        records: tuple[CapitalCycleRecord, ...],
    ) -> tuple[Decimal, set[str]]:
        evaluation_times = tuple(
            sorted(
                {
                    item.evaluated_at
                    for item in records
                    if spec.observation_start <= item.evaluated_at < spec.observation_end
                }
                | {spec.observation_end}
            )
        )
        if len(evaluation_times) < 2:
            raise ValueError("Capital counterfactual 缺少共享 Trigger 时点")
        points = tuple(self._market_point(value) for value in evaluation_times)
        equity = spec.starting_equity
        quantity = Decimal("0")
        previous: _MarketPoint | None = None
        source_ids: set[str] = {
            spec.source_evaluation_id,
            spec.source_result_hash,
        }
        rebalanced: set[tuple[int, int]] = set()
        settlement_ids: set[str] = set()
        spot_spec = next(
            item
            for item in self._config.capital.execution_specs
            if item.instrument.product == InstrumentProduct.SPOT
        )
        perpetual_spec = next(
            item
            for item in self._config.capital.execution_specs
            if item.instrument.product == InstrumentProduct.USD_M_PERPETUAL
        )
        quantity_step = max(spot_spec.quantity_step, perpetual_spec.quantity_step)
        if spot_spec.instrument.contract_multiplier != Decimal(
            "1"
        ) or perpetual_spec.instrument.contract_multiplier != Decimal("1"):
            raise ValueError("Capital counterfactual 仅支持冻结的同基准数量合约")
        evidence = self._config.carry_forecast.evidence
        if evidence is None:
            raise ValueError("Capital counterfactual 缺少预登记 source policy evidence")
        leg_fraction = evidence.evaluated_gross_exposure_fraction / Decimal("2")
        for point in points:
            source_ids.update((point.spot.quote_id, point.perpetual.quote_id))
            if previous is not None and quantity:
                spot_change = _mid(point.spot.bid, point.spot.ask) - _mid(
                    previous.spot.bid,
                    previous.spot.ask,
                )
                perpetual_change = _mid(
                    point.perpetual.bid,
                    point.perpetual.ask,
                ) - _mid(previous.perpetual.bid, previous.perpetual.ask)
                equity += quantity * (spot_change - perpetual_change)
                for settlement in self._market.funding_settlements(
                    instrument=perpetual_spec.instrument,
                    start=previous.at,
                    end=point.at,
                    visible_at=point.at,
                ):
                    if settlement.settlement_id in settlement_ids:
                        continue
                    settlement_ids.add(settlement.settlement_id)
                    source_ids.add(settlement.settlement_id)
                    equity += quantity * settlement.mark_price * settlement.funding_rate
            month = (point.at.year, point.at.month)
            month_start = point.at.replace(
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
            minutes = (point.at - month_start).total_seconds() / 60
            if (
                point.at < spec.observation_end
                and month not in rebalanced
                and minutes < self._config.carry_forecast.maximum_monthly_entry_delay_minutes
            ):
                target = floor_to_step(
                    equity * leg_fraction / max(point.spot.ask, point.perpetual.bid),
                    quantity_step,
                )
                delta = target - quantity
                equity -= _execution_cost(
                    delta=delta,
                    point=point,
                    spot_fee_bps=spot_spec.fee_bps,
                    perpetual_fee_bps=perpetual_spec.fee_bps,
                )
                quantity = target
                rebalanced.add(month)
            previous = point
        assert previous is not None
        equity -= _execution_cost(
            delta=-quantity,
            point=previous,
            spot_fee_bps=spot_spec.fee_bps,
            perpetual_fee_bps=perpetual_spec.fee_bps,
        )
        total_return = (equity - spec.starting_equity) / spec.starting_equity
        months = Decimal(
            len(
                _calendar_month_windows(
                    spec.observation_start,
                    spec.observation_end,
                )
            )
        )
        annualized = (Decimal("1") + total_return) ** (Decimal("12") / months) - Decimal("1")
        return annualized, source_ids

    def _market_point(self, at: datetime) -> _MarketPoint:
        spot_spec = next(
            item
            for item in self._config.capital.execution_specs
            if item.instrument.product == InstrumentProduct.SPOT
        )
        perpetual_spec = next(
            item
            for item in self._config.capital.execution_specs
            if item.instrument.product == InstrumentProduct.USD_M_PERPETUAL
        )
        spot = self._market.latest_spot_quote(
            instrument=spot_spec.instrument,
            evaluation_at=at,
            visible_at=at,
        )
        perpetual = self._market.latest_perpetual_quote(
            instrument=perpetual_spec.instrument,
            evaluation_at=at,
            visible_at=at,
        )
        if spot is None or perpetual is None:
            raise ValueError("Capital counterfactual 缺少点时可成交报价")
        maximum_age = self._config.capital.risk.maximum_quote_age_seconds
        if (at - spot.observed_at).total_seconds() > maximum_age or (
            at - perpetual.exchange_time
        ).total_seconds() > maximum_age:
            raise ValueError("Capital counterfactual 点时可成交报价过期")
        return _MarketPoint(at=at, spot=spot, perpetual=perpetual)


def _execution_cost(
    *,
    delta: Decimal,
    point: _MarketPoint,
    spot_fee_bps: Decimal,
    perpetual_fee_bps: Decimal,
) -> Decimal:
    quantity = abs(delta)
    if not quantity:
        return Decimal("0")
    spot_price = point.spot.ask if delta > 0 else point.spot.bid
    perpetual_price = point.perpetual.bid if delta > 0 else point.perpetual.ask
    spread = quantity * (
        abs(spot_price - _mid(point.spot.bid, point.spot.ask))
        + abs(perpetual_price - _mid(point.perpetual.bid, point.perpetual.ask))
    )
    fee = (
        quantity
        * (spot_price * spot_fee_bps + perpetual_price * perpetual_fee_bps)
        / Decimal("10000")
    )
    return spread + fee


def _mid(bid: Decimal, ask: Decimal) -> Decimal:
    return (bid + ask) / Decimal("2")


def _account_points(
    intervals: tuple[PortfolioPerformanceInterval, ...],
) -> tuple[_AccountPoint, ...]:
    by_revision: dict[int, _AccountPoint] = {}
    for interval in intervals:
        for point in (
            _AccountPoint(
                snapshot_id=interval.start_snapshot_id,
                revision=interval.start_revision,
                at=interval.start_as_of,
                equity=interval.start_equity,
            ),
            _AccountPoint(
                snapshot_id=interval.end_snapshot_id,
                revision=interval.end_revision,
                at=interval.end_as_of,
                equity=interval.end_equity,
            ),
        ):
            existing = by_revision.get(point.revision)
            if existing is not None and existing != point:
                raise ValueError("Capital ledger 同一账户 revision 出现不同事实")
            by_revision[point.revision] = point
    return tuple(by_revision[key] for key in sorted(by_revision))


def _boundary_account_point(
    points: tuple[_AccountPoint, ...],
    boundary: datetime,
) -> _AccountPoint:
    candidates = tuple(item for item in points if item.at == boundary)
    if not candidates:
        raise ValueError("Capital ledger 自然月边界缺少精确账户估值")
    return min(candidates, key=lambda item: item.revision)


def _maximum_group_unhedged_seconds(
    group: ExecutionGroup,
    observations: tuple[ProductOrderObservation, ...],
    *,
    projected_at: datetime,
) -> int:
    """Conservatively measure visible one-leg exposure from immutable order facts."""

    known_leg_ids = {
        item.execution_leg_id for item in (*group.target_legs, *group.compensation_legs)
    }
    latest_orders: dict[str, ProductOrder] = {}
    unhedged_since: datetime | None = None
    maximum = 0
    for available_at, batch in _observation_batches(observations):
        for observation in batch:
            order = observation.order
            if order.execution_leg_id not in known_leg_ids:
                raise ValueError("Capital ledger 订单观察不属于 ExecutionGroup 当前事实")
            latest_orders[order.execution_leg_id] = order
        is_unhedged = _orders_are_unhedged(group, latest_orders)
        if is_unhedged and unhedged_since is None:
            unhedged_since = available_at
        elif not is_unhedged and unhedged_since is not None:
            maximum = max(
                maximum,
                int((available_at - unhedged_since).total_seconds()),
            )
            unhedged_since = None
    if unhedged_since is not None:
        if group.terminal:
            raise ValueError("Capital ledger 终态 ExecutionGroup 的订单观察仍未对冲")
        maximum = max(
            maximum,
            int((projected_at - unhedged_since).total_seconds()),
        )
    return maximum


def _observation_batches(
    observations: tuple[ProductOrderObservation, ...],
) -> tuple[tuple[datetime, tuple[ProductOrderObservation, ...]], ...]:
    batches: list[tuple[datetime, tuple[ProductOrderObservation, ...]]] = []
    for observation in observations:
        if batches and batches[-1][0] == observation.available_at:
            batches[-1] = (
                observation.available_at,
                (*batches[-1][1], observation),
            )
        else:
            batches.append((observation.available_at, (observation,)))
    return tuple(batches)


def _orders_are_unhedged(
    group: ExecutionGroup,
    latest_orders: dict[str, ProductOrder],
) -> bool:
    if len(group.target_legs) < 2:
        return False
    compensation_by_planned: dict[str, Decimal] = {}
    for leg in group.compensation_legs:
        order = latest_orders.get(leg.execution_leg_id)
        if order is not None:
            compensation_by_planned[leg.planned_leg_id] = (
                compensation_by_planned.get(leg.planned_leg_id, Decimal("0"))
                + order.filled_quantity
            )
    progress = []
    for leg in group.target_legs:
        order = latest_orders.get(leg.execution_leg_id)
        target_filled = order.filled_quantity if order is not None else Decimal("0")
        residual = target_filled - compensation_by_planned.get(
            leg.planned_leg_id,
            Decimal("0"),
        )
        progress.append(residual / leg.requested_quantity)
    return max(progress) != min(progress)


def _calendar_month_windows(
    start: datetime,
    end: datetime,
) -> tuple[tuple[datetime, datetime], ...]:
    windows = []
    cursor = start
    while cursor < end:
        following = (
            cursor.replace(year=cursor.year + 1, month=1)
            if cursor.month == 12
            else cursor.replace(month=cursor.month + 1)
        )
        windows.append((cursor, following))
        cursor = following
    return tuple(windows)
