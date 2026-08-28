"""Read-only Dashboard projection of cost-after Forecast-producer paths."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock

from sqlalchemy.engine import Engine

from investment_manager.forecast.context.posterior import (
    quant_context_posterior_behavior_id,
)
from investment_manager.forecast.context.stability import (
    ContextForecastStabilityAssignment,
    ContextForecastStabilityReport,
    ContextForecastStabilityResult,
    SqlContextForecastStabilityRepository,
    evaluate_context_forecast_stability,
)
from investment_manager.forecast.context.targets import assemble_context_capital_targets
from investment_manager.forecast.product.repository import SqlProductPayoffProjectionStore
from investment_manager.forecast.quant.runtime import (
    load_quant_forecast_artifact,
    quant_forecast_behavior_id,
)
from investment_manager.governance.evaluation.logical_account import (
    ProducerPanelLedger,
    SqlProducerPanelReader,
)
from investment_manager.governance.evaluation.producer_capital import (
    ProducerCapitalComparisonEvidence,
    ProducerCapitalReplay,
    ProductPayoffBuilder,
    compare_producer_capital_paths,
)
from investment_manager.governance.evaluation.producer_stability import (
    PortfolioForecastStabilityEvaluator,
    PortfolioForecastStabilityReport,
)
from investment_manager.kernel.time import require_utc
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.settings import AppConfig

ProducerCapitalCacheKey = tuple[tuple[str, int, int, tuple[str, ...]], ...]


@dataclass(frozen=True, slots=True)
class ForecastStabilitySourceEvidence:
    label: str
    role: str
    forecast: ContextForecastStabilityReport
    capital: PortfolioForecastStabilityReport


@dataclass(frozen=True, slots=True)
class ForecastStabilityEvidence:
    sources: tuple[ForecastStabilitySourceEvidence, ...]


class ProducerCapitalDashboardReader:
    """Cache one immutable replay until a producer panel ledger changes."""

    def __init__(self, engine: Engine, config: AppConfig) -> None:
        self._engine = engine
        self._config = config
        self._cache_key: ProducerCapitalCacheKey | None = None
        self._cache: ProducerCapitalComparisonEvidence | None = None
        self._stability_cache: dict[
            str,
            tuple[tuple[object, ...], PortfolioForecastStabilityReport],
        ] = {}
        self._lock = Lock()
        self._market: SqlMarketDataStore | None = None
        self._projectors: dict[str, ProductPayoffBuilder] = {}
        self._behavior_ids: dict[str, str] | None = None
        quant = self._config.outcome_evaluation.quant_baseline
        posterior = self._config.outcome_evaluation.quant_context_posterior
        context = self._config.capital.context_forecast
        if (
            not self._config.capital.enabled
            or quant is None
            or not quant.enabled
            or posterior is None
            or not posterior.enabled
            or context is None
            or not context.enabled
        ):
            return
        self._market = SqlMarketDataStore(self._engine)
        definitions = assemble_context_capital_targets(
            capital=self._config.capital,
            feature=self._config.feature,
            market_policy=self._config.market_data,
            market=self._market,
            product_store=SqlProductPayoffProjectionStore(self._engine),
        )
        contracts = tuple(item.contract for item in definitions)
        artifacts = {
            item.outcome_family_id: load_quant_forecast_artifact(
                Path(item.relative_path),
                expected_artifact_id=item.artifact_id,
            )
            for item in quant.artifacts
        }
        quant_behavior_id = quant_forecast_behavior_id(
            policy_version=quant.version,
            producer_id=quant.producer_id,
            targets=tuple(
                (contract, artifacts.get(contract.outcome_family_id))
                for contract in sorted(contracts, key=lambda item: item.outcome_family_id)
            ),
        )
        self._behavior_ids = {
            "QUANT": quant_behavior_id,
            "CONTEXT_AI": context.producer_behavior_id,
            "AI_QUANT": quant_context_posterior_behavior_id(
                config=self._config,
                contracts=tuple(sorted(contracts, key=lambda item: item.outcome_family_id)),
                quant_producer_behavior_id=quant_behavior_id,
            ),
        }
        self._projectors = {
            item.contract.outcome_family_id: item.product_payoffs
            for item in definitions
            if item.product_payoffs is not None
        }

    def evidence(self, *, now: datetime) -> ProducerCapitalComparisonEvidence | None:
        now = require_utc(now)
        if self._behavior_ids is None or self._market is None:
            return None
        panel_reader = SqlProducerPanelReader(self._engine)
        ledgers = {
            label: panel_reader.read(producer_behavior_id=behavior_id, as_of=now)
            for label, behavior_id in self._behavior_ids.items()
        }
        cache_key: ProducerCapitalCacheKey = tuple(
            (
                label,
                ledger.obligated_panel_count,
                ledger.pending_panel_count,
                tuple(item.panel_id for item in ledger.complete_panels),
            )
            for label, ledger in ledgers.items()
        )
        with self._lock:
            if cache_key == self._cache_key:
                return self._cache
        sources = {
            label: (
                ledgers[label],
                ProducerCapitalReplay(
                    producer_behavior_id=behavior_id,
                    capital_policy=self._config.capital,
                    initial_cash=self._config.shadow.initial_quote_balance,
                    market=self._market,
                    product_payoffs_by_family=self._projectors,
                    sleeve_risk=self._config.capital.sleeve_risk,
                ),
            )
            for label, behavior_id in self._behavior_ids.items()
        }
        result = compare_producer_capital_paths(
            initial_cash=self._config.shadow.initial_quote_balance,
            sources=sources,
        )
        with self._lock:
            if cache_key == self._cache_key:
                return self._cache
            self._cache_key = cache_key
            self._cache = result
            return result

    def forecast_stability_evidence(
        self,
        *,
        now: datetime,
    ) -> ForecastStabilityEvidence | None:
        """Evaluate exact-input replicas before any capital authorization."""

        now = require_utc(now)
        policy = self._config.outcome_evaluation.context_forecast_stability
        context = self._config.capital.context_forecast
        if (
            self._behavior_ids is None
            or self._market is None
            or policy is None
            or not policy.enabled
            or context is None
            or not context.enabled
        ):
            return None
        repository = SqlContextForecastStabilityRepository(self._engine)
        panel_reader = SqlProducerPanelReader(self._engine)
        capital_behaviors = {
            item.producer_behavior_id
            for item in self._config.capital.candidate_capital_authorizations
        }
        evaluator = PortfolioForecastStabilityEvaluator(
            capital_policy=self._config.capital,
            initial_cash=self._config.shadow.initial_quote_balance,
            market=self._market,
            product_payoffs_by_family=self._projectors,
            sleeve_risk=self._config.capital.sleeve_risk,
        )
        sources = []
        for label in ("CONTEXT_AI", "AI_QUANT"):
            behavior_id = self._behavior_ids[label]
            assignments = repository.assignments(
                policy_version=policy.version,
                formal_producer_behavior_id=behavior_id,
            )
            results = repository.results(tuple(item.assignment_id for item in assignments))
            ledger = panel_reader.read(producer_behavior_id=behavior_id, as_of=now)
            forecast_ids = tuple(
                target.formal_forecast_id
                for assignment in assignments
                for target in assignment.targets
            )
            sources.append(
                ForecastStabilitySourceEvidence(
                    label=label,
                    role=(
                        "CAPITAL_CANDIDATE"
                        if behavior_id in capital_behaviors
                        else "RESEARCH"
                    ),
                    forecast=evaluate_context_forecast_stability(
                        policy=policy,
                        formal_producer_behavior_id=behavior_id,
                        assignments=assignments,
                        results=results,
                        formal_forecasts=repository.formal_forecasts(forecast_ids),
                        as_of=now,
                    ),
                    capital=self._stability_capital(
                        behavior_id=behavior_id,
                        evaluator=evaluator,
                        ledger=ledger,
                        assignments=assignments,
                        results=results,
                    ),
                )
            )
        return ForecastStabilityEvidence(sources=tuple(sources))

    def _stability_capital(
        self,
        *,
        behavior_id: str,
        evaluator: PortfolioForecastStabilityEvaluator,
        ledger: ProducerPanelLedger,
        assignments: tuple[ContextForecastStabilityAssignment, ...],
        results: tuple[ContextForecastStabilityResult, ...],
    ) -> PortfolioForecastStabilityReport:
        key = (
            tuple((item.assignment_id, item.source_hash) for item in assignments),
            tuple(
                (
                    item.result_id,
                    item.status.value,
                    item.completed_at.isoformat(),
                    item.output_hash,
                )
                for item in results
            ),
            tuple(item.panel_id for item in ledger.complete_panels),
        )
        with self._lock:
            cached = self._stability_cache.get(behavior_id)
            if cached is not None and cached[0] == key:
                return cached[1]
        report = evaluator.evaluate(
            formal_ledger=ledger,
            assignments=assignments,
            results=results,
        )
        with self._lock:
            self._stability_cache[behavior_id] = (key, report)
        return report


def serialize_producer_capital_evidence(
    evidence: ProducerCapitalComparisonEvidence | None,
) -> dict:
    if evidence is None:
        return {"producer_capital_evidence": None}
    paths = []
    for item in evidence.paths:
        account = item.path.account
        accounting = account.accounting
        paths.append(
            {
                "label": item.label,
                "producer_id": item.producer_id,
                "producer_behavior_id": item.producer_behavior_id,
                "panel_count": len(item.panel_ids),
                "decision_count": len(item.steps),
                "execution_group_count": sum(
                    len(step.execution_groups) for step in item.steps
                ),
                "final_equity": str(account.equity),
                "net_pnl": None if accounting is None else str(accounting.net_pnl),
                "price_pnl": None if accounting is None else str(accounting.price_pnl),
                "funding_pnl": None if accounting is None else str(accounting.funding_pnl),
                "fee_cost": None if accounting is None else str(accounting.fee_cost),
                "gross_turnover": str(item.path.gross_turnover),
                "drawdown_fraction": str(account.drawdown_fraction),
                "position_count": len(account.positions),
            }
        )
    return {
        "producer_capital_evidence": {
            "comparison_id": evidence.comparison_id,
            "evaluation_version": evidence.evaluation_version,
            "as_of": evidence.as_of.isoformat(),
            "initial_cash": str(evidence.initial_cash),
            "shared_panel_count": len(evidence.shared_decision_slot_sets),
            "paths": paths,
        }
    }


def serialize_forecast_stability_evidence(
    evidence: ForecastStabilityEvidence | None,
) -> dict:
    def source_payload(item: ForecastStabilitySourceEvidence) -> dict:
        forecast = item.forecast
        capital = item.capital
        return {
            "label": item.label,
            "role": item.role,
            "assignment_count": forecast.assignment_count,
            "successful_replica_count": forecast.successful_replica_count,
            "failed_replica_count": forecast.failed_replica_count,
            "complete_sample_count": forecast.complete_sample_count,
            "mean_expected_gross_difference_bps": (
                None
                if forecast.mean_max_expected_gross_difference_bps is None
                else str(forecast.mean_max_expected_gross_difference_bps)
            ),
            "maximum_expected_gross_difference_bps": (
                None
                if forecast.maximum_expected_gross_difference_bps is None
                else str(forecast.maximum_expected_gross_difference_bps)
            ),
            "direction_flip_count": forecast.canonical_direction_flip_count,
            "capital": {
                "replayable_case_count": capital.replayable_case_count,
                "unreplayable_case_count": capital.unreplayable_case_count,
                "cash_flip_count": capital.cash_flip_count,
                "expression_flip_count": capital.expression_flip_count,
                "target_change_count": capital.target_change_count,
                "maximum_allocation_fraction_delta": _decimal(
                    capital.maximum_allocation_fraction_delta
                ),
                "maximum_absolute_final_equity_delta": _decimal(
                    capital.maximum_absolute_final_equity_delta
                ),
                "maximum_absolute_fee_cost_delta": _decimal(
                    capital.maximum_absolute_fee_cost_delta
                ),
                "maximum_absolute_turnover_delta": _decimal(
                    capital.maximum_absolute_turnover_delta
                ),
            },
        }

    return {
        "forecast_stability_evidence": (
            None
            if evidence is None
            else {"sources": [source_payload(item) for item in evidence.sources]}
        )
    }


def _decimal(value) -> str | None:
    return None if value is None else str(value)
