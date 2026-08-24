"""Read-only projection of the active product-capital ledger for the dashboard."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import and_, func, select
from sqlalchemy.engine import Engine

from investment_manager.entrypoints.dashboard.pagination import PageCursor, older_than
from investment_manager.execution.group.models import ExecutionGroup
from investment_manager.execution.planning.planner import TradePlan
from investment_manager.execution.tables import (
    execution_groups,
    mock_product_orders,
    trade_plans,
)
from investment_manager.forecast.context.evaluation import (
    ForecastEvidence,
    ForecastScoringCase,
    evaluate_forecast_evidence,
)
from investment_manager.forecast.contracts import ForecastContract
from investment_manager.forecast.results import (
    BaseForecast,
    CalibratedForecast,
    ForecastOutcome,
    ForecastOutcomeStatus,
    ForecastResultKind,
)
from investment_manager.forecast.tables import (
    forecast_contracts,
    forecast_decision_slots,
    forecast_no_estimates,
    forecast_outcomes,
    forecast_slot_obligations,
)
from investment_manager.forecast.tables import forecasts as forecast_records
from investment_manager.kernel.time import require_utc
from investment_manager.portfolio.models import (
    CapitalCycleOutcome,
    CapitalCycleRecord,
    PortfolioAccountSnapshot,
    PortfolioPerformanceInterval,
    PortfolioTarget,
)
from investment_manager.portfolio.tables import (
    capital_cycle_records,
    portfolio_account_snapshots,
    portfolio_performance_intervals,
    portfolio_targets,
)
from investment_manager.risk.portfolio import PortfolioRiskDecision
from investment_manager.risk.tables import portfolio_risk_decisions
from investment_manager.settings import AppConfig


@dataclass(frozen=True, slots=True)
class CapitalOverview:
    enabled: bool
    account: PortfolioAccountSnapshot | None = None
    cycle_record: CapitalCycleRecord | None = None
    target: PortfolioTarget | None = None
    risk: PortfolioRiskDecision | None = None
    active_groups: tuple[ExecutionGroup, ...] = ()
    total_order_count: int = 0
    performance_interval_count: int = 0
    cumulative_net_pnl: Decimal = Decimal("0")
    latest_performance: PortfolioPerformanceInterval | None = None
    forecast_evidence: ForecastEvidence | None = None


@dataclass(frozen=True, slots=True)
class CapitalCandidateEconomics:
    forecast_id: str
    producer_id: str
    outcome_family_id: str
    information_cutoff_at: datetime
    available_at: datetime
    valid_until: datetime
    world_model_id: str | None
    outcome_probabilities: tuple[tuple[str, Decimal], ...]
    mechanism_contributions: tuple[tuple[str, str, str], ...]
    evidence_refs: tuple[str, ...]
    analysis_input: dict | None
    gross_bps: Decimal
    estimated_cost_bps: Decimal
    net_bps: Decimal
    decision_threshold_bps: Decimal
    current_gross_notional: Decimal
    evaluation_gross_notional: Decimal
    desired_gross_notional: Decimal
    eligible: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CapitalActivity:
    activity_id: str
    at: datetime
    symbol: str
    trigger_types: tuple[str, ...]
    outcome: str
    summary: str
    reason_codes: tuple[str, ...] = ()
    risk_outcome: str | None = None
    order_count: int = 0
    candidate_economics_recorded: bool = False
    candidate_economics: tuple[CapitalCandidateEconomics, ...] = ()


class CapitalDashboardReader:
    """Load a compact current-state view without inventing a second ledger."""

    def __init__(self, engine: Engine, config: AppConfig) -> None:
        self._engine = engine
        self._config = config

    def overview(self, *, now: datetime) -> CapitalOverview:
        now = require_utc(now)
        if not self._config.capital.enabled:
            return CapitalOverview(enabled=False)
        with self._engine.connect() as connection:
            account = self._latest_payload(
                connection,
                portfolio_account_snapshots.c.payload,
                portfolio_account_snapshots.c.as_of,
                portfolio_account_snapshots.c.snapshot_id,
                PortfolioAccountSnapshot,
                secondary_order=portfolio_account_snapshots.c.revision,
            )
            cycle_record = self._latest_payload(
                connection,
                capital_cycle_records.c.payload,
                capital_cycle_records.c.evaluated_at,
                capital_cycle_records.c.record_id,
                CapitalCycleRecord,
                where_clause=(capital_cycle_records.c.pipeline_id == self._config.pipeline.version),
            )
            target = self._latest_payload(
                connection,
                portfolio_targets.c.payload,
                portfolio_targets.c.as_of,
                portfolio_targets.c.target_id,
                PortfolioTarget,
            )
            if account is not None and target is not None and account.as_of > target.as_of:
                target = None
            risk = None
            if target is not None:
                risk = self._payload_for(
                    connection,
                    select(portfolio_risk_decisions.c.payload).where(
                        portfolio_risk_decisions.c.target_id == target.target_id
                    ),
                    PortfolioRiskDecision,
                )
            active = tuple(
                ExecutionGroup.model_validate(payload)
                for payload in connection.execute(
                    select(execution_groups.c.payload)
                    .where(execution_groups.c.terminal.is_(False))
                    .order_by(
                        execution_groups.c.updated_at,
                        execution_groups.c.group_id,
                    )
                ).scalars()
            )
            order_count = int(
                connection.scalar(select(func.count()).select_from(mock_product_orders)) or 0
            )
            performance_count = int(
                connection.scalar(
                    select(func.count())
                    .select_from(portfolio_performance_intervals)
                    .where(
                        portfolio_performance_intervals.c.portfolio_id
                        == self._config.capital.decision.portfolio_id
                    )
                )
                or 0
            )
            first_account_payload = connection.execute(
                select(portfolio_account_snapshots.c.payload)
                .where(
                    portfolio_account_snapshots.c.portfolio_id
                    == self._config.capital.decision.portfolio_id
                )
                .order_by(
                    portfolio_account_snapshots.c.as_of,
                    portfolio_account_snapshots.c.revision,
                    portfolio_account_snapshots.c.snapshot_id,
                )
                .limit(1)
            ).scalar_one_or_none()
            first_account = (
                None
                if first_account_payload is None
                else PortfolioAccountSnapshot.model_validate(first_account_payload)
            )
            cumulative_net_pnl = (
                Decimal("0")
                if account is None or first_account is None
                else account.equity - first_account.equity
            )
            latest_performance = self._latest_payload(
                connection,
                portfolio_performance_intervals.c.payload,
                portfolio_performance_intervals.c.end_as_of,
                portfolio_performance_intervals.c.interval_id,
                PortfolioPerformanceInterval,
                secondary_order=portfolio_performance_intervals.c.end_revision,
                where_clause=(
                    portfolio_performance_intervals.c.portfolio_id
                    == self._config.capital.decision.portfolio_id
                ),
            )
            forecast_evidence = self._forecast_evidence(connection, now=now)
        return CapitalOverview(
            enabled=True,
            account=account,
            cycle_record=cycle_record,
            target=target,
            risk=risk,
            active_groups=active,
            total_order_count=order_count,
            performance_interval_count=performance_count,
            cumulative_net_pnl=cumulative_net_pnl,
            latest_performance=latest_performance,
            forecast_evidence=forecast_evidence,
        )

    def _forecast_evidence(self, connection, *, now: datetime) -> ForecastEvidence | None:
        policy = self._config.capital.context_forecast
        if policy is None or not policy.enabled:
            return None
        contract_row = connection.execute(
            select(forecast_contracts.c.contract_id, forecast_contracts.c.payload).where(
                forecast_contracts.c.outcome_family_id == policy.outcome_family_id,
                forecast_contracts.c.contract_version == policy.contract_version,
            )
        ).one_or_none()
        if contract_row is None:
            return None
        contract = ForecastContract.model_validate(contract_row.payload)
        due_slot_count = int(
            connection.scalar(
                select(func.count())
                .select_from(
                    forecast_slot_obligations.join(
                        forecast_decision_slots,
                        forecast_decision_slots.c.slot_id
                        == forecast_slot_obligations.c.slot_id,
                    )
                )
                .where(
                    forecast_decision_slots.c.contract_id == contract.contract_id,
                    forecast_slot_obligations.c.producer_id == policy.producer_id,
                    forecast_slot_obligations.c.producer_behavior_id
                    == policy.producer_behavior_id,
                    forecast_decision_slots.c.completion_deadline_at <= now,
                )
            )
            or 0
        )
        forecast_count = int(
            connection.scalar(
                select(func.count())
                .select_from(forecast_records)
                .join(
                    forecast_decision_slots,
                    forecast_decision_slots.c.slot_id == forecast_records.c.decision_slot_id,
                )
                .where(
                    forecast_records.c.contract_id == contract.contract_id,
                    forecast_records.c.kind == ForecastResultKind.BASE.value,
                    forecast_records.c.producer_id == policy.producer_id,
                    forecast_records.c.producer_behavior_id == policy.producer_behavior_id,
                    forecast_decision_slots.c.completion_deadline_at <= now,
                )
            )
            or 0
        )
        no_estimate_count = int(
            connection.scalar(
                select(func.count())
                .select_from(forecast_no_estimates)
                .join(
                    forecast_decision_slots,
                    forecast_decision_slots.c.slot_id == forecast_no_estimates.c.slot_id,
                )
                .where(
                    forecast_no_estimates.c.contract_id == contract.contract_id,
                    forecast_no_estimates.c.producer_id == policy.producer_id,
                    forecast_no_estimates.c.producer_behavior_id == policy.producer_behavior_id,
                    forecast_decision_slots.c.completion_deadline_at <= now,
                )
            )
            or 0
        )
        rows = connection.execute(
            select(forecast_records.c.payload, forecast_outcomes.c.payload)
            .select_from(
                forecast_records.join(
                    forecast_outcomes,
                    and_(
                        forecast_outcomes.c.decision_slot_id
                        == forecast_records.c.decision_slot_id,
                        forecast_outcomes.c.evaluation_version
                        == self._config.outcome_evaluation.target_forecast_version,
                    ),
                )
            )
            .where(
                forecast_records.c.contract_id == contract.contract_id,
                forecast_records.c.kind == ForecastResultKind.BASE.value,
                forecast_records.c.producer_id == policy.producer_id,
                forecast_records.c.producer_behavior_id == policy.producer_behavior_id,
                forecast_outcomes.c.status == ForecastOutcomeStatus.SETTLED.value,
            )
            .order_by(forecast_records.c.available_at, forecast_records.c.forecast_id)
        ).all()
        cases = []
        benchmark = tuple(
            (item.bucket_id, item.probability) for item in contract.forecast_benchmark
        )
        for row in rows:
            forecast = BaseForecast.model_validate(row[0])
            outcome = ForecastOutcome.model_validate(row[1])
            assert outcome.gross_target_return_bps is not None
            assert outcome.realized_bucket_id is not None
            cases.append(
                ForecastScoringCase(
                    forecast_id=forecast.forecast_id,
                    information_cutoff_at=forecast.information_cutoff_at,
                    evaluation_at=outcome.evaluation_at,
                    probabilities=tuple(
                        (item.bucket_id, item.probability)
                        for item in forecast.outcome_probabilities
                    ),
                    benchmark_probabilities=benchmark,
                    realized_bucket_id=outcome.realized_bucket_id,
                    expected_gross_bps=forecast.expected_gross_bps,
                    realized_gross_bps=outcome.gross_target_return_bps,
                )
            )
        return evaluate_forecast_evidence(
            tuple(cases),
            due_slot_count=due_slot_count,
            forecast_count=forecast_count,
            no_estimate_count=no_estimate_count,
            required_non_overlapping_samples=(
                self._config.calibration.minimum_non_overlapping_samples
            ),
            permission_evidence_eligible=contract.permission_evidence_eligible,
        )

    def activity(
        self,
        *,
        cursor: PageCursor | None = None,
        limit: int = 30,
    ) -> tuple[CapitalActivity, ...]:
        """Project durable capital decisions, excluding retired no-op receipts."""

        if limit < 1 or limit > 101:
            raise ValueError("Capital activity internal limit 必须在 1..101")
        query = select(
            capital_cycle_records.c.evaluated_at,
            capital_cycle_records.c.payload,
        ).where(
            capital_cycle_records.c.outcome.not_in(
                (
                    CapitalCycleOutcome.CASH.value,
                    CapitalCycleOutcome.NO_OPPORTUNITY.value,
                    CapitalCycleOutcome.OPPORTUNITY_ALREADY_DECIDED.value,
                )
            )
        )
        if cursor is not None:
            query = query.where(
                older_than(
                    capital_cycle_records.c.evaluated_at,
                    capital_cycle_records.c.record_id,
                    cursor,
                )
            )
        with self._engine.connect() as connection:
            rows = connection.execute(
                query.order_by(
                    capital_cycle_records.c.evaluated_at.desc(),
                    capital_cycle_records.c.record_id.desc(),
                ).limit(limit)
            ).all()
            if not rows:
                return ()
            records = tuple(CapitalCycleRecord.model_validate(item.payload) for item in rows)
            forecast_ids = tuple(
                sorted({forecast_id for item in records for forecast_id in item.forecast_ids})
            )
            loaded_forecasts = (
                {
                    item.forecast_id: (
                        BaseForecast.model_validate(item.payload)
                        if ForecastResultKind(item.kind) == ForecastResultKind.BASE
                        else CalibratedForecast.model_validate(item.payload)
                    )
                    for item in connection.execute(
                        select(
                            forecast_records.c.forecast_id,
                            forecast_records.c.kind,
                            forecast_records.c.payload,
                        ).where(forecast_records.c.forecast_id.in_(forecast_ids))
                    )
                }
                if forecast_ids
                else {}
            )
            target_ids = tuple(item.target_id for item in records if item.target_id is not None)
            targets = (
                {
                    item.target_id: PortfolioTarget.model_validate(item.payload)
                    for item in connection.execute(
                        select(
                            portfolio_targets.c.target_id,
                            portfolio_targets.c.payload,
                        ).where(portfolio_targets.c.target_id.in_(target_ids))
                    )
                }
                if target_ids
                else {}
            )
            risks = (
                {
                    item.target_id: PortfolioRiskDecision.model_validate(item.payload)
                    for item in connection.execute(
                        select(
                            portfolio_risk_decisions.c.target_id,
                            portfolio_risk_decisions.c.payload,
                        ).where(portfolio_risk_decisions.c.target_id.in_(target_ids))
                    )
                }
                if target_ids
                else {}
            )
            approved_ids = tuple(
                sorted(
                    {
                        *(
                            item.approved_target.approved_target_id
                            for item in risks.values()
                            if item.approved_target is not None
                        ),
                        *(
                            item.execution_authorization_id
                            for item in records
                            if item.execution_authorization_id is not None
                        ),
                    }
                )
            )
            plans = (
                {
                    item.approved_target_id: TradePlan.model_validate(item.payload)
                    for item in connection.execute(
                        select(
                            trade_plans.c.approved_target_id,
                            trade_plans.c.payload,
                        ).where(trade_plans.c.approved_target_id.in_(approved_ids))
                    )
                }
                if approved_ids
                else {}
            )
            plan_ids = tuple(item.plan_id for item in plans.values())
            groups_by_plan: dict[str, list[ExecutionGroup]] = {}
            if plan_ids:
                for item in connection.execute(
                    select(execution_groups.c.plan_id, execution_groups.c.payload).where(
                        execution_groups.c.plan_id.in_(plan_ids)
                    )
                ):
                    groups_by_plan.setdefault(item.plan_id, []).append(
                        ExecutionGroup.model_validate(item.payload)
                    )
            order_counts = (
                {
                    item.plan_id: int(item.order_count)
                    for item in connection.execute(
                        select(
                            execution_groups.c.plan_id,
                            func.count(mock_product_orders.c.client_order_id).label("order_count"),
                        )
                        .select_from(
                            execution_groups.outerjoin(
                                mock_product_orders,
                                mock_product_orders.c.group_id == execution_groups.c.group_id,
                            )
                        )
                        .where(execution_groups.c.plan_id.in_(plan_ids))
                        .group_by(execution_groups.c.plan_id)
                    )
                }
                if plan_ids
                else {}
            )
        return tuple(
            self._activity_row(
                record=record,
                target=(targets.get(record.target_id) if record.target_id is not None else None),
                risks=risks,
                plans=plans,
                groups_by_plan=groups_by_plan,
                order_counts=order_counts,
                forecasts=loaded_forecasts,
            )
            for record in records
        )

    def _activity_row(
        self,
        *,
        record: CapitalCycleRecord,
        target: PortfolioTarget | None,
        risks: dict[str, PortfolioRiskDecision],
        plans: dict[str, TradePlan],
        groups_by_plan: dict[str, list[ExecutionGroup]],
        order_counts: dict[str, int],
        forecasts: dict[str, BaseForecast | CalibratedForecast],
    ) -> CapitalActivity:
        candidate_economics = self._candidate_economics(
            target=target,
            forecasts=forecasts,
        )
        candidate_economics_recorded = (
            target is not None and target.candidate_evaluations is not None
        )
        if record.outcome in {CapitalCycleOutcome.CASH, CapitalCycleOutcome.HOLD}:
            return CapitalActivity(
                activity_id=record.record_id,
                at=record.evaluated_at,
                symbol=record.symbol,
                trigger_types=record.trigger_types,
                outcome=record.outcome.value,
                summary=self._routine_summary(record),
                reason_codes=record.reason_codes,
            )
        if (
            record.outcome == CapitalCycleOutcome.RISK_EXIT
            and record.execution_authorization_id is not None
        ):
            plan = plans.get(record.execution_authorization_id)
            if plan is None:
                return CapitalActivity(
                    activity_id=record.record_id,
                    at=record.evaluated_at,
                    symbol=record.symbol,
                    trigger_types=record.trigger_types,
                    outcome="PENDING",
                    summary="程序化风控已要求减险，等待交易计划",
                    reason_codes=record.reason_codes,
                    risk_outcome="REDUCE_ONLY",
                )
            groups = tuple(groups_by_plan.get(plan.plan_id, ()))
            order_count = order_counts.get(plan.plan_id, 0)
            if not groups:
                outcome = "NO_ORDER"
                summary = "程序化减险已授权，当前数量无需下单"
            elif all(item.terminal for item in groups):
                outcome = "EXECUTED"
                summary = f"程序化减险完成：{len(groups)} 个交易组，{order_count} 笔订单"
            else:
                outcome = "EXECUTING"
                summary = f"程序化减险执行中：{len(groups)} 个交易组，{order_count} 笔订单"
            return CapitalActivity(
                activity_id=record.record_id,
                at=record.evaluated_at,
                symbol=record.symbol,
                trigger_types=record.trigger_types,
                outcome=outcome,
                summary=summary,
                reason_codes=record.reason_codes,
                risk_outcome="REDUCE_ONLY",
                order_count=order_count,
            )
        if target is None:
            raise ValueError("Capital activity record 缺少绑定 Target")
        risk = risks.get(target.target_id)
        if risk is None:
            return CapitalActivity(
                activity_id=record.record_id,
                at=record.evaluated_at,
                symbol=record.symbol,
                trigger_types=record.trigger_types,
                outcome="PENDING",
                summary="组合目标已生成，等待风险审核",
                reason_codes=target.reason_codes,
                candidate_economics_recorded=candidate_economics_recorded,
                candidate_economics=candidate_economics,
            )
        if risk.approved_target is None:
            return CapitalActivity(
                activity_id=record.record_id,
                at=record.evaluated_at,
                symbol=record.symbol,
                trigger_types=record.trigger_types,
                outcome="RISK_REJECTED",
                summary="组合目标被程序化风控拒绝",
                reason_codes=target.reason_codes,
                risk_outcome=risk.outcome.value,
                candidate_economics_recorded=candidate_economics_recorded,
                candidate_economics=candidate_economics,
            )
        if record.outcome in {
            CapitalCycleOutcome.FORECAST_ALREADY_DECIDED,
            CapitalCycleOutcome.OPPORTUNITY_ALREADY_DECIDED,
        }:
            return CapitalActivity(
                activity_id=record.record_id,
                at=record.evaluated_at,
                symbol=record.symbol,
                trigger_types=record.trigger_types,
                outcome=record.outcome.value,
                summary="同一 Forecast 已经完成资本决策，本轮未重复下单",
                reason_codes=target.reason_codes,
                risk_outcome=risk.outcome.value,
                candidate_economics_recorded=candidate_economics_recorded,
                candidate_economics=candidate_economics,
            )
        plan = plans.get(risk.approved_target.approved_target_id)
        if plan is None:
            return CapitalActivity(
                activity_id=record.record_id,
                at=record.evaluated_at,
                symbol=record.symbol,
                trigger_types=record.trigger_types,
                outcome="PENDING",
                summary="风险审核通过，等待交易计划",
                reason_codes=target.reason_codes,
                risk_outcome=risk.outcome.value,
                candidate_economics_recorded=candidate_economics_recorded,
                candidate_economics=candidate_economics,
            )
        groups = tuple(groups_by_plan.get(plan.plan_id, ()))
        order_count = order_counts.get(plan.plan_id, 0)
        if not groups:
            outcome = "NO_ORDER"
            summary = "组合决策完成，无需产生订单"
        elif all(item.terminal for item in groups):
            outcome = "EXECUTED"
            summary = f"模拟执行完成：{len(groups)} 个交易组，{order_count} 笔订单"
        else:
            outcome = "EXECUTING"
            summary = f"正在执行：{len(groups)} 个交易组，{order_count} 笔订单"
        return CapitalActivity(
            activity_id=record.record_id,
            at=record.evaluated_at,
            symbol=record.symbol,
            trigger_types=record.trigger_types,
            outcome=outcome,
            summary=summary,
            reason_codes=target.reason_codes,
            risk_outcome=risk.outcome.value,
            order_count=order_count,
            candidate_economics_recorded=candidate_economics_recorded,
            candidate_economics=candidate_economics,
        )

    def _candidate_economics(
        self,
        *,
        target: PortfolioTarget | None,
        forecasts: dict[str, BaseForecast | CalibratedForecast],
    ) -> tuple[CapitalCandidateEconomics, ...]:
        if target is None:
            return ()
        if target.candidate_evaluations is None:
            return ()
        candidates = []
        for evaluation in target.candidate_evaluations:
            forecast = forecasts.get(evaluation.forecast_id)
            if forecast is None:
                raise ValueError("PortfolioTarget candidate 缺少不可变 Forecast 引用")
            candidates.append(
                CapitalCandidateEconomics(
                    forecast_id=forecast.forecast_id,
                    producer_id=forecast.producer_id,
                    outcome_family_id=forecast.outcome_family_id,
                    information_cutoff_at=forecast.information_cutoff_at,
                    available_at=forecast.available_at,
                    valid_until=forecast.valid_until,
                    world_model_id=(
                        forecast.world_model_id if isinstance(forecast, BaseForecast) else None
                    ),
                    outcome_probabilities=tuple(
                        (item.bucket_id, item.probability)
                        for item in forecast.outcome_probabilities
                    ),
                    mechanism_contributions=(
                        tuple(
                            (item.mechanism_id, item.effect.value, item.rationale)
                            for item in forecast.mechanism_contributions
                        )
                        if isinstance(forecast, BaseForecast)
                        else ()
                    ),
                    evidence_refs=(
                        forecast.evidence_refs if isinstance(forecast, BaseForecast) else ()
                    ),
                    analysis_input=(
                        json.loads(forecast.analysis_input_json)
                        if isinstance(forecast, BaseForecast)
                        and forecast.analysis_input_json is not None
                        else None
                    ),
                    gross_bps=evaluation.decision_gross_bps,
                    estimated_cost_bps=evaluation.cost.total_bps,
                    net_bps=evaluation.decision_net_bps,
                    decision_threshold_bps=evaluation.minimum_net_bps,
                    current_gross_notional=evaluation.current_gross_notional,
                    evaluation_gross_notional=evaluation.evaluation_gross_notional,
                    desired_gross_notional=evaluation.desired_gross_notional,
                    eligible=evaluation.eligible,
                    reason_codes=evaluation.reason_codes,
                )
            )
        return tuple(candidates)

    @staticmethod
    def _routine_summary(record: CapitalCycleRecord) -> str:
        reasons = set(record.reason_codes)
        if "PROGRAMMATIC_RISK_REVIEW" in reasons:
            return (
                "程序化账户与风险复核完成，现有仓位保持不变"
                if record.outcome == CapitalCycleOutcome.HOLD
                else "程序化账户与风险复核完成，当前保持现金"
            )
        no_estimate = next(
            (item for item in record.reason_codes if item.startswith("FORECAST_NO_ESTIMATE:")),
            None,
        )
        if no_estimate is not None:
            return f"预测源未形成可用概率估计，当前保持现金（{no_estimate.split(':', 1)[1]}）"
        if "NO_REGISTERED_FORECAST_SOURCE" in reasons:
            return "当前没有装配可运行的预测源，资金保持现金"
        if record.outcome == CapitalCycleOutcome.HOLD:
            return "本轮预测与程序化约束未要求改变现有仓位"
        return "本轮没有形成可进入组合比较的预测，资金保持现金"

    @staticmethod
    def _latest_payload(
        connection,
        payload_column,
        time_column,
        id_column,
        model,
        *,
        secondary_order=None,
        where_clause=None,
    ):
        ordering = [time_column.desc()]
        if secondary_order is not None:
            ordering.append(secondary_order.desc())
        ordering.append(id_column.desc())
        statement = select(payload_column)
        if where_clause is not None:
            statement = statement.where(where_clause)
        payload = connection.execute(statement.order_by(*ordering).limit(1)).scalar_one_or_none()
        return None if payload is None else model.model_validate(payload)

    @staticmethod
    def _payload_for(connection, statement, model):
        payload = connection.execute(statement).scalar_one_or_none()
        return None if payload is None else model.model_validate(payload)


def serialize_capital_overview(overview: CapitalOverview) -> dict:
    account = overview.account
    cycle_record = overview.cycle_record
    target = overview.target
    risk = overview.risk
    performance = overview.latest_performance
    return {
        "enabled": overview.enabled,
        "account": None
        if account is None
        else {
            "as_of": _iso(account.as_of),
            "cash_balance": str(account.cash_balance),
            "equity": str(account.equity),
            "daily_pnl": str(account.daily_pnl),
            "drawdown_fraction": str(account.drawdown_fraction),
            "reconciled": account.reconciled,
            "kill_switch_active": account.kill_switch_active,
            "positions": [
                {
                    "instrument": item.instrument.key,
                    "quantity": str(item.quantity),
                    "average_price": str(item.average_price),
                }
                for item in account.positions
            ],
        },
        "decision": {
            "as_of": _iso(
                target.as_of
                if target is not None
                else cycle_record.evaluated_at
                if cycle_record is not None
                else None
            ),
            "mode": (
                "DECIDE"
                if target is not None
                else "NO_CHANGE"
                if cycle_record is not None
                else None
            ),
            "reason_codes": list(
                target.reason_codes
                if target is not None
                else cycle_record.reason_codes
                if cycle_record is not None
                else ()
            ),
            "risk_outcome": risk.outcome.value if risk is not None else None,
        },
        "execution": {
            "active_group_count": len(overview.active_groups),
            "active_groups": [
                {
                    "group_id": item.group_id,
                    "status": item.status.value,
                    "updated_at": _iso(item.updated_at),
                    "unhedged_notional": str(item.unhedged_notional),
                }
                for item in overview.active_groups
            ],
            "total_order_count": overview.total_order_count,
        },
        "performance": {
            "interval_count": overview.performance_interval_count,
            "cumulative_net_pnl": str(overview.cumulative_net_pnl),
            "latest": None
            if performance is None
            else {
                "kind": performance.kind.value,
                "start_as_of": _iso(performance.start_as_of),
                "end_as_of": _iso(performance.end_as_of),
                "net_pnl": str(performance.net_pnl),
                "return_fraction": str(performance.return_fraction),
            },
        },
        "forecast_evidence": None
        if overview.forecast_evidence is None
        else {
            "status": overview.forecast_evidence.status.value,
            "terminal_result_count": overview.forecast_evidence.terminal_result_count,
            "due_slot_count": overview.forecast_evidence.due_slot_count,
            "result_coverage": (
                None
                if overview.forecast_evidence.result_coverage is None
                else str(overview.forecast_evidence.result_coverage)
            ),
            "permission_evidence_eligible": (
                overview.forecast_evidence.permission_evidence_eligible
            ),
            "forecast_count": overview.forecast_evidence.forecast_count,
            "no_estimate_count": overview.forecast_evidence.no_estimate_count,
            "settled_forecast_count": overview.forecast_evidence.settled_forecast_count,
            "non_overlapping_sample_count": (
                overview.forecast_evidence.non_overlapping_sample_count
            ),
            "required_non_overlapping_samples": (
                overview.forecast_evidence.required_non_overlapping_samples
            ),
            "mean_brier_score": (
                None
                if overview.forecast_evidence.mean_brier_score is None
                else str(overview.forecast_evidence.mean_brier_score)
            ),
            "benchmark_mean_brier_score": (
                None
                if overview.forecast_evidence.benchmark_mean_brier_score is None
                else str(overview.forecast_evidence.benchmark_mean_brier_score)
            ),
            "brier_skill": (
                None
                if overview.forecast_evidence.brier_skill is None
                else str(overview.forecast_evidence.brier_skill)
            ),
            "mean_expected_gross_bps": (
                None
                if overview.forecast_evidence.mean_expected_gross_bps is None
                else str(overview.forecast_evidence.mean_expected_gross_bps)
            ),
            "mean_realized_gross_bps": (
                None
                if overview.forecast_evidence.mean_realized_gross_bps is None
                else str(overview.forecast_evidence.mean_realized_gross_bps)
            ),
        },
    }


def serialize_capital_activity(items: tuple[CapitalActivity, ...]) -> dict:
    return {
        "actions": [
            {
                "activity_id": item.activity_id,
                "at": _iso(item.at),
                "symbol": item.symbol,
                "trigger_types": list(item.trigger_types),
                "outcome": item.outcome,
                "summary": item.summary,
                "reason_codes": list(item.reason_codes),
                "risk_outcome": item.risk_outcome,
                "order_count": item.order_count,
                "candidate_economics_recorded": item.candidate_economics_recorded,
                "candidate_economics": [
                    {
                        "forecast_id": candidate.forecast_id,
                        "producer_id": candidate.producer_id,
                        "outcome_family_id": candidate.outcome_family_id,
                        "information_cutoff_at": _iso(candidate.information_cutoff_at),
                        "available_at": _iso(candidate.available_at),
                        "valid_until": _iso(candidate.valid_until),
                        "world_model_id": candidate.world_model_id,
                        "outcome_probabilities": [
                            {"bucket_id": bucket_id, "probability": str(probability)}
                            for bucket_id, probability in candidate.outcome_probabilities
                        ],
                        "mechanism_contributions": [
                            {
                                "mechanism_id": mechanism_id,
                                "effect": effect,
                                "rationale": rationale,
                            }
                            for mechanism_id, effect, rationale in candidate.mechanism_contributions
                        ],
                        "evidence_refs": list(candidate.evidence_refs),
                        "analysis_input": candidate.analysis_input,
                        "gross_bps": str(candidate.gross_bps),
                        "estimated_cost_bps": str(candidate.estimated_cost_bps),
                        "net_bps": str(candidate.net_bps),
                        "decision_threshold_bps": str(candidate.decision_threshold_bps),
                        "current_gross_notional": str(candidate.current_gross_notional),
                        "evaluation_gross_notional": str(
                            candidate.evaluation_gross_notional
                        ),
                        "desired_gross_notional": str(candidate.desired_gross_notional),
                        "eligible": candidate.eligible,
                        "reason_codes": list(candidate.reason_codes),
                    }
                    for candidate in item.candidate_economics
                ],
            }
            for item in items
        ]
    }


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()
