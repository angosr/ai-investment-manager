"""Cost-after logical-account impact of exact-input Forecast replicas."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from investment_manager.forecast.context.estimate import (
    ContextForecastDraft,
)
from investment_manager.forecast.context.posterior import (
    audit_quant_context_posterior_draft,
)
from investment_manager.forecast.context.stability import (
    ContextForecastStabilityAssignment,
    ContextForecastStabilityResult,
    ContextForecastStabilityStatus,
    parse_context_forecast_output_json,
)
from investment_manager.forecast.contracts import (
    ForecastNoEstimate,
    ForecastNoEstimateReason,
    ForecastPriceAnchor,
)
from investment_manager.forecast.results import (
    BaseForecast,
    ForecastBucketProbability,
    ForecastMechanismContribution,
)
from investment_manager.governance.evaluation.logical_account import (
    LogicalAccountPath,
    LogicalAccountStep,
    ProducerDecisionPanel,
    ProducerPanelLedger,
)
from investment_manager.governance.evaluation.producer_capital import (
    ProducerCapitalReplay,
    ProductPayoffBuilder,
    compare_producer_capital_paths,
)
from investment_manager.kernel.errors import PointInTimeInputUnavailable
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.types import FrozenModel
from investment_manager.market.features import point_in_time_quote_views
from investment_manager.market.models import InstrumentProduct
from investment_manager.market.repository import MarketDataStore
from investment_manager.portfolio.models import PortfolioTarget
from investment_manager.portfolio.policy import CapitalPolicy, SleeveRiskTemplate


@dataclass(frozen=True, slots=True)
class _CapitalStabilityExpression:
    forecast_family: str
    instrument_keys: tuple[str, ...]
    directions: tuple[str, ...]
    desired_gross_notional: Decimal


@dataclass(frozen=True, slots=True)
class _CapitalStabilityChoice:
    expressions: tuple[_CapitalStabilityExpression, ...]
    reference_equity: Decimal

    @property
    def cash(self) -> bool:
        return not any(item.desired_gross_notional > 0 for item in self.expressions)


@dataclass(frozen=True, slots=True)
class _CapitalStabilityCase:
    cash_flip: bool
    expression_flip: bool
    allocation_changed: bool
    maximum_allocation_fraction_delta: Decimal

    @property
    def target_changed(self) -> bool:
        return self.expression_flip or self.allocation_changed


@dataclass(frozen=True, slots=True)
class _CapitalStabilityPathDelta:
    final_equity_delta: Decimal
    fee_cost_delta: Decimal
    turnover_delta: Decimal


class PortfolioForecastStabilityReport(FrozenModel):
    assignment_count: int
    successful_replica_count: int
    replayable_case_count: int
    unreplayable_case_count: int
    cash_flip_count: int
    expression_flip_count: int
    target_change_count: int
    maximum_allocation_fraction_delta: Decimal | None
    maximum_absolute_final_equity_delta: Decimal | None
    maximum_absolute_fee_cost_delta: Decimal | None
    maximum_absolute_turnover_delta: Decimal | None


@dataclass(frozen=True, slots=True)
class _PanelPair:
    formal: ProducerDecisionPanel
    replica: ProducerDecisionPanel


class PortfolioForecastStabilityEvaluator:
    """Run formal and replica outputs through independent logical accounts."""

    def __init__(
        self,
        *,
        capital_policy: CapitalPolicy,
        initial_cash: Decimal,
        market: MarketDataStore,
        product_payoffs_by_family: Mapping[str, ProductPayoffBuilder],
        sleeve_risk: SleeveRiskTemplate,
    ) -> None:
        self._policy = capital_policy
        self._initial_cash = initial_cash
        self._market = market
        self._product_payoffs = dict(product_payoffs_by_family)
        self._sleeve_risk = sleeve_risk

    def evaluate(
        self,
        *,
        formal_ledger: ProducerPanelLedger,
        assignments: tuple[ContextForecastStabilityAssignment, ...],
        results: tuple[ContextForecastStabilityResult, ...],
    ) -> PortfolioForecastStabilityReport:
        result_by_key = {
            (item.assignment_id, item.replica_index): item for item in results
        }
        panels_by_assignment = {
            assignment.assignment_id: self._formal_panel(formal_ledger, assignment)
            for assignment in assignments
        }
        replica_indices = tuple(sorted({item.replica_index for item in results}))
        cases: list[_CapitalStabilityCase] = []
        path_deltas: list[_CapitalStabilityPathDelta] = []
        unreplayable = 0
        for replica_index in replica_indices:
            pairs = []
            for assignment in assignments:
                result = result_by_key.get((assignment.assignment_id, replica_index))
                panel = panels_by_assignment[assignment.assignment_id]
                if result is None:
                    continue
                if result.status == ContextForecastStabilityStatus.NOT_REQUIRED:
                    continue
                if panel is None:
                    unreplayable += 1
                    continue
                try:
                    replica = self._replica_panel(
                        assignment=assignment,
                        result=result,
                        formal=panel,
                    )
                except (PointInTimeInputUnavailable, ValueError):
                    unreplayable += 1
                    continue
                pairs.append(
                    _PanelPair(
                        formal=panel,
                        replica=replica,
                    )
                )
            if not pairs:
                continue
            ordered = tuple(
                sorted(
                    pairs,
                    key=lambda item: (item.formal.available_at, item.formal.panel_id),
                )
            )
            try:
                comparison = compare_producer_capital_paths(
                    initial_cash=self._initial_cash,
                    sources={
                        "FORMAL": (
                            _ledger(formal_ledger, tuple(item.formal for item in ordered)),
                            self._replay(formal_ledger.producer_behavior_id),
                        ),
                        "REPLICA": (
                            _ledger(formal_ledger, tuple(item.replica for item in ordered)),
                            self._replay(formal_ledger.producer_behavior_id),
                        ),
                    },
                )
            except PointInTimeInputUnavailable:
                unreplayable += len(ordered)
                continue
            if comparison is None:
                unreplayable += len(ordered)
                continue
            by_label = {item.label: item for item in comparison.paths}
            formal_path = by_label["FORMAL"]
            replica_path = by_label["REPLICA"]
            formal_steps = dict(zip(formal_path.panel_ids, formal_path.steps, strict=True))
            replica_steps = dict(zip(replica_path.panel_ids, replica_path.steps, strict=True))
            for pair in ordered:
                cases.append(
                    _case(
                        formal=formal_steps[pair.formal.panel_id],
                        replica=replica_steps[pair.replica.panel_id],
                    )
                )
            path_deltas.append(
                _path_delta(
                    formal=formal_path.path,
                    replica=replica_path.path,
                )
            )

        return PortfolioForecastStabilityReport(
            assignment_count=len(assignments),
            successful_replica_count=sum(
                item.status == ContextForecastStabilityStatus.SUCCEEDED for item in results
            ),
            replayable_case_count=len(cases),
            unreplayable_case_count=unreplayable,
            cash_flip_count=sum(item.cash_flip for item in cases),
            expression_flip_count=sum(item.expression_flip for item in cases),
            target_change_count=sum(item.target_changed for item in cases),
            maximum_allocation_fraction_delta=max(
                (item.maximum_allocation_fraction_delta for item in cases),
                default=None,
            ),
            maximum_absolute_final_equity_delta=_maximum_absolute(
                item.final_equity_delta for item in path_deltas
            ),
            maximum_absolute_fee_cost_delta=_maximum_absolute(
                item.fee_cost_delta for item in path_deltas
            ),
            maximum_absolute_turnover_delta=_maximum_absolute(
                item.turnover_delta for item in path_deltas
            ),
        )

    def _formal_panel(
        self,
        ledger: ProducerPanelLedger,
        assignment: ContextForecastStabilityAssignment,
    ) -> ProducerDecisionPanel | None:
        target_slots = {item.decision_slot_id for item in assignment.targets}
        matches = tuple(
            panel
            for panel in ledger.complete_panels
            if target_slots.issubset({item.slot_id for item in panel.slots})
            and panel.information_cutoff_at == assignment.information_cutoff_at
        )
        if len(matches) > 1:
            raise ValueError("稳定性 assignment 匹配了多个 producer panel")
        return matches[0] if matches else None

    def _replica_panel(
        self,
        *,
        assignment: ContextForecastStabilityAssignment,
        result: ContextForecastStabilityResult,
        formal: ProducerDecisionPanel,
    ) -> ProducerDecisionPanel:
        if result.status == ContextForecastStabilityStatus.FAILED:
            return self._failed_replica_panel(
                assignment=assignment,
                result=result,
                formal=formal,
            )
        if result.output_json is None:
            raise ValueError("成功稳定性副本缺少输出")
        output = parse_context_forecast_output_json(result.output_json)
        drafts = {item.decision_slot_id: item for item in output.forecasts}
        expected = {item.decision_slot_id for item in assignment.targets}
        if len(drafts) != len(output.forecasts) or set(drafts) != expected:
            raise ValueError("稳定性副本 target 集合漂移")
        formal_by_slot = {item.decision_slot_id: item for item in formal.forecasts}
        if not expected.issubset(formal_by_slot):
            raise ValueError("稳定性资本路径缺少正式 Forecast")
        analysis_input = json.loads(assignment.formal_analysis_input_json)
        replicas = tuple(
            self._replica_forecast(
                assignment=assignment,
                result=result,
                formal=forecast,
                draft=drafts[forecast.decision_slot_id],
                analysis_input=analysis_input,
            )
            if forecast.decision_slot_id in expected
            else forecast
            for forecast in formal.forecasts
        )
        values = {
            "producer_id": formal.producer_id,
            "producer_behavior_id": formal.producer_behavior_id,
            "slot_as_of": formal.slot_as_of,
            "information_cutoff_at": formal.information_cutoff_at,
            "available_at": max(
                (
                    *(item.available_at for item in replicas),
                    *(item.completed_at for item in formal.no_estimates),
                )
            ),
            "obligations": formal.obligations,
            "slots": formal.slots,
            "forecasts": tuple(sorted(replicas, key=lambda item: item.outcome_family_id)),
            "no_estimates": formal.no_estimates,
        }
        return ProducerDecisionPanel(
            panel_id=stable_id(
                "producer_stability_replica_panel",
                assignment.assignment_id,
                result.replica_index,
                content_hash(values),
            ),
            **values,
        )

    @staticmethod
    def _failed_replica_panel(
        *,
        assignment: ContextForecastStabilityAssignment,
        result: ContextForecastStabilityResult,
        formal: ProducerDecisionPanel,
    ) -> ProducerDecisionPanel:
        target_slots = {item.decision_slot_id for item in assignment.targets}
        obligation_by_slot = {item.slot_id: item for item in formal.obligations}
        replacement_absences = tuple(
            ForecastNoEstimate(
                result_id=stable_id(
                    "forecast_no_estimate",
                    slot_id,
                    formal.producer_behavior_id,
                ),
                slot_id=slot_id,
                contract_id=obligation_by_slot[slot_id].contract_id,
                producer_kind=obligation_by_slot[slot_id].producer_kind,
                producer_id=formal.producer_id,
                producer_behavior_id=formal.producer_behavior_id,
                reason=ForecastNoEstimateReason.PRODUCER_FAILED,
                information_cutoff_at=formal.information_cutoff_at,
                attempted_at=formal.slot_as_of,
                completed_at=max(result.completed_at, formal.slot_as_of),
                input_refs=tuple(sorted((assignment.assignment_id, result.result_id))),
                detail=result.reason_code,
            )
            for slot_id in sorted(target_slots)
        )
        forecasts = tuple(
            item for item in formal.forecasts if item.decision_slot_id not in target_slots
        )
        no_estimates = tuple(
            sorted(
                (
                    *(item for item in formal.no_estimates if item.slot_id not in target_slots),
                    *replacement_absences,
                ),
                key=lambda item: item.contract_id,
            )
        )
        values = {
            "producer_id": formal.producer_id,
            "producer_behavior_id": formal.producer_behavior_id,
            "slot_as_of": formal.slot_as_of,
            "information_cutoff_at": formal.information_cutoff_at,
            "available_at": max(
                (
                    *(item.available_at for item in forecasts),
                    *(item.completed_at for item in no_estimates),
                )
            ),
            "obligations": formal.obligations,
            "slots": formal.slots,
            "forecasts": forecasts,
            "no_estimates": no_estimates,
        }
        return ProducerDecisionPanel(
            panel_id=stable_id(
                "producer_stability_replica_panel",
                assignment.assignment_id,
                result.replica_index,
                content_hash(values),
            ),
            **values,
        )

    def _replica_forecast(
        self,
        *,
        assignment: ContextForecastStabilityAssignment,
        result: ContextForecastStabilityResult,
        formal: BaseForecast,
        draft: ContextForecastDraft,
        analysis_input: dict,
    ) -> BaseForecast:
        target = next(
            item
            for item in assignment.targets
            if item.decision_slot_id == formal.decision_slot_id
        )
        if analysis_input.get("purpose") == "QUANT_CONTEXT_POSTERIOR":
            quant_prior = _quant_prior(
                formal=formal,
                target=target,
                analysis_input=analysis_input,
            )
            draft = audit_quant_context_posterior_draft(
                draft=draft,
                quant_prior=quant_prior,
                analysis_input=analysis_input,
            )
        probabilities = tuple(
            ForecastBucketProbability(
                bucket_id=item.bucket_id,
                probability=Decimal(item.probability),
            )
            for item in draft.outcome_probabilities
        )
        representatives = dict(target.representative_bps)
        if tuple(item.bucket_id for item in probabilities) != tuple(representatives):
            raise ValueError("稳定性副本 bucket 顺序漂移")
        completed_at = result.completed_at
        anchors = self._entry_anchors(formal, at=completed_at)
        validity = formal.valid_until - formal.available_at
        available_until = min(formal.economic_horizon_end, completed_at + validity)
        if available_until <= completed_at:
            raise PointInTimeInputUnavailable("稳定性副本完成时经济时域已耗尽")
        world_model = analysis_input.get("world_model")
        world_model_id = (
            world_model.get("assessment_id") if isinstance(world_model, dict) else None
        )
        if not isinstance(world_model_id, str):
            raise ValueError("稳定性副本缺少 WorldModel 身份")
        values = {
            **formal.model_dump(mode="python"),
            "entry_prices": anchors,
            "available_at": completed_at,
            "valid_until": available_until,
            "outcome_probabilities": probabilities,
            "expected_gross_bps": sum(
                (
                    item.probability * representatives[item.bucket_id]
                    for item in probabilities
                ),
                Decimal("0"),
            ),
            "input_refs": tuple(
                sorted({*formal.input_refs, assignment.assignment_id, result.result_id})
            ),
            "world_model_id": world_model_id,
            "mechanism_contributions": tuple(
                ForecastMechanismContribution(**item.model_dump())
                for item in draft.mechanism_contributions
            ),
            "evidence_refs": tuple(sorted(set(draft.evidence_refs))),
            "invalidation_conditions": tuple(
                sorted(set(draft.invalidation_conditions))
            ),
            "analysis_input_json": assignment.formal_analysis_input_json,
            "analysis_input_hash": assignment.formal_analysis_input_hash,
        }
        return BaseForecast.model_validate(values)

    def _entry_anchors(
        self,
        forecast: BaseForecast,
        *,
        at: datetime,
    ) -> tuple[ForecastPriceAnchor, ...]:
        schedule = (
            self._market.latest_trading_schedule(as_of=at)
            if any(
                item.instrument.product == InstrumentProduct.TRADFI_PERPETUAL
                for item in forecast.target.legs
            )
            else None
        )
        anchors = []
        for leg in forecast.target.legs:
            views = point_in_time_quote_views(
                market=self._market,
                instrument=leg.instrument,
                as_of=at,
                maximum_live_age_seconds=self._policy.risk.maximum_quote_age_seconds,
                trading_schedule=schedule,
            )
            executable = None if views is None else views[1]
            if executable is None:
                raise PointInTimeInputUnavailable(
                    f"稳定性副本缺少 {leg.instrument.key} 可成交报价"
                )
            anchors.append(
                ForecastPriceAnchor(
                    instrument_id=leg.instrument.key,
                    price=executable.ask,
                    observed_at=executable.observed_at,
                    available_at=at,
                    quote_ref=executable.source_quote_id,
                )
            )
        return tuple(sorted(anchors, key=lambda item: item.instrument_id))

    def _replay(self, producer_behavior_id: str) -> ProducerCapitalReplay:
        return ProducerCapitalReplay(
            producer_behavior_id=producer_behavior_id,
            capital_policy=self._policy,
            initial_cash=self._initial_cash,
            market=self._market,
            product_payoffs_by_family=self._product_payoffs,
            sleeve_risk=self._sleeve_risk,
        )


def _quant_prior(
    *,
    formal: BaseForecast,
    target,
    analysis_input: dict,
) -> BaseForecast:
    raw_targets = analysis_input.get("forecast_targets")
    if not isinstance(raw_targets, list):
        raise ValueError("稳定性 posterior 输入缺少 targets")
    raw_target = next(
        (
            item
            for item in raw_targets
            if isinstance(item, dict)
            and isinstance(item.get("decision_slot"), dict)
            and item["decision_slot"].get("decision_slot_id") == formal.decision_slot_id
        ),
        None,
    )
    quant_panel = raw_target.get("quant_panel") if isinstance(raw_target, dict) else None
    quant_prior = quant_panel.get("quant_prior") if isinstance(quant_panel, dict) else None
    raw_probabilities = (
        quant_prior.get("outcome_probabilities")
        if isinstance(quant_prior, dict)
        else None
    )
    if not isinstance(raw_probabilities, list):
        raise ValueError("稳定性 posterior 输入缺少 Quant prior")
    probabilities = tuple(
        ForecastBucketProbability.model_validate(item) for item in raw_probabilities
    )
    representatives = dict(target.representative_bps)
    return BaseForecast.model_validate(
        {
            **formal.model_dump(mode="python"),
            "outcome_probabilities": probabilities,
            "expected_gross_bps": sum(
                (
                    item.probability * representatives[item.bucket_id]
                    for item in probabilities
                ),
                Decimal("0"),
            ),
        }
    )


def _ledger(
    source: ProducerPanelLedger,
    panels: tuple[ProducerDecisionPanel, ...],
) -> ProducerPanelLedger:
    return ProducerPanelLedger(
        producer_behavior_id=source.producer_behavior_id,
        as_of=max(item.available_at for item in panels),
        obligated_panel_count=len(panels),
        complete_panels=tuple(sorted(panels, key=lambda item: (item.available_at, item.panel_id))),
        pending_panel_count=0,
    )


def _case(
    *,
    formal: LogicalAccountStep,
    replica: LogicalAccountStep,
) -> _CapitalStabilityCase:
    formal_choice = _choice(formal.target, equity=formal.account.equity)
    replica_choice = _choice(replica.target, equity=replica.account.equity)
    formal_expressions = _positive_expression_keys(formal_choice)
    replica_expressions = _positive_expression_keys(replica_choice)
    allocation_delta = _maximum_allocation_delta(formal_choice, replica_choice)
    return _CapitalStabilityCase(
        cash_flip=formal_choice.cash != replica_choice.cash,
        expression_flip=formal_expressions != replica_expressions,
        allocation_changed=allocation_delta != 0,
        maximum_allocation_fraction_delta=allocation_delta,
    )


def _choice(
    target: PortfolioTarget | None,
    *,
    equity: Decimal,
) -> _CapitalStabilityChoice:
    if target is None:
        return _CapitalStabilityChoice(expressions=(), reference_equity=equity)
    return _CapitalStabilityChoice(
        expressions=tuple(
            _CapitalStabilityExpression(
                forecast_family=item.forecast_family,
                instrument_keys=tuple(
                    leg.instrument.key for leg in item.forecast_target.legs
                ),
                directions=tuple(leg.direction.value for leg in item.forecast_target.legs),
                desired_gross_notional=item.desired_gross_notional,
            )
            for item in target.sleeves
        ),
        reference_equity=target.reference_equity,
    )


def _positive_expression_keys(
    choice: _CapitalStabilityChoice,
) -> frozenset[tuple[str, tuple[str, ...], tuple[str, ...]]]:
    return frozenset(
        (item.forecast_family, item.instrument_keys, item.directions)
        for item in choice.expressions
        if item.desired_gross_notional > 0
    )


def _maximum_allocation_delta(
    formal: _CapitalStabilityChoice,
    replica: _CapitalStabilityChoice,
) -> Decimal:
    def allocations(choice: _CapitalStabilityChoice):
        return {
            (item.forecast_family, item.instrument_keys, item.directions): (
                item.desired_gross_notional / choice.reference_equity
            )
            for item in choice.expressions
        }

    formal_values = allocations(formal)
    replica_values = allocations(replica)
    return max(
        (
            abs(formal_values.get(key, Decimal("0")) - replica_values.get(key, Decimal("0")))
            for key in set(formal_values) | set(replica_values)
        ),
        default=Decimal("0"),
    )


def _path_delta(
    *,
    formal: LogicalAccountPath,
    replica: LogicalAccountPath,
) -> _CapitalStabilityPathDelta:
    formal_fee = (
        Decimal("0")
        if formal.account.accounting is None
        else formal.account.accounting.fee_cost
    )
    replica_fee = (
        Decimal("0")
        if replica.account.accounting is None
        else replica.account.accounting.fee_cost
    )
    return _CapitalStabilityPathDelta(
        final_equity_delta=replica.account.equity - formal.account.equity,
        fee_cost_delta=replica_fee - formal_fee,
        turnover_delta=replica.gross_turnover - formal.gross_turnover,
    )


def _maximum_absolute(values) -> Decimal | None:
    materialized = tuple(abs(item) for item in values)
    return max(materialized, default=None)


__all__ = [
    "PortfolioForecastStabilityEvaluator",
    "PortfolioForecastStabilityReport",
]
