"""Read-only Dashboard projection of cost-after Forecast-producer paths."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from threading import Lock

from sqlalchemy.engine import Engine

from investment_manager.forecast.context.posterior import (
    quant_context_posterior_behavior_id,
)
from investment_manager.forecast.context.targets import assemble_context_capital_targets
from investment_manager.forecast.product.repository import SqlProductPayoffProjectionStore
from investment_manager.forecast.quant.runtime import (
    load_quant_forecast_artifact,
    quant_forecast_behavior_id,
)
from investment_manager.governance.evaluation.logical_account import SqlProducerPanelReader
from investment_manager.governance.evaluation.producer_capital import (
    ProducerCapitalComparisonEvidence,
    ProducerCapitalReplay,
    ProductPayoffBuilder,
    compare_producer_capital_paths,
)
from investment_manager.kernel.time import require_utc
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.settings import AppConfig

ProducerCapitalCacheKey = tuple[tuple[str, int, int, tuple[str, ...]], ...]


class ProducerCapitalDashboardReader:
    """Cache one immutable replay until a producer panel ledger changes."""

    def __init__(self, engine: Engine, config: AppConfig) -> None:
        self._engine = engine
        self._config = config
        self._cache_key: ProducerCapitalCacheKey | None = None
        self._cache: ProducerCapitalComparisonEvidence | None = None
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
            self._cache_key = cache_key
            self._cache = result
            return result


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
