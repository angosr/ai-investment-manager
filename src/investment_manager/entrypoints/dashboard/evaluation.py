"""Read-only projections of forecast and capital evaluation evidence."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from threading import Lock

from sqlalchemy import and_, func, select
from sqlalchemy.engine import Engine

from investment_manager.execution.group.models import ExecutionGroup
from investment_manager.execution.tables import execution_groups
from investment_manager.forecast.context.evaluation import (
    ForecastEvidence,
    ForecastPairEvidence,
    ForecastPairPanelCase,
    ForecastScoringCase,
    ForecastSourceEvidence,
    evaluate_forecast_evidence,
    evaluate_forecast_pair_evidence,
    multiclass_brier_score,
    ordinal_ranked_probability_score,
)
from investment_manager.forecast.context.posterior import (
    quant_context_posterior_behavior_id,
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
from investment_manager.forecast.quant.runtime import (
    load_quant_forecast_artifact,
    quant_forecast_behavior_id,
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
from investment_manager.kernel.identity import stable_id
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


@dataclass(frozen=True, slots=True)
class QuantContextPairEvidence:
    vs_quant: ForecastPairEvidence | None
    vs_context: ForecastPairEvidence | None


class EvaluationDashboardReader:
    """Derive evaluation views from immutable domain facts without writes."""

    def __init__(self, engine: Engine, config: AppConfig) -> None:
        self._engine = engine
        self._config = config
        self._trading_cost_cache_key: tuple[
            int,
            int,
            datetime | None,
            tuple[str, ...],
        ] | None = None
        self._trading_cost_cache: TradingCostEvidence | None = None
        self._trading_cost_cache_lock = Lock()

    def forecast_evidence(self, *, now: datetime) -> ForecastEvidence | None:
        now = require_utc(now)
        policy = self._config.capital.context_forecast
        if policy is None or not policy.enabled:
            return None
        with self._engine.connect() as connection:
            return self._forecast_evidence(
                connection,
                now=now,
                producer_id=policy.producer_id,
                producer_behavior_id=policy.producer_behavior_id,
            )

    def quant_forecast_evidence(self, *, now: datetime) -> ForecastEvidence | None:
        """Read the research-only Program producer on the same source-independent slots."""

        now = require_utc(now)
        policy = self._config.outcome_evaluation.quant_baseline
        context = self._config.capital.context_forecast
        if policy is None or not policy.enabled or context is None or not context.enabled:
            return None
        with self._engine.connect() as connection:
            contracts = self._active_forecast_contracts(connection)
            if not contracts:
                return None
            behavior_id = self._quant_behavior_id(contracts)
            return self._forecast_evidence(
                connection,
                now=now,
                producer_id=policy.producer_id,
                producer_behavior_id=behavior_id,
            )

    def quant_context_posterior_evidence(
        self,
        *,
        now: datetime,
    ) -> ForecastEvidence | None:
        """Read the research-only AI posterior on the same Quant-backed slots."""

        now = require_utc(now)
        policy = self._config.outcome_evaluation.quant_context_posterior
        context = self._config.capital.context_forecast
        if policy is None or not policy.enabled or context is None or not context.enabled:
            return None
        with self._engine.connect() as connection:
            contracts = self._active_forecast_contracts(connection)
            if not contracts:
                return None
            behavior_id = quant_context_posterior_behavior_id(
                config=self._config,
                contracts=tuple(
                    sorted(contracts, key=lambda item: item.outcome_family_id)
                ),
                quant_producer_behavior_id=self._quant_behavior_id(contracts),
            )
            return self._forecast_evidence(
                connection,
                now=now,
                producer_id=policy.producer_id,
                producer_behavior_id=behavior_id,
            )

    def quant_context_pair_evidence(self) -> QuantContextPairEvidence | None:
        """Compare the posterior only on shared settled DecisionSlots."""

        posterior = self._config.outcome_evaluation.quant_context_posterior
        quant = self._config.outcome_evaluation.quant_baseline
        context = self._config.capital.context_forecast
        if (
            posterior is None
            or not posterior.enabled
            or quant is None
            or not quant.enabled
            or context is None
            or not context.enabled
        ):
            return None
        with self._engine.connect() as connection:
            contracts = self._active_forecast_contracts(connection)
            if not contracts:
                return None
            quant_behavior_id = self._quant_behavior_id(contracts)
            posterior_behavior_id = quant_context_posterior_behavior_id(
                config=self._config,
                contracts=tuple(sorted(contracts, key=lambda item: item.outcome_family_id)),
                quant_producer_behavior_id=quant_behavior_id,
            )
            return QuantContextPairEvidence(
                vs_quant=self._forecast_pair_evidence(
                    connection,
                    contracts=contracts,
                    candidate_producer_id=posterior.producer_id,
                    candidate_behavior_id=posterior_behavior_id,
                    comparator_producer_id=quant.producer_id,
                    comparator_behavior_id=quant_behavior_id,
                ),
                vs_context=self._forecast_pair_evidence(
                    connection,
                    contracts=contracts,
                    candidate_producer_id=posterior.producer_id,
                    candidate_behavior_id=posterior_behavior_id,
                    comparator_producer_id=context.producer_id,
                    comparator_behavior_id=context.producer_behavior_id,
                ),
            )

    def _quant_behavior_id(self, contracts: tuple[ForecastContract, ...]) -> str:
        policy = self._config.outcome_evaluation.quant_baseline
        if policy is None or not policy.enabled:
            raise ValueError("Quant Forecast evidence 缺少启用政策")
        artifact_policy_by_family = {
            item.outcome_family_id: item for item in policy.artifacts
        }
        artifacts = {
            family: load_quant_forecast_artifact(
                Path(item.relative_path),
                expected_artifact_id=item.artifact_id,
            )
            for family, item in artifact_policy_by_family.items()
        }
        return quant_forecast_behavior_id(
            policy_version=policy.version,
            producer_id=policy.producer_id,
            targets=tuple(
                (
                    contract,
                    artifacts.get(contract.outcome_family_id),
                )
                for contract in sorted(
                    contracts,
                    key=lambda item: item.outcome_family_id,
                )
            ),
        )

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

    def _active_forecast_contracts(self, connection) -> tuple[ForecastContract, ...]:
        policy = self._config.capital.context_forecast
        if policy is None or not policy.enabled:
            return ()
        target_versions = {item.outcome_family_id: item.contract_version for item in policy.targets}
        contract_rows = connection.execute(
            select(forecast_contracts.c.contract_id, forecast_contracts.c.payload).where(
                forecast_contracts.c.outcome_family_id.in_(tuple(target_versions))
            )
        ).all()
        return tuple(
            ForecastContract.model_validate(row.payload)
            for row in contract_rows
            if target_versions.get(row.payload["outcome_family_id"])
            == row.payload["contract_version"]
        )

    def _forecast_evidence(
        self,
        connection,
        *,
        now: datetime,
        producer_id: str,
        producer_behavior_id: str,
    ) -> ForecastEvidence | None:
        contracts = self._active_forecast_contracts(connection)
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
                forecast_slot_obligations.c.producer_id == producer_id,
                forecast_slot_obligations.c.producer_behavior_id == producer_behavior_id,
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
                    forecast_records.c.producer_id == producer_id,
                    forecast_records.c.producer_behavior_id == producer_behavior_id,
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
                    forecast_no_estimates.c.producer_id == producer_id,
                    forecast_no_estimates.c.producer_behavior_id == producer_behavior_id,
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
                forecast_records.c.producer_id == producer_id,
                forecast_records.c.producer_behavior_id == producer_behavior_id,
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
                ),
            )
            for stratum in ForecastSlotStratum
        )
        overall = evaluate_forecast_evidence(
            tuple(cases),
            due_slot_count=due_slot_count,
            forecast_count=forecast_count,
            no_estimate_count=no_estimate_count,
        )
        return replace(overall, source_evidence=source_evidence)

    def _forecast_pair_evidence(
        self,
        connection,
        *,
        contracts: tuple[ForecastContract, ...],
        candidate_producer_id: str,
        candidate_behavior_id: str,
        comparator_producer_id: str,
        comparator_behavior_id: str,
    ) -> ForecastPairEvidence | None:
        candidate = forecast_records.alias("pair_candidate_forecast")
        comparator = forecast_records.alias("pair_comparator_forecast")
        contract_ids = tuple(item.contract_id for item in contracts)
        rows = connection.execute(
            select(
                candidate.c.payload,
                comparator.c.payload,
                forecast_outcomes.c.payload,
                forecast_decision_slots.c.payload,
            )
            .select_from(
                candidate.join(
                    comparator,
                    and_(
                        comparator.c.decision_slot_id == candidate.c.decision_slot_id,
                        comparator.c.contract_id == candidate.c.contract_id,
                    ),
                )
                .join(
                    forecast_outcomes,
                    and_(
                        forecast_outcomes.c.decision_slot_id
                        == candidate.c.decision_slot_id,
                        forecast_outcomes.c.evaluation_version
                        == self._config.outcome_evaluation.target_forecast_version,
                    ),
                )
                .join(
                    forecast_decision_slots,
                    forecast_decision_slots.c.slot_id == candidate.c.decision_slot_id,
                )
            )
            .where(
                candidate.c.contract_id.in_(contract_ids),
                candidate.c.kind == ForecastResultKind.BASE.value,
                candidate.c.producer_id == candidate_producer_id,
                candidate.c.producer_behavior_id == candidate_behavior_id,
                comparator.c.kind == ForecastResultKind.BASE.value,
                comparator.c.producer_id == comparator_producer_id,
                comparator.c.producer_behavior_id == comparator_behavior_id,
                forecast_outcomes.c.status == ForecastOutcomeStatus.SETTLED.value,
            )
            .order_by(
                forecast_decision_slots.c.information_cutoff_at,
                candidate.c.decision_slot_id,
            )
        ).all()
        grouped: dict[
            tuple[datetime, datetime, ForecastSlotStratum],
            list[tuple[Decimal, Decimal, Decimal, Decimal]],
        ] = {}
        for candidate_raw, comparator_raw, outcome_raw, slot_raw in rows:
            candidate_forecast = BaseForecast.model_validate(candidate_raw)
            comparator_forecast = BaseForecast.model_validate(comparator_raw)
            outcome = ForecastOutcome.model_validate(outcome_raw)
            slot = ForecastDecisionSlot.model_validate(slot_raw)
            if (
                candidate_forecast.decision_slot_id != comparator_forecast.decision_slot_id
                or candidate_forecast.contract_id != comparator_forecast.contract_id
                or candidate_forecast.outcome_family_id != comparator_forecast.outcome_family_id
                or outcome.realized_bucket_id is None
            ):
                raise ValueError("Forecast 配对读取到不一致的 Slot/Contract/Outcome")
            candidate_probabilities = tuple(
                (item.bucket_id, item.probability)
                for item in candidate_forecast.outcome_probabilities
            )
            comparator_probabilities = tuple(
                (item.bucket_id, item.probability)
                for item in comparator_forecast.outcome_probabilities
            )
            if tuple(item[0] for item in candidate_probabilities) != tuple(
                item[0] for item in comparator_probabilities
            ):
                raise ValueError("Forecast 配对概率桶不一致")
            maximum_probability_delta = max(
                abs(candidate_item[1] - comparator_item[1])
                for candidate_item, comparator_item in zip(
                    candidate_probabilities,
                    comparator_probabilities,
                    strict=True,
                )
            )
            key = (slot.information_cutoff_at, outcome.evaluation_at, slot.stratum)
            grouped.setdefault(key, []).append(
                (
                    ordinal_ranked_probability_score(
                        candidate_probabilities,
                        outcome.realized_bucket_id,
                    ),
                    ordinal_ranked_probability_score(
                        comparator_probabilities,
                        outcome.realized_bucket_id,
                    ),
                    multiclass_brier_score(
                        candidate_probabilities,
                        outcome.realized_bucket_id,
                    ),
                    multiclass_brier_score(
                        comparator_probabilities,
                        outcome.realized_bucket_id,
                    ),
                    maximum_probability_delta,
                    candidate_forecast.expected_gross_bps
                    - comparator_forecast.expected_gross_bps,
                )
            )
        if not grouped:
            return None
        panels = []
        for (cutoff, evaluation_at, stratum), values in sorted(grouped.items()):
            count = Decimal(len(values))
            panels.append(
                ForecastPairPanelCase(
                    panel_id=stable_id(
                        "forecast_pair_panel",
                        candidate_behavior_id,
                        comparator_behavior_id,
                        cutoff.isoformat(),
                        evaluation_at.isoformat(),
                        stratum.value,
                    ),
                    information_cutoff_at=cutoff,
                    evaluation_at=evaluation_at,
                    source_stratum=stratum,
                    paired_target_count=len(values),
                    candidate_ranked_probability_score=sum(
                        (item[0] for item in values), Decimal("0")
                    )
                    / count,
                    comparator_ranked_probability_score=sum(
                        (item[1] for item in values), Decimal("0")
                    )
                    / count,
                    candidate_brier_score=sum(
                        (item[2] for item in values), Decimal("0")
                    )
                    / count,
                    comparator_brier_score=sum(
                        (item[3] for item in values), Decimal("0")
                    )
                    / count,
                    mean_max_bucket_probability_delta=sum(
                        (item[4] for item in values), Decimal("0")
                    )
                    / count,
                    mean_expected_gross_bps_delta=sum(
                        (item[5] for item in values), Decimal("0")
                    )
                    / count,
                )
            )
        return evaluate_forecast_pair_evidence(tuple(panels))

    @staticmethod
    def _forecast_market_state_key(forecast: BaseForecast) -> str | None:
        """Read the point-in-time regime frozen in this Forecast, never current state."""

        if forecast.analysis_input_json is None:
            if forecast.program_input_json is None:
                return None
            try:
                return _quant_panel_cell_key(json.loads(forecast.program_input_json))
            except (json.JSONDecodeError, TypeError):
                return None
        try:
            payload = json.loads(forecast.analysis_input_json)
            target_states = payload.get("forecast_targets")
            if target_states is not None:
                matching_target = next(
                    (
                        item
                        for item in target_states
                        if item.get("decision_slot", {}).get("decision_slot_id")
                        == forecast.decision_slot_id
                    ),
                    None,
                )
                if matching_target is None:
                    return None
                quant_cell = _quant_panel_cell_key(matching_target.get("quant_panel"))
                if quant_cell is not None:
                    return quant_cell
                assets = matching_target.get("target_state", {}).get("asset_states", ())
            else:
                assets = payload["target_state"]["asset_states"]
            regime = assets[0]["regime"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
            return None
        return regime if isinstance(regime, str) and regime else None


def _quant_panel_cell_key(raw: object) -> str | None:
    """Resolve the selected model's frozen cell from the real Quant panel schema."""

    if not isinstance(raw, dict):
        return None
    prior = raw.get("quant_prior")
    candidates = raw.get("candidate_predictions")
    if not isinstance(prior, dict) or not isinstance(candidates, list):
        return None
    selected_model = prior.get("model_name")
    if not isinstance(selected_model, str) or not selected_model:
        return None
    matches = tuple(
        item.get("cell_key")
        for item in candidates
        if isinstance(item, dict) and item.get("model_name") == selected_model
    )
    if len(matches) != 1:
        return None
    cell_key = matches[0]
    return cell_key if isinstance(cell_key, str) and cell_key else None


def serialize_forecast_evidence(evidence: ForecastEvidence | None) -> dict:
    if evidence is None:
        return {"forecast_evidence": None}
    return {"forecast_evidence": _serialize_forecast_evidence_payload(evidence)}


def serialize_quant_forecast_evidence(evidence: ForecastEvidence | None) -> dict:
    return {
        "quant_forecast_evidence": (
            None if evidence is None else _serialize_forecast_evidence_payload(evidence)
        )
    }


def serialize_quant_context_posterior_evidence(
    evidence: ForecastEvidence | None,
) -> dict:
    return {
        "quant_context_posterior_evidence": (
            None if evidence is None else _serialize_forecast_evidence_payload(evidence)
        )
    }


def serialize_quant_context_pair_evidence(
    evidence: QuantContextPairEvidence | None,
) -> dict:
    def serialize_pair(value: ForecastPairEvidence | None) -> dict | None:
        if value is None:
            return None
        payload = asdict(value)
        for field_name in (
            "mean_candidate_ranked_probability_score",
            "mean_comparator_ranked_probability_score",
            "mean_ranked_probability_improvement",
            "ranked_probability_improvement_lower_bound",
            "ranked_probability_improvement_upper_bound",
            "mean_candidate_brier_score",
            "mean_comparator_brier_score",
            "mean_brier_improvement",
            "brier_improvement_lower_bound",
            "brier_improvement_upper_bound",
            "mean_max_bucket_probability_delta",
            "mean_expected_gross_bps_delta",
        ):
            field_value = getattr(value, field_name)
            payload[field_name] = None if field_value is None else str(field_value)
        return payload

    return {
        "quant_context_pair_evidence": (
            None
            if evidence is None
            else {
                "vs_quant": serialize_pair(evidence.vs_quant),
                "vs_context": serialize_pair(evidence.vs_context),
            }
        )
    }


def serialize_forecast_stability_evidence(
    evidence: PortfolioForecastStabilityReport | None,
) -> dict:
    return {
        "forecast_stability_evidence": (
            None
            if evidence is None
            else {
                "assignment_count": evidence.assignment_count,
                "successful_replica_count": evidence.successful_replica_count,
                "replayable_case_count": evidence.replayable_case_count,
                "missing_capital_target_count": evidence.missing_capital_target_count,
                "cash_flip_count": evidence.cash_flip_count,
                "expression_flip_count": evidence.expression_flip_count,
                "target_change_count": evidence.target_change_count,
                "maximum_allocation_fraction_delta": (
                    None
                    if evidence.maximum_allocation_fraction_delta is None
                    else str(evidence.maximum_allocation_fraction_delta)
                ),
            }
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
            "mean_ranked_probability_improvement": (
                None
                if report.mean_ranked_probability_improvement is None
                else str(report.mean_ranked_probability_improvement)
            ),
            "conservative_mean_ranked_probability_improvement": (
                None
                if report.conservative_mean_ranked_probability_improvement is None
                else str(report.conservative_mean_ranked_probability_improvement)
            ),
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
        "mean_ranked_probability_score",
        "benchmark_mean_ranked_probability_score",
        "ranked_probability_skill",
        "rolling_benchmark_mean_ranked_probability_score",
        "rolling_ranked_probability_skill",
        "rolling_ranked_probability_skill_lower_bound",
        "rolling_ranked_probability_skill_upper_bound",
        "market_benchmark_mean_ranked_probability_score",
        "market_ranked_probability_skill",
        "market_ranked_probability_skill_lower_bound",
        "market_ranked_probability_skill_upper_bound",
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
        "mean_absolute_return_error_bps",
        "expected_realized_return_correlation",
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
