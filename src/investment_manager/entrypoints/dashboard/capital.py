"""Read-only projection of the active product-capital ledger for the dashboard."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import and_, func, select
from sqlalchemy.engine import Engine

from investment_manager.entrypoints.dashboard.pagination import PageCursor, older_than
from investment_manager.execution.group.models import ExecutionGroup, ExecutionLegRole
from investment_manager.execution.models import Side
from investment_manager.execution.planning.planner import TradePlan
from investment_manager.execution.tables import (
    execution_groups,
    product_order_observations,
    trade_plans,
)
from investment_manager.forecast.context.evaluation import (
    ForecastEvidence,
    ForecastScoringCase,
    ForecastSourceEvidence,
    evaluate_forecast_evidence,
)
from investment_manager.forecast.contracts import (
    ForecastContract,
    ForecastDecisionSlot,
    ForecastSlotStratum,
)
from investment_manager.forecast.product.evaluation import (
    ProductPayoffEvidence,
    evaluate_product_payoff_evidence,
)
from investment_manager.forecast.product.models import ProductPayoffProjection
from investment_manager.forecast.product.repository import (
    SqlProductPayoffProjectionStore,
)
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
    product_payoff_projections,
)
from investment_manager.forecast.tables import forecasts as forecast_records
from investment_manager.governance.evaluation.world_model_ablation import (
    SqlWorldModelAblationRepository,
    WorldModelAblationReport,
)
from investment_manager.kernel.time import require_utc
from investment_manager.market.features import freeze_quote_views
from investment_manager.market.models import (
    InstrumentId,
    InstrumentProduct,
    ValuationQuoteQuality,
)
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.portfolio.evaluation import (
    CapitalChoiceCase,
    CapitalChoiceEvidence,
    evaluate_capital_choice,
    is_full_forecast_capital_choice,
)
from investment_manager.portfolio.models import (
    CapitalCycleOutcome,
    CapitalCycleRecord,
    PortfolioAccountSnapshot,
    PortfolioPerformanceInterval,
    PortfolioTarget,
)
from investment_manager.portfolio.policy import EconomicExposure, MandateStatus
from investment_manager.portfolio.repository import load_portfolio_target
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
    policy: CapitalPolicyStatus | None = None
    account: PortfolioAccountSnapshot | None = None
    cycle_record: CapitalCycleRecord | None = None
    target: PortfolioTarget | None = None
    risk: PortfolioRiskDecision | None = None
    active_groups: tuple[ExecutionGroup, ...] = ()
    total_order_count: int = 0
    performance_interval_count: int = 0
    cumulative_net_pnl: Decimal = Decimal("0")
    latest_performance: PortfolioPerformanceInterval | None = None
    instruments: tuple[CapitalInstrumentStatus, ...] = ()


@dataclass(frozen=True, slots=True)
class CapitalInstrumentStatus:
    """Dashboard projection joining the investable universe, ledger, and latest quote."""

    instrument: InstrumentId
    quantity: Decimal | None
    average_price: Decimal | None
    bid: Decimal | None
    ask: Decimal | None
    quote_observed_at: datetime | None
    quote_quality: ValuationQuoteQuality | None


@dataclass(frozen=True, slots=True)
class CapitalPolicyStatus:
    mandate_version: str
    mandate_status: MandateStatus
    objective: str
    horizon_years: int
    base_currency: str
    universe_version: str
    covered_exposures: tuple[EconomicExposure, ...]
    reference_policy_version: str | None


@dataclass(frozen=True, slots=True)
class CapitalCandidateEconomics:
    candidate_id: str
    forecast_id: str
    payoff_projection_id: str | None
    producer_id: str
    outcome_family_id: str
    target_legs: tuple[tuple[str, str, str, str], ...]
    edge_basis: str
    forecast_current: bool
    information_cutoff_at: datetime
    available_at: datetime
    valid_until: datetime
    world_model_id: str | None
    outcome_probabilities: tuple[tuple[str, Decimal], ...]
    mechanism_contributions: tuple[tuple[str, str, str], ...]
    evidence_refs: tuple[str, ...]
    gross_bps: Decimal
    fee_bps: Decimal
    exit_spread_bps: Decimal
    depth_slippage_bps: Decimal
    estimated_cost_bps: Decimal
    net_bps: Decimal
    decision_threshold_bps: Decimal
    current_gross_notional: Decimal
    evaluation_gross_notional: Decimal
    desired_gross_notional: Decimal
    eligible: bool
    reason_codes: tuple[str, ...]
    validity_reason_codes: tuple[str, ...] | None
    validity_evidence_refs: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class CapitalPositionChange:
    instrument: InstrumentId
    side: Side
    effect: str
    role: ExecutionLegRole
    status: str
    requested_quantity: Decimal
    filled_quantity: Decimal
    average_fill_price: Decimal | None
    fee: Decimal


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
    position_changes: tuple[CapitalPositionChange, ...] = ()
    candidate_economics_recorded: bool = False
    candidate_economics: tuple[CapitalCandidateEconomics, ...] = ()
    analysis_input: dict | None = None


@dataclass(frozen=True, slots=True)
class CapitalEquityPoint:
    snapshot_id: str
    at: datetime
    revision: int
    equity: Decimal
    net_pnl: Decimal | None
    drawdown_fraction: Decimal
    cash_benchmark_equity: Decimal | None
    increment_vs_cash: Decimal | None


class CapitalDashboardReader:
    """Load a compact current-state view without inventing a second ledger."""

    def __init__(self, engine: Engine, config: AppConfig) -> None:
        self._engine = engine
        self._config = config

    def overview(self, *, now: datetime | None = None) -> CapitalOverview:
        if not self._config.capital.enabled:
            return CapitalOverview(enabled=False)
        now = datetime.now(UTC) if now is None else require_utc(now)
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
                where_clause=(
                    capital_cycle_records.c.pipeline_id == self._config.capital.version
                ),
            )
            target = self._latest_payload(
                connection,
                portfolio_targets.c.payload,
                portfolio_targets.c.as_of,
                portfolio_targets.c.target_id,
                PortfolioTarget,
                loader=load_portfolio_target,
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
                connection.scalar(
                    select(
                        func.count(
                            func.distinct(product_order_observations.c.client_order_id)
                        )
                    ).select_from(product_order_observations)
                )
                or 0
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
        return CapitalOverview(
            enabled=True,
            policy=self._policy_status(),
            account=account,
            cycle_record=cycle_record,
            target=target,
            risk=risk,
            active_groups=active,
            total_order_count=order_count,
            performance_interval_count=performance_count,
            cumulative_net_pnl=cumulative_net_pnl,
            latest_performance=latest_performance,
            instruments=self._instrument_statuses(account=account, as_of=now),
        )

    def _instrument_statuses(
        self,
        *,
        account: PortfolioAccountSnapshot | None,
        as_of: datetime,
    ) -> tuple[CapitalInstrumentStatus, ...]:
        """Keep zero holdings visible without polluting the non-zero position ledger."""

        store = SqlMarketDataStore(self._engine)
        schedule = store.latest_trading_schedule(as_of=as_of)
        position_by_key = {
            item.instrument.key: item for item in (() if account is None else account.positions)
        }
        rows: list[CapitalInstrumentStatus] = []
        specs_by_symbol: dict[str, list] = {}
        for spec in self._config.capital.execution_specs:
            specs_by_symbol.setdefault(spec.instrument.symbol, []).append(spec)
        for symbol in sorted(specs_by_symbol):
            specs = specs_by_symbol[symbol]
            positions = tuple(
                position_by_key[item.instrument.key]
                for item in specs
                if item.instrument.key in position_by_key
            )
            position = max(positions, key=lambda item: abs(item.quantity), default=None)
            spec = next(
                (
                    item
                    for item in specs
                    if position is not None
                    and item.instrument.key == position.instrument.key
                ),
                next(
                    (
                        item
                        for item in specs
                        if item.instrument.product == InstrumentProduct.SPOT
                    ),
                    specs[0],
                ),
            )
            instrument = spec.instrument
            if instrument.product == InstrumentProduct.SPOT:
                observed = store.latest_spot_quote(
                    instrument=instrument,
                    evaluation_at=as_of,
                    visible_at=as_of,
                )
            else:
                observed = store.latest_perpetual_quote(
                    instrument=instrument,
                    evaluation_at=as_of,
                    visible_at=as_of,
                )
            valuation = None
            if observed is not None:
                valuation, _ = freeze_quote_views(
                    instrument=instrument,
                    quote=observed,
                    as_of=as_of,
                    maximum_live_age_seconds=self._config.capital.risk.maximum_quote_age_seconds,
                    trading_schedule=(
                        schedule
                        if instrument.product == InstrumentProduct.TRADFI_PERPETUAL
                        else None
                    ),
                )
            rows.append(
                CapitalInstrumentStatus(
                    instrument=instrument,
                    quantity=(
                        None
                        if account is None
                        else Decimal("0")
                        if position is None
                        else position.quantity
                    ),
                    average_price=None if position is None else position.average_price,
                    bid=None if valuation is None else valuation.bid,
                    ask=None if valuation is None else valuation.ask,
                    quote_observed_at=(
                        None if valuation is None else valuation.observed_at
                    ),
                    quote_quality=None if valuation is None else valuation.quality,
                )
            )
        return tuple(rows)

    def _policy_status(self) -> CapitalPolicyStatus:
        capital = self._config.capital
        covered = {
            EconomicExposure.CASH,
            *(item.economic_exposure for item in capital.investable_universe.instruments),
        }
        return CapitalPolicyStatus(
            mandate_version=capital.mandate.version,
            mandate_status=capital.mandate.status,
            objective=capital.mandate.objective,
            horizon_years=capital.mandate.horizon_years,
            base_currency=capital.mandate.base_currency,
            universe_version=capital.investable_universe.version,
            covered_exposures=tuple(sorted(covered)),
            reference_policy_version=(
                None
                if capital.reference_policy is None
                else capital.reference_policy.version
            ),
        )

    def forecast_evidence(self, *, now: datetime) -> ForecastEvidence | None:
        now = require_utc(now)
        with self._engine.connect() as connection:
            return self._forecast_evidence(connection, now=now)

    def world_model_ablation_evidence(
        self, *, now: datetime
    ) -> WorldModelAblationReport | None:
        """Read the active prospective comparison without registering or mutating it."""

        now = require_utc(now)
        policy = self._config.outcome_evaluation.world_model_ablation
        context = self._config.capital.context_forecast
        if (
            policy is None
            or not policy.enabled
            or context is None
            or not context.enabled
        ):
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
        cases = SqlProductPayoffProjectionStore(self._engine).outcome_cases(
            product_outcome_version=evaluation.product_payoff_version,
            forecast_outcome_version=evaluation.target_forecast_version,
            producer_behavior_id=context.producer_behavior_id,
        )
        return evaluate_product_payoff_evidence(
            cases,
            product_outcome_version=evaluation.product_payoff_version,
            forecast_outcome_version=evaluation.target_forecast_version,
            required_independent_source_forecasts=(
                evaluation.product_payoff_minimum_sample_size
            ),
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
                    portfolio_targets.c.portfolio_id
                    == self._config.capital.decision.portfolio_id
                )
                .order_by(portfolio_targets.c.as_of.desc(), portfolio_targets.c.target_id.desc())
                .execution_options(stream_results=True)
            )
            for row in rows:
                target = PortfolioTarget.model_validate(row[0])
                receipt = CapitalCycleRecord.model_validate(row[1])
                if not is_full_forecast_capital_choice(receipt):
                    continue
                candidates = target.candidate_evaluations
                if not candidates or any(
                    item.payoff_projection_id is None for item in candidates
                ):
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
                return evaluate_capital_choice(tuple(cases))
        return None

    def _forecast_evidence(self, connection, *, now: datetime) -> ForecastEvidence | None:
        policy = self._config.capital.context_forecast
        if policy is None or not policy.enabled:
            return None
        target_versions = {
            item.outcome_family_id: item.contract_version for item in policy.targets
        }
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
                    forecast_decision_slots.c.slot_id
                    == forecast_slot_obligations.c.slot_id,
                )
            )
            .where(
                forecast_decision_slots.c.contract_id.in_(contract_ids),
                forecast_slot_obligations.c.producer_id == policy.producer_id,
                forecast_slot_obligations.c.producer_behavior_id
                == policy.producer_behavior_id,
                forecast_decision_slots.c.completion_deadline_at <= now,
            )
        ).all()
        due_slots = {
            row.slot_id: ForecastDecisionSlot.model_validate(row.payload)
            for row in due_rows
        }
        forecast_slot_ids = tuple(
            connection.scalars(
                select(forecast_records.c.decision_slot_id)
                .join(
                    forecast_decision_slots,
                    forecast_decision_slots.c.slot_id
                    == forecast_records.c.decision_slot_id,
                )
                .where(
                    forecast_records.c.contract_id.in_(contract_ids),
                    forecast_records.c.kind == ForecastResultKind.BASE.value,
                    forecast_records.c.producer_id == policy.producer_id,
                    forecast_records.c.producer_behavior_id
                    == policy.producer_behavior_id,
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
                    forecast_no_estimates.c.producer_behavior_id
                    == policy.producer_behavior_id,
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
                        forecast_outcomes.c.decision_slot_id
                        == forecast_records.c.decision_slot_id,
                        forecast_outcomes.c.evaluation_version
                        == self._config.outcome_evaluation.target_forecast_version,
                    ),
                ).join(
                    forecast_decision_slots,
                    forecast_decision_slots.c.slot_id
                    == forecast_records.c.decision_slot_id,
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
                (item.bucket_id, item.probability)
                for item in contract.forecast_benchmark
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
                    due_slot_count=sum(
                        slot.stratum == stratum for slot in due_slots.values()
                    ),
                    forecast_count=sum(
                        due_slots[slot_id].stratum == stratum
                        for slot_id in forecast_slot_ids
                    ),
                    no_estimate_count=sum(
                        due_slots[slot_id].stratum == stratum
                        for slot_id in no_estimate_slot_ids
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
                self._config.outcome_evaluation.target_forecast_minimum_sample_size
                * len(contracts)
            ),
            permission_evidence_eligible=False,
        )
        return replace(overall, source_evidence=source_evidence)

    def equity_history(
        self,
        *,
        cursor: PageCursor | None = None,
        limit: int = 100,
    ) -> tuple[CapitalEquityPoint, ...]:
        """Project immutable account snapshots without reconstructing equity."""

        if limit < 1 or limit > 101:
            raise ValueError("Capital equity internal limit 必须在 1..101")
        portfolio_id = self._config.capital.decision.portfolio_id
        statement = select(portfolio_account_snapshots.c.payload).where(
            portfolio_account_snapshots.c.portfolio_id == portfolio_id
        )
        if cursor is not None:
            statement = statement.where(
                older_than(
                    portfolio_account_snapshots.c.as_of,
                    portfolio_account_snapshots.c.snapshot_id,
                    cursor,
                )
            )
        with self._engine.connect() as connection:
            baseline_payload = connection.execute(
                select(portfolio_account_snapshots.c.payload)
                .where(portfolio_account_snapshots.c.portfolio_id == portfolio_id)
                .order_by(
                    portfolio_account_snapshots.c.revision,
                    portfolio_account_snapshots.c.as_of,
                    portfolio_account_snapshots.c.snapshot_id,
                )
                .limit(1)
            ).scalar_one_or_none()
            baseline = (
                None
                if baseline_payload is None
                else PortfolioAccountSnapshot.model_validate(baseline_payload)
            )
            rows = connection.execute(
                statement.order_by(
                    portfolio_account_snapshots.c.as_of.desc(),
                    portfolio_account_snapshots.c.snapshot_id.desc(),
                ).limit(limit)
            )
            snapshots = tuple(PortfolioAccountSnapshot.model_validate(row.payload) for row in rows)
        return tuple(
            CapitalEquityPoint(
                snapshot_id=account.snapshot_id,
                at=account.as_of,
                revision=account.revision,
                equity=account.equity,
                net_pnl=(
                    account.accounting.net_pnl
                    if account.accounting is not None
                    else None
                ),
                drawdown_fraction=account.drawdown_fraction,
                cash_benchmark_equity=(
                    baseline.equity if baseline is not None else None
                ),
                increment_vs_cash=(
                    account.equity - baseline.equity
                    if baseline is not None
                    else None
                ),
            )
            for account in snapshots
        )

    @staticmethod
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
                    item.target_id: load_portfolio_target(item.payload)
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
            projection_ids = tuple(
                sorted(
                    {
                        evaluation.payoff_projection_id
                        for target in targets.values()
                        if target.candidate_evaluations is not None
                        for evaluation in target.candidate_evaluations
                        if evaluation.payoff_projection_id is not None
                    }
                )
            )
            projections = (
                {
                    item.projection_id: ProductPayoffProjection.model_validate(
                        item.payload
                    )
                    for item in connection.execute(
                        select(
                            product_payoff_projections.c.projection_id,
                            product_payoff_projections.c.payload,
                        ).where(
                            product_payoff_projections.c.projection_id.in_(
                                projection_ids
                            )
                        )
                    )
                }
                if projection_ids
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
                            func.count(
                                func.distinct(product_order_observations.c.client_order_id)
                            ).label("order_count"),
                        )
                        .select_from(
                            execution_groups.outerjoin(
                                product_order_observations,
                                product_order_observations.c.group_id
                                == execution_groups.c.group_id,
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
                projections=projections,
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
        projections: dict[str, ProductPayoffProjection],
    ) -> CapitalActivity:
        candidate_economics = self._candidate_economics(
            target=target,
            forecasts=forecasts,
            projections=projections,
        )
        candidate_economics_recorded = (
            target is not None and target.candidate_evaluations is not None
        )
        analysis_input = self._decision_analysis_input(
            target=target,
            forecasts=forecasts,
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
            position_changes = self._position_changes(plan=plan, groups=groups)
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
                position_changes=position_changes,
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
                analysis_input=analysis_input,
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
                analysis_input=analysis_input,
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
                analysis_input=analysis_input,
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
                analysis_input=analysis_input,
            )
        groups = tuple(groups_by_plan.get(plan.plan_id, ()))
        order_count = order_counts.get(plan.plan_id, 0)
        position_changes = self._position_changes(plan=plan, groups=groups)
        if not groups:
            outcome = "NO_ORDER"
            summary = "组合决策完成，无需产生订单"
        elif all(item.terminal for item in groups):
            outcome = "EXECUTED"
            summary = f"仓位调整完成：{len(groups)} 个交易组，{order_count} 笔订单"
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
            position_changes=position_changes,
            candidate_economics_recorded=candidate_economics_recorded,
            candidate_economics=candidate_economics,
            analysis_input=analysis_input,
        )

    @staticmethod
    def _position_changes(
        *,
        plan: TradePlan,
        groups: tuple[ExecutionGroup, ...],
    ) -> tuple[CapitalPositionChange, ...]:
        deltas = {item.sleeve_id: item for item in plan.target_deltas}
        planned_legs = {
            item.leg_id: item
            for group in plan.groups
            for item in group.legs
        }
        changes: list[CapitalPositionChange] = []
        for group in sorted(groups, key=lambda item: item.group_id):
            delta = deltas.get(group.sleeve_id)
            for leg in (*group.target_legs, *group.compensation_legs):
                planned = planned_legs.get(leg.planned_leg_id)
                if leg.role == ExecutionLegRole.COMPENSATION:
                    effect = "COMPENSATION"
                else:
                    if planned is None or delta is None:
                        raise ValueError("ExecutionGroup 无法还原权威仓位变动语义")
                    direction = "LONG" if leg.side == Side.BUY else "SHORT"
                    if planned.reduce_only:
                        direction = "LONG" if leg.side == Side.SELL else "SHORT"
                        operation = (
                            "CLOSE"
                            if delta.desired_gross_notional == 0
                            else "REDUCE"
                        )
                    else:
                        operation = (
                            "OPEN"
                            if delta.current_gross_notional == 0
                            else "INCREASE"
                        )
                    effect = f"{operation}_{direction}"
                changes.append(
                    CapitalPositionChange(
                        instrument=leg.instrument,
                        side=leg.side,
                        effect=effect,
                        role=leg.role,
                        status=leg.status.value,
                        requested_quantity=leg.requested_quantity,
                        filled_quantity=leg.filled_quantity,
                        average_fill_price=leg.average_fill_price,
                        fee=leg.fee,
                    )
                )
        return tuple(changes)

    @staticmethod
    def _decision_analysis_input(
        *,
        target: PortfolioTarget | None,
        forecasts: dict[str, BaseForecast | CalibratedForecast],
    ) -> dict | None:
        if target is None or target.candidate_evaluations is None:
            return None
        snapshots = {
            forecast.analysis_input_json
            for evaluation in target.candidate_evaluations
            if isinstance((forecast := forecasts.get(evaluation.forecast_id)), BaseForecast)
            and forecast.analysis_input_json is not None
        }
        if not snapshots:
            return None
        if len(snapshots) != 1:
            raise ValueError("同一资本决策引用了多个不同的 AI 输入快照")
        return json.loads(snapshots.pop())

    def _candidate_economics(
        self,
        *,
        target: PortfolioTarget | None,
        forecasts: dict[str, BaseForecast | CalibratedForecast],
        projections: dict[str, ProductPayoffProjection],
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
            projection = (
                None
                if evaluation.payoff_projection_id is None
                else projections.get(evaluation.payoff_projection_id)
            )
            if evaluation.payoff_projection_id is not None and projection is None:
                raise ValueError("PortfolioTarget candidate 缺少不可变 product projection")
            candidate_target = (
                forecast.target if projection is None else projection.target
            )
            candidates.append(
                CapitalCandidateEconomics(
                    candidate_id=evaluation.sleeve_id,
                    forecast_id=forecast.forecast_id,
                    payoff_projection_id=evaluation.payoff_projection_id,
                    producer_id=forecast.producer_id,
                    outcome_family_id=forecast.outcome_family_id,
                    target_legs=tuple(
                        (
                            leg.instrument.key,
                            leg.instrument.symbol,
                            leg.instrument.product.value,
                            leg.direction.value,
                        )
                        for leg in candidate_target.legs
                    ),
                    edge_basis=evaluation.edge_basis.value,
                    forecast_current=evaluation.forecast_current,
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
                    gross_bps=evaluation.decision_gross_bps,
                    fee_bps=evaluation.cost.fee_bps,
                    exit_spread_bps=evaluation.cost.exit_spread_bps,
                    depth_slippage_bps=evaluation.cost.depth_slippage_bps,
                    estimated_cost_bps=evaluation.cost.total_bps,
                    net_bps=evaluation.decision_net_bps,
                    decision_threshold_bps=evaluation.minimum_net_bps,
                    current_gross_notional=evaluation.current_gross_notional,
                    evaluation_gross_notional=evaluation.evaluation_gross_notional,
                    desired_gross_notional=evaluation.desired_gross_notional,
                    eligible=evaluation.eligible,
                    reason_codes=evaluation.reason_codes,
                    validity_reason_codes=evaluation.validity_reason_codes,
                    validity_evidence_refs=evaluation.validity_evidence_refs,
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
        loader=None,
    ):
        ordering = [time_column.desc()]
        if secondary_order is not None:
            ordering.append(secondary_order.desc())
        ordering.append(id_column.desc())
        statement = select(payload_column)
        if where_clause is not None:
            statement = statement.where(where_clause)
        payload = connection.execute(statement.order_by(*ordering).limit(1)).scalar_one_or_none()
        if payload is None:
            return None
        return loader(payload) if loader is not None else model.model_validate(payload)

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
    policy = overview.policy
    return {
        "enabled": overview.enabled,
        "instruments": [
            {
                "instrument": item.instrument.key,
                "symbol": item.instrument.symbol,
                "product": item.instrument.product.value,
                "quantity": None if item.quantity is None else str(item.quantity),
                "average_price": (
                    None if item.average_price is None else str(item.average_price)
                ),
                "bid": None if item.bid is None else str(item.bid),
                "ask": None if item.ask is None else str(item.ask),
                "price": (
                    None
                    if item.bid is None or item.ask is None
                    else str((item.bid + item.ask) / Decimal("2"))
                ),
                "quote_observed_at": _iso(item.quote_observed_at),
                "quote_quality": (
                    None if item.quote_quality is None else item.quote_quality.value
                ),
            }
            for item in overview.instruments
        ],
        "policy": None
        if policy is None
        else {
            "mandate_version": policy.mandate_version,
            "mandate_status": policy.mandate_status.value,
            "objective": policy.objective,
            "horizon_years": policy.horizon_years,
            "base_currency": policy.base_currency,
            "universe_version": policy.universe_version,
            "covered_exposures": [item.value for item in policy.covered_exposures],
            "reference_policy_version": policy.reference_policy_version,
        },
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
    }


def serialize_forecast_evidence(evidence: ForecastEvidence | None) -> dict:
    if evidence is None:
        return {"forecast_evidence": None}
    return {"forecast_evidence": _serialize_forecast_evidence_payload(evidence)}


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
            "decision_id": evidence.decision_id,
            "decision_at": _iso(evidence.decision_at),
            "evaluation_at": _iso(evidence.evaluation_at),
            "candidate_count": evidence.candidate_count,
            "missed_profitable_exposure_count": (
                evidence.missed_profitable_exposure_count
            ),
            "selected_unprofitable_exposure_count": (
                evidence.selected_unprofitable_exposure_count
            ),
            "exposures": [
                {
                    "economic_exposure_id": item.economic_exposure_id,
                    "selected": candidate(item.selected),
                    "best_realized": candidate(item.best_realized),
                    "opportunity_gap_bps": str(item.opportunity_gap_bps),
                    "missed_profitable_exposure": item.missed_profitable_exposure,
                    "selected_unprofitable_exposure": (
                        item.selected_unprofitable_exposure
                    ),
                }
                for item in evidence.exposures
            ],
        }
    }


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
                "position_changes": [
                    {
                        "instrument": change.instrument.key,
                        "symbol": change.instrument.symbol,
                        "product": change.instrument.product.value,
                        "side": change.side.value,
                        "effect": change.effect,
                        "role": change.role.value,
                        "status": change.status,
                        "requested_quantity": str(change.requested_quantity),
                        "filled_quantity": str(change.filled_quantity),
                        "average_fill_price": (
                            None
                            if change.average_fill_price is None
                            else str(change.average_fill_price)
                        ),
                        "fee": str(change.fee),
                    }
                    for change in item.position_changes
                ],
                "candidate_economics_recorded": item.candidate_economics_recorded,
                "analysis_input": item.analysis_input,
                "candidate_economics": [
                    {
                        "candidate_id": candidate.candidate_id,
                        "forecast_id": candidate.forecast_id,
                        "payoff_projection_id": candidate.payoff_projection_id,
                        "producer_id": candidate.producer_id,
                        "outcome_family_id": candidate.outcome_family_id,
                        "target_legs": [
                            {
                                "instrument": instrument,
                                "symbol": symbol,
                                "product": product,
                                "direction": direction,
                            }
                            for instrument, symbol, product, direction in candidate.target_legs
                        ],
                        "edge_basis": candidate.edge_basis,
                        "forecast_current": candidate.forecast_current,
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
                        "gross_bps": str(candidate.gross_bps),
                        "fee_bps": str(candidate.fee_bps),
                        "exit_spread_bps": str(candidate.exit_spread_bps),
                        "depth_slippage_bps": str(candidate.depth_slippage_bps),
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
                        "validity_reason_codes": (
                            None
                            if candidate.validity_reason_codes is None
                            else list(candidate.validity_reason_codes)
                        ),
                        "validity_evidence_refs": (
                            None
                            if candidate.validity_evidence_refs is None
                            else list(candidate.validity_evidence_refs)
                        ),
                    }
                    for candidate in item.candidate_economics
                ],
            }
            for item in items
        ]
    }


def serialize_capital_equity(items: tuple[CapitalEquityPoint, ...]) -> dict:
    return {
        "points": [
            {
                "snapshot_id": item.snapshot_id,
                "at": _iso(item.at),
                "revision": item.revision,
                "equity": str(item.equity),
                "net_pnl": None if item.net_pnl is None else str(item.net_pnl),
                "drawdown_fraction": str(item.drawdown_fraction),
                "cash_benchmark_equity": (
                    None
                    if item.cash_benchmark_equity is None
                    else str(item.cash_benchmark_equity)
                ),
                "increment_vs_cash": (
                    None if item.increment_vs_cash is None else str(item.increment_vs_cash)
                ),
            }
            for item in items
        ]
    }


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()
