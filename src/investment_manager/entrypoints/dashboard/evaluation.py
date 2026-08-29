"""Read-only projections of forecast and capital evaluation evidence."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from threading import Lock

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from investment_manager.execution.group.models import ExecutionGroup
from investment_manager.execution.tables import execution_groups
from investment_manager.forecast.context.increment_evidence import (
    ForecastIncrementEvidence,
    SqlForecastIncrementEvidenceReader,
)
from investment_manager.forecast.context.posterior_contract import POSTERIOR_PRODUCER_ID
from investment_manager.forecast.product.repository import (
    SqlProductPayoffProjectionStore,
)
from investment_manager.forecast.program.prior import PRIOR_PRODUCER_ID
from investment_manager.forecast.results import ForecastOutcomeStatus
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
        self._trading_cost_cache_key: (
            tuple[
                int,
                int,
                datetime | None,
                tuple[str, ...],
            ]
            | None
        ) = None
        self._trading_cost_cache: TradingCostEvidence | None = None
        self._trading_cost_cache_lock = Lock()
        self._world_model_increment = SqlForecastIncrementEvidenceReader(
            engine=engine,
            outcome_evaluation_version=(config.outcome_evaluation.target_forecast_version),
            candidate_producer_id=POSTERIOR_PRODUCER_ID,
            comparator_producer_id=PRIOR_PRODUCER_ID,
        )

    def world_model_increment_evidence(self) -> ForecastIncrementEvidence:
        return self._world_model_increment.read()

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
        """Reconstruct fee drag once per immutable execution-ledger revision."""

        with self._engine.connect() as connection:
            row = connection.execute(
                select(
                    func.count(execution_groups.c.group_id),
                    func.coalesce(func.sum(execution_groups.c.revision), 0),
                    func.max(execution_groups.c.updated_at),
                ).where(execution_groups.c.terminal.is_(True))
            ).one()
        account = SqlPortfolioStore(self._engine).head_account(
            portfolio_id=self._config.capital.decision.portfolio_id
        )
        accounting = None if account is None else account.accounting
        can_reconcile = account is not None and accounting is not None and not account.positions
        reconciliation_key = (
            ("FLAT", str(accounting.price_pnl), str(accounting.fee_cost))
            if can_reconcile
            else ("UNAVAILABLE",)
        )
        cache_key = (int(row[0]), int(row[1]), row[2], reconciliation_key)
        with self._trading_cost_cache_lock:
            if cache_key == self._trading_cost_cache_key and self._trading_cost_cache is not None:
                return self._trading_cost_cache
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
            evidence = self._evaluate_trading_cost(
                groups,
                expected_price_pnl=(accounting.price_pnl if can_reconcile else None),
                expected_fee_cost=(accounting.fee_cost if can_reconcile else None),
            )
            self._trading_cost_cache_key = cache_key
            self._trading_cost_cache = evidence
            return evidence

    def _evaluate_trading_cost(
        self,
        groups: tuple[ExecutionGroup, ...],
        *,
        expected_price_pnl: Decimal | None,
        expected_fee_cost: Decimal | None,
    ) -> TradingCostEvidence:
        """Project final fills and reconcile the derived totals to the flat account."""

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
        return evaluate_trading_cost(
            tuple(fills),
            expected_price_pnl=expected_price_pnl,
            expected_fee_cost=expected_fee_cost,
        )


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
    }
    for field_name in optional_decimals:
        value = getattr(evidence, field_name)
        payload[field_name] = None if value is None else str(value)
    return {"trading_cost_evidence": payload}


def serialize_world_model_increment_evidence(
    evidence: ForecastIncrementEvidence,
) -> dict:
    pair = evidence.pair

    def optional_decimal(value: Decimal | None) -> str | None:
        return None if value is None else str(value)

    return {
        "world_model_increment_evidence": {
            "status": evidence.status.value,
            "candidate_producer_id": evidence.candidate_producer_id,
            "comparator_producer_id": evidence.comparator_producer_id,
            "candidate_behavior_id": evidence.candidate_behavior_id,
            "due_panel_count": evidence.due_panel_count,
            "forecast_panel_count": evidence.forecast_panel_count,
            "unavailable_panel_count": evidence.unavailable_panel_count,
            "pending_panel_count": evidence.pending_panel_count,
            "settled_panel_count": pair.settled_panel_count,
            "paired_target_count": pair.paired_target_count,
            "non_overlapping_panel_count": pair.non_overlapping_panel_count,
            "candidate_better_panel_count": pair.candidate_better_panel_count,
            "equal_panel_count": pair.equal_panel_count,
            "candidate_worse_panel_count": pair.candidate_worse_panel_count,
            "mean_ranked_probability_improvement": optional_decimal(
                pair.mean_ranked_probability_improvement
            ),
            "ranked_probability_improvement_lower_bound": optional_decimal(
                pair.ranked_probability_improvement_lower_bound
            ),
            "ranked_probability_improvement_upper_bound": optional_decimal(
                pair.ranked_probability_improvement_upper_bound
            ),
            "mean_max_bucket_probability_delta": optional_decimal(
                pair.mean_max_bucket_probability_delta
            ),
            "mean_expected_gross_bps_delta": optional_decimal(pair.mean_expected_gross_bps_delta),
        }
    }


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()
