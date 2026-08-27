"""Read-only projections of forecast and capital evaluation evidence."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import datetime

from sqlalchemy import and_, select
from sqlalchemy.engine import Engine

from investment_manager.execution.group.models import ExecutionGroup
from investment_manager.execution.tables import execution_groups
from investment_manager.forecast.context.evaluation import (
    ForecastEvidence,
    ForecastScoringCase,
    ForecastSourceEvidence,
    evaluate_forecast_evidence,
)
from investment_manager.forecast.context.stability import (
    SqlContextForecastStabilityRepository,
)
from investment_manager.forecast.contracts import (
    ForecastContract,
    ForecastDecisionSlot,
    ForecastSlotStratum,
)
from investment_manager.forecast.product.evaluation import (
    ProductPayoffEvidence,
    ProductPayoffMappingIdentity,
    evaluate_product_payoff_evidence,
)
from investment_manager.forecast.product.repository import (
    SqlProductPayoffProjectionStore,
)
from investment_manager.forecast.results import (
    BaseForecast,
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
from investment_manager.governance.evaluation.world_model_ablation import (
    SqlWorldModelAblationRepository,
    WorldModelAblationReport,
)
from investment_manager.kernel.time import require_utc
from investment_manager.portfolio.evaluation import (
    CapitalChoiceCase,
    CapitalChoiceEvidence,
    ExecutionFillCase,
    TradingCostEvidence,
    evaluate_capital_choice,
    evaluate_trading_cost,
    is_full_forecast_capital_choice,
)
from investment_manager.portfolio.models import (
    CapitalCycleRecord,
    PortfolioTarget,
)
from investment_manager.portfolio.repository import SqlPortfolioStore
from investment_manager.portfolio.stability import (
    PortfolioForecastStabilityEvaluator,
    PortfolioForecastStabilityReport,
)
from investment_manager.portfolio.tables import (
    capital_cycle_records,
    portfolio_targets,
)
from investment_manager.settings import AppConfig


class EvaluationDashboardReader:
    """Derive evaluation views from immutable domain facts without writes."""

    def __init__(self, engine: Engine, config: AppConfig) -> None:
        self._engine = engine
        self._config = config

    def forecast_evidence(self, *, now: datetime) -> ForecastEvidence | None:
        now = require_utc(now)
        with self._engine.connect() as connection:
            return self._forecast_evidence(connection, now=now)

    def forecast_stability_evidence(
        self,
    ) -> PortfolioForecastStabilityReport | None:
        policy = self._config.outcome_evaluation.context_forecast_stability
        context = self._config.capital.context_forecast
        if (
            policy is None
            or not policy.enabled
            or context is None
            or not context.enabled
            or not self._config.capital.enabled
        ):
            return None
        repository = SqlContextForecastStabilityRepository(self._engine)
        assignments = repository.assignments(
            policy_version=policy.version,
            formal_producer_behavior_id=context.producer_behavior_id,
        )
        return PortfolioForecastStabilityEvaluator(
            engine=self._engine,
            capital_policy=self._config.capital,
        ).evaluate(
            assignments=assignments,
            results=repository.results(tuple(item.assignment_id for item in assignments)),
        )

    def world_model_ablation_evidence(self, *, now: datetime) -> WorldModelAblationReport | None:
        """Read the active prospective comparison without registering or mutating it."""

        now = require_utc(now)
        policy = self._config.outcome_evaluation.world_model_ablation
        context = self._config.capital.context_forecast
        if policy is None or not policy.enabled or context is None or not context.enabled:
            return None
        return SqlWorldModelAblationRepository(self._engine).report(
            plan_id=policy.plan_id,
            evaluation_version=self._config.outcome_evaluation.target_forecast_version,
            minimum_sample_size=policy.minimum_sample_size,
            formal_producer_behavior_id=context.producer_behavior_id,
            activated_at=policy.activated_at,
            as_of=now,
        )

    def product_payoff_evidence(self) -> ProductPayoffEvidence | None:
        context = self._config.capital.context_forecast
        if (
            context is None
            or not context.enabled
            or not any(item.product_payoffs is not None for item in context.targets)
        ):
            return None
        evaluation = self._config.outcome_evaluation
        mapping_cohort = tuple(
            sorted(
                ProductPayoffMappingIdentity(
                    economic_exposure_id=payoffs.economic_exposure_id,
                    projection_version=payoffs.version,
                    instrument_keys=payoffs.instrument_keys,
                    maximum_rule_age_seconds=payoffs.maximum_rule_age_seconds,
                )
                for target in context.targets
                if (payoffs := target.product_payoffs) is not None
            )
        )
        cases = SqlProductPayoffProjectionStore(self._engine).outcome_cases(
            product_outcome_version=evaluation.product_payoff_version,
            forecast_outcome_version=evaluation.target_forecast_version,
            producer_behavior_id=context.producer_behavior_id,
            mapping_cohort=mapping_cohort,
        )
        return evaluate_product_payoff_evidence(
            cases,
            mapping_cohort=mapping_cohort,
            product_outcome_version=evaluation.product_payoff_version,
            forecast_outcome_version=evaluation.target_forecast_version,
            required_independent_source_forecasts=(evaluation.product_payoff_minimum_sample_size),
        )

    def capital_choice_evidence(self) -> CapitalChoiceEvidence | None:
        """Evaluate the newest decision whose complete candidate set has settled."""

        if not self._config.capital.enabled:
            return None
        store = SqlProductPayoffProjectionStore(self._engine)
        outcome_version = self._config.outcome_evaluation.product_payoff_version
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(portfolio_targets.c.payload, capital_cycle_records.c.payload)
                .select_from(
                    portfolio_targets.join(
                        capital_cycle_records,
                        capital_cycle_records.c.target_id == portfolio_targets.c.target_id,
                    )
                )
                .where(
                    portfolio_targets.c.portfolio_id == self._config.capital.decision.portfolio_id,
                    capital_cycle_records.c.pipeline_id == self._config.capital.version,
                )
                .order_by(portfolio_targets.c.as_of.desc(), portfolio_targets.c.target_id.desc())
                .execution_options(stream_results=True)
            )
            for row in rows:
                target = PortfolioTarget.model_validate(row[0])
                receipt = CapitalCycleRecord.model_validate(row[1])
                if not is_full_forecast_capital_choice(
                    receipt,
                    capital_behavior_id=self._config.capital.version,
                ):
                    continue
                candidates = target.candidate_evaluations
                if not candidates or any(item.payoff_projection_id is None for item in candidates):
                    continue
                projection_ids = tuple(
                    sorted(
                        item.payoff_projection_id
                        for item in candidates
                        if item.payoff_projection_id is not None
                    )
                )
                resolved = store.projection_outcomes(
                    projection_ids=projection_ids,
                    evaluation_version=outcome_version,
                )
                if len(resolved) != len(projection_ids):
                    continue
                if any(
                    outcome.status != ForecastOutcomeStatus.SETTLED
                    or outcome.realized_gross_bps is None
                    for _projection, outcome in resolved
                ):
                    continue
                by_projection = {
                    projection.projection_id: (projection, outcome)
                    for projection, outcome in resolved
                }
                cases = []
                for candidate in candidates:
                    projection_id = candidate.payoff_projection_id
                    assert projection_id is not None
                    projection, outcome = by_projection[projection_id]
                    if (
                        projection.source_forecast_id != candidate.forecast_id
                        or outcome.projection_id != projection_id
                        or outcome.source_forecast_id != candidate.forecast_id
                        or outcome.evaluation_at != projection.evaluation_at
                    ):
                        raise ValueError("Capital choice 候选、产品投影与结果身份不一致")
                    leg = projection.target.legs[0]
                    assert outcome.realized_gross_bps is not None
                    cases.append(
                        CapitalChoiceCase(
                            decision_id=target.target_id,
                            decision_at=target.as_of,
                            evaluation_at=projection.evaluation_at,
                            economic_exposure_id=projection.economic_exposure_id,
                            projection_id=projection_id,
                            instrument_key=leg.instrument.key,
                            direction=leg.direction,
                            selected=candidate.desired_gross_notional > 0,
                            predicted_net_bps=candidate.decision_net_bps,
                            decision_gross_bps=candidate.decision_gross_bps,
                            projection_gross_bps=projection.conservative_gross_bps,
                            decision_cost_bps=candidate.cost.total_bps,
                            realized_product_gross_bps=outcome.realized_gross_bps,
                        )
                    )
                return evaluate_capital_choice(
                    tuple(cases),
                    capital_behavior_id=self._config.capital.version,
                )
        return None

    def trading_cost_evidence(self) -> TradingCostEvidence:
        """Reconstruct fee drag from immutable terminal execution fills."""

        with self._engine.connect() as connection:
            groups = tuple(
                ExecutionGroup.model_validate(item)
                for item in connection.execute(
                    select(execution_groups.c.payload)
                    .where(execution_groups.c.terminal.is_(True))
                    .order_by(
                        execution_groups.c.started_at,
                        execution_groups.c.group_id,
                    )
                ).scalars()
            )
        fills = []
        for group in groups:
            for leg in (*group.target_legs, *group.compensation_legs):
                if leg.filled_quantity <= 0:
                    continue
                if leg.average_fill_price is None or leg.observed_at is None:
                    raise ValueError("终态 Execution fill 缺少成交价格或观察时间")
                fills.append(
                    ExecutionFillCase(
                        fill_id=leg.execution_leg_id,
                        cycle_id=group.cycle_id,
                        sleeve_id=group.sleeve_id,
                        instrument_key=leg.instrument.key,
                        side=leg.side,
                        group_started_at=group.started_at,
                        filled_at=leg.observed_at,
                        quantity=leg.filled_quantity,
                        price=leg.average_fill_price,
                        contract_multiplier=leg.instrument.contract_multiplier,
                        fee=leg.fee,
                    )
                )
        account = SqlPortfolioStore(self._engine).head_account(
            portfolio_id=self._config.capital.decision.portfolio_id
        )
        accounting = None if account is None else account.accounting
        can_reconcile = account is not None and accounting is not None and not account.positions
        return evaluate_trading_cost(
            tuple(fills),
            expected_price_pnl=(accounting.price_pnl if can_reconcile else None),
            expected_fee_cost=(accounting.fee_cost if can_reconcile else None),
        )

    def _forecast_evidence(self, connection, *, now: datetime) -> ForecastEvidence | None:
        policy = self._config.capital.context_forecast
        if policy is None or not policy.enabled:
            return None
        target_versions = {item.outcome_family_id: item.contract_version for item in policy.targets}
        contract_rows = connection.execute(
            select(forecast_contracts.c.contract_id, forecast_contracts.c.payload).where(
                forecast_contracts.c.outcome_family_id.in_(tuple(target_versions))
            )
        ).all()
        contracts = tuple(
            ForecastContract.model_validate(row.payload)
            for row in contract_rows
            if target_versions.get(row.payload["outcome_family_id"])
            == row.payload["contract_version"]
        )
        if not contracts:
            return None
        contract_by_id = {item.contract_id: item for item in contracts}
        contract_ids = tuple(contract_by_id)
        due_rows = connection.execute(
            select(
                forecast_decision_slots.c.slot_id,
                forecast_decision_slots.c.payload,
            )
            .select_from(
                forecast_slot_obligations.join(
                    forecast_decision_slots,
                    forecast_decision_slots.c.slot_id == forecast_slot_obligations.c.slot_id,
                )
            )
            .where(
                forecast_decision_slots.c.contract_id.in_(contract_ids),
                forecast_slot_obligations.c.producer_id == policy.producer_id,
                forecast_slot_obligations.c.producer_behavior_id == policy.producer_behavior_id,
                forecast_decision_slots.c.completion_deadline_at <= now,
            )
        ).all()
        due_slots = {
            row.slot_id: ForecastDecisionSlot.model_validate(row.payload) for row in due_rows
        }
        forecast_slot_ids = tuple(
            connection.scalars(
                select(forecast_records.c.decision_slot_id)
                .join(
                    forecast_decision_slots,
                    forecast_decision_slots.c.slot_id == forecast_records.c.decision_slot_id,
                )
                .where(
                    forecast_records.c.contract_id.in_(contract_ids),
                    forecast_records.c.kind == ForecastResultKind.BASE.value,
                    forecast_records.c.producer_id == policy.producer_id,
                    forecast_records.c.producer_behavior_id == policy.producer_behavior_id,
                    forecast_decision_slots.c.completion_deadline_at <= now,
                )
            ).all()
        )
        no_estimate_slot_ids = tuple(
            connection.scalars(
                select(forecast_no_estimates.c.slot_id)
                .join(
                    forecast_decision_slots,
                    forecast_decision_slots.c.slot_id == forecast_no_estimates.c.slot_id,
                )
                .where(
                    forecast_no_estimates.c.contract_id.in_(contract_ids),
                    forecast_no_estimates.c.producer_id == policy.producer_id,
                    forecast_no_estimates.c.producer_behavior_id == policy.producer_behavior_id,
                    forecast_decision_slots.c.completion_deadline_at <= now,
                )
            ).all()
        )
        terminal_slot_ids = {*forecast_slot_ids, *no_estimate_slot_ids}
        if not terminal_slot_ids.issubset(due_slots):
            raise ValueError("Forecast evidence 终态缺少对应到期槽")
        due_slot_count = len(due_slots)
        forecast_count = len(forecast_slot_ids)
        no_estimate_count = len(no_estimate_slot_ids)
        rows = connection.execute(
            select(
                forecast_records.c.payload,
                forecast_outcomes.c.payload,
                forecast_decision_slots.c.payload,
            )
            .select_from(
                forecast_records.join(
                    forecast_outcomes,
                    and_(
                        forecast_outcomes.c.decision_slot_id == forecast_records.c.decision_slot_id,
                        forecast_outcomes.c.evaluation_version
                        == self._config.outcome_evaluation.target_forecast_version,
                    ),
                ).join(
                    forecast_decision_slots,
                    forecast_decision_slots.c.slot_id == forecast_records.c.decision_slot_id,
                )
            )
            .where(
                forecast_records.c.contract_id.in_(contract_ids),
                forecast_records.c.kind == ForecastResultKind.BASE.value,
                forecast_records.c.producer_id == policy.producer_id,
                forecast_records.c.producer_behavior_id == policy.producer_behavior_id,
                forecast_outcomes.c.status == ForecastOutcomeStatus.SETTLED.value,
            )
            .order_by(forecast_records.c.available_at, forecast_records.c.forecast_id)
        ).all()
        cases = []
        for row in rows:
            forecast = BaseForecast.model_validate(row[0])
            outcome = ForecastOutcome.model_validate(row[1])
            slot = ForecastDecisionSlot.model_validate(row[2])
            contract = contract_by_id[forecast.contract_id]
            benchmark = tuple(
                (item.bucket_id, item.probability) for item in contract.forecast_benchmark
            )
            assert outcome.gross_target_return_bps is not None
            assert outcome.realized_bucket_id is not None
            cases.append(
                ForecastScoringCase(
                    forecast_id=forecast.forecast_id,
                    cohort_key=forecast.outcome_family_id,
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
                    market_state_key=self._forecast_market_state_key(forecast),
                    outcome_available_at=outcome.settled_at,
                    source_stratum=slot.stratum,
                )
            )
        source_evidence = tuple(
            ForecastSourceEvidence(
                stratum=stratum,
                evidence=evaluate_forecast_evidence(
                    tuple(item for item in cases if item.source_stratum == stratum),
                    due_slot_count=sum(slot.stratum == stratum for slot in due_slots.values()),
                    forecast_count=sum(
                        due_slots[slot_id].stratum == stratum for slot_id in forecast_slot_ids
                    ),
                    no_estimate_count=sum(
                        due_slots[slot_id].stratum == stratum for slot_id in no_estimate_slot_ids
                    ),
                    required_non_overlapping_samples=(
                        self._config.outcome_evaluation.target_forecast_minimum_sample_size
                        * len(contracts)
                    ),
                    permission_evidence_eligible=False,
                ),
            )
            for stratum in ForecastSlotStratum
        )
        overall = evaluate_forecast_evidence(
            tuple(cases),
            due_slot_count=due_slot_count,
            forecast_count=forecast_count,
            no_estimate_count=no_estimate_count,
            required_non_overlapping_samples=(
                self._config.outcome_evaluation.target_forecast_minimum_sample_size * len(contracts)
            ),
            permission_evidence_eligible=False,
        )
        return replace(overall, source_evidence=source_evidence)

    def _forecast_market_state_key(forecast: BaseForecast) -> str | None:
        """Read the point-in-time regime frozen in this Forecast, never current state."""

        if forecast.analysis_input_json is None:
            return None
        try:
            payload = json.loads(forecast.analysis_input_json)
            target_states = payload.get("forecast_targets")
            if target_states is not None:
                matching = next(
                    (
                        item["target_state"]
                        for item in target_states
                        if item.get("decision_slot", {}).get("decision_slot_id")
                        == forecast.decision_slot_id
                    ),
                    None,
                )
                assets = () if matching is None else matching.get("asset_states", ())
            else:
                assets = payload["target_state"]["asset_states"]
            regime = assets[0]["regime"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
            return None
        return regime if isinstance(regime, str) and regime else None


def serialize_forecast_evidence(evidence: ForecastEvidence | None) -> dict:
    if evidence is None:
        return {"forecast_evidence": None}
    return {"forecast_evidence": _serialize_forecast_evidence_payload(evidence)}


def serialize_forecast_stability_evidence(
    evidence: PortfolioForecastStabilityReport | None,
) -> dict:
    return {
        "forecast_stability_evidence": (
            None if evidence is None else evidence.model_dump(mode="json")
        )
    }


def serialize_product_payoff_evidence(
    evidence: ProductPayoffEvidence | None,
) -> dict:
    if evidence is None:
        return {"product_payoff_evidence": None}
    payload = asdict(evidence)
    payload["status"] = evidence.status.value
    for field_name in (
        "mean_absolute_mapping_error_bps",
        "mapping_conservative_coverage",
        "mapping_residual_sign_accuracy",
    ):
        value = getattr(evidence, field_name)
        payload[field_name] = None if value is None else str(value)
    return {"product_payoff_evidence": payload}


def serialize_capital_choice_evidence(
    evidence: CapitalChoiceEvidence | None,
) -> dict:
    if evidence is None:
        return {"capital_choice_evidence": None}

    def candidate(item):
        if item is None:
            return None
        return {
            "projection_id": item.projection_id,
            "instrument_key": item.instrument_key,
            "direction": item.direction.value,
            "predicted_net_bps": str(item.predicted_net_bps),
            "realized_net_bps": str(item.realized_net_bps),
        }

    return {
        "capital_choice_evidence": {
            "evaluation_version": evidence.evaluation_version,
            "capital_behavior_id": evidence.capital_behavior_id,
            "decision_id": evidence.decision_id,
            "decision_at": _iso(evidence.decision_at),
            "evaluation_at": _iso(evidence.evaluation_at),
            "candidate_count": evidence.candidate_count,
            "missed_profitable_exposure_count": (evidence.missed_profitable_exposure_count),
            "selected_unprofitable_exposure_count": (evidence.selected_unprofitable_exposure_count),
            "exposures": [
                {
                    "economic_exposure_id": item.economic_exposure_id,
                    "selected": candidate(item.selected),
                    "best_realized": candidate(item.best_realized),
                    "opportunity_gap_bps": str(item.opportunity_gap_bps),
                    "missed_profitable_exposure": item.missed_profitable_exposure,
                    "selected_unprofitable_exposure": (item.selected_unprofitable_exposure),
                }
                for item in evidence.exposures
            ],
        }
    }


def serialize_trading_cost_evidence(evidence: TradingCostEvidence) -> dict:
    optional_decimals = (
        "closed_fee_to_realized_gross_pnl",
        "closed_fee_to_positive_gross_pnl",
        "minimum_holding_seconds",
        "median_holding_seconds",
        "maximum_holding_seconds",
    )
    payload = {
        "evaluation_version": evidence.evaluation_version,
        "fill_count": evidence.fill_count,
        "round_trip_count": evidence.round_trip_count,
        "open_lot_count": evidence.open_lot_count,
        "gross_turnover": str(evidence.gross_turnover),
        "realized_gross_pnl": str(evidence.realized_gross_pnl),
        "closed_fee_cost": str(evidence.closed_fee_cost),
        "open_fee_cost": str(evidence.open_fee_cost),
        "realized_net_pnl": str(evidence.realized_net_pnl),
        "positive_gross_pnl": str(evidence.positive_gross_pnl),
        "cost_reversal_round_trip_count": evidence.cost_reversal_round_trip_count,
        "accounting_reconciled": evidence.accounting_reconciled,
        "round_trips": [
            {
                "round_trip_id": item.round_trip_id,
                "sleeve_id": item.sleeve_id,
                "instrument_key": item.instrument_key,
                "direction": item.direction.value,
                "entry_fill_id": item.entry_fill_id,
                "exit_fill_id": item.exit_fill_id,
                "entry_cycle_id": item.entry_cycle_id,
                "exit_cycle_id": item.exit_cycle_id,
                "opened_at": _iso(item.opened_at),
                "closed_at": _iso(item.closed_at),
                "holding_seconds": str(item.holding_seconds),
                "quantity": str(item.quantity),
                "gross_turnover": str(item.gross_turnover),
                "realized_gross_pnl": str(item.realized_gross_pnl),
                "fee_cost": str(item.fee_cost),
                "realized_net_pnl": str(item.realized_net_pnl),
            }
            for item in evidence.round_trips
        ],
    }
    for field_name in optional_decimals:
        value = getattr(evidence, field_name)
        payload[field_name] = None if value is None else str(value)
    return {"trading_cost_evidence": payload}


def serialize_world_model_ablation_evidence(
    report: WorldModelAblationReport | None,
) -> dict:
    if report is None:
        return {"world_model_ablation": None}
    return {
        "world_model_ablation": {
            "plan_id": report.plan_id,
            "as_of": _iso(report.as_of),
            "formal_forecast_count": report.formal_forecast_count,
            "formal_no_estimate_count": report.formal_no_estimate_count,
            "assignments": report.assignments,
            "pending_controls": report.pending_controls,
            "successful_controls": report.successful_controls,
            "failed_controls": report.failed_controls,
            "settled_pairs": report.settled_pairs,
            "conservative_sample_count": report.conservative_sample_count,
            "mean_brier_improvement": (
                None
                if report.mean_brier_improvement is None
                else str(report.mean_brier_improvement)
            ),
            "conservative_improvement_lower_bound": (
                None
                if report.conservative_improvement_lower_bound is None
                else str(report.conservative_improvement_lower_bound)
            ),
            "minimum_sample_size": report.minimum_sample_size,
            "evidence_sufficient": report.evidence_sufficient,
        }
    }


def _serialize_forecast_evidence_payload(evidence: ForecastEvidence) -> dict:
    optional_decimals = (
        "result_coverage",
        "mean_brier_score",
        "benchmark_mean_brier_score",
        "brier_skill",
        "rolling_benchmark_mean_brier_score",
        "rolling_brier_skill",
        "rolling_brier_skill_lower_bound",
        "rolling_brier_skill_upper_bound",
        "market_benchmark_mean_brier_score",
        "market_brier_skill",
        "market_brier_skill_lower_bound",
        "market_brier_skill_upper_bound",
        "mean_expected_gross_bps",
        "mean_realized_gross_bps",
    )
    payload = asdict(evidence)
    payload.pop("source_evidence", None)
    payload["status"] = evidence.status.value
    payload["result_coverage"] = evidence.result_coverage
    for field_name in optional_decimals:
        value = getattr(evidence, field_name)
        payload[field_name] = None if value is None else str(value)
    if evidence.source_evidence:
        payload["source_evidence"] = [
            {
                "stratum": item.stratum.value,
                "evidence": _serialize_forecast_evidence_payload(item.evidence),
            }
            for item in evidence.source_evidence
        ]
    return payload


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()
