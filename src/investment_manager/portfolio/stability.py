"""Point-in-time capital impact of exact-input Context Forecast replicas."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from investment_manager.forecast.context.estimate import (
    ContextForecastStructuredOutput,
)
from investment_manager.forecast.context.stability import (
    ContextForecastStabilityAssignment,
    ContextForecastStabilityResult,
    ContextForecastStabilityStatus,
)
from investment_manager.forecast.models import ExposureDirection
from investment_manager.forecast.product.models import (
    ProductPayoffBucket,
    ProductPayoffProjection,
)
from investment_manager.forecast.product.repository import (
    SqlProductPayoffProjectionStore,
)
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.forecast.results import (
    BaseForecast,
    Forecast,
    ForecastBucketProbability,
)
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.types import FrozenModel
from investment_manager.portfolio.decision import (
    PortfolioDecisionEngine,
    PortfolioSleeveInput,
)
from investment_manager.portfolio.models import (
    CandidateCapitalAuthorization,
    PortfolioAccountSnapshot,
    PortfolioTarget,
)
from investment_manager.portfolio.policy import CapitalPolicy
from investment_manager.portfolio.repository import (
    SqlPortfolioStore,
    load_portfolio_target,
)
from investment_manager.portfolio.tables import (
    portfolio_target_forecasts,
    portfolio_targets,
)


class CapitalStabilityExpression(FrozenModel):
    economic_exposure_id: str
    instrument_keys: tuple[str, ...]
    directions: tuple[ExposureDirection, ...]
    desired_gross_notional: Decimal


class CapitalStabilityChoice(FrozenModel):
    expressions: tuple[CapitalStabilityExpression, ...]
    reference_equity: Decimal

    @property
    def cash(self) -> bool:
        return not any(item.desired_gross_notional > 0 for item in self.expressions)


class CapitalStabilityCase(FrozenModel):
    assignment_id: str
    replica_index: int
    formal_target_id: str
    formal: CapitalStabilityChoice
    replica: CapitalStabilityChoice
    cash_flip: bool
    expression_flip: bool
    allocation_changed: bool
    maximum_allocation_fraction_delta: Decimal

    @property
    def target_changed(self) -> bool:
        return self.expression_flip or self.allocation_changed


class PortfolioForecastStabilityReport(FrozenModel):
    assignment_count: int
    successful_replica_count: int
    replayable_case_count: int
    missing_capital_target_count: int
    cash_flip_count: int
    expression_flip_count: int
    target_change_count: int
    maximum_allocation_fraction_delta: Decimal | None
    cases: tuple[CapitalStabilityCase, ...]


@dataclass(frozen=True, slots=True)
class CapitalStabilityReplayInputs:
    target: PortfolioTarget
    account: PortfolioAccountSnapshot
    forecasts: dict[str, Forecast]
    projections: dict[str, ProductPayoffProjection]


class PortfolioForecastStabilityEvaluator:
    """Replay replicas through the sole Portfolio decision engine, without writes."""

    def __init__(self, *, engine: Engine, capital_policy: CapitalPolicy) -> None:
        self._engine = engine
        self._policy = capital_policy
        self._portfolio = SqlPortfolioStore(engine)
        self._forecasts = SqlForecastStore(engine)
        self._projections = SqlProductPayoffProjectionStore(engine)
        self._decision = PortfolioDecisionEngine(capital_policy.decision)
        self._authorization_by_family = {
            item.outcome_family_id: item
            for item in capital_policy.candidate_capital_authorizations
        }

    def evaluate(
        self,
        *,
        assignments: tuple[ContextForecastStabilityAssignment, ...],
        results: tuple[ContextForecastStabilityResult, ...],
    ) -> PortfolioForecastStabilityReport:
        result_by_assignment: dict[str, list[ContextForecastStabilityResult]] = {}
        for result in results:
            if result.status == ContextForecastStabilityStatus.SUCCEEDED:
                result_by_assignment.setdefault(result.assignment_id, []).append(result)

        cases: list[CapitalStabilityCase] = []
        missing_targets = 0
        successful_replicas = 0
        for assignment in assignments:
            assignment_results = tuple(
                sorted(
                    result_by_assignment.get(assignment.assignment_id, ()),
                    key=lambda item: item.replica_index,
                )
            )
            successful_replicas += len(assignment_results)
            if not assignment_results:
                continue
            inputs = self._inputs(assignment)
            if inputs is None:
                missing_targets += 1
                continue
            for result in assignment_results:
                cases.append(
                    replay_context_forecast_capital_impact(
                        assignment=assignment,
                        result=result,
                        inputs=inputs,
                        decision=self._decision,
                        capital_policy=self._policy,
                        authorization_by_family=self._authorization_by_family,
                    )
                )

        ordered = tuple(sorted(cases, key=lambda item: (item.assignment_id, item.replica_index)))
        return PortfolioForecastStabilityReport(
            assignment_count=len(assignments),
            successful_replica_count=successful_replicas,
            replayable_case_count=len(ordered),
            missing_capital_target_count=missing_targets,
            cash_flip_count=sum(item.cash_flip for item in ordered),
            expression_flip_count=sum(item.expression_flip for item in ordered),
            target_change_count=sum(item.target_changed for item in ordered),
            maximum_allocation_fraction_delta=max(
                (item.maximum_allocation_fraction_delta for item in ordered),
                default=None,
            ),
            cases=ordered,
        )

    def _inputs(
        self,
        assignment: ContextForecastStabilityAssignment,
    ) -> CapitalStabilityReplayInputs | None:
        formal_ids = tuple(item.formal_forecast_id for item in assignment.targets)
        target = self._target_covering(formal_ids)
        if target is None:
            return None
        if target.policy_version != self._policy.decision.version:
            raise ValueError("稳定性资本重放不能使用不同 Portfolio 行为的历史目标")
        if target.candidate_evaluations is None:
            raise ValueError("稳定性资本重放需要完整的冻结候选经济性")
        account = self._portfolio.account(target.account_snapshot_id)
        if account is None:
            raise ValueError("稳定性资本重放缺少冻结账户快照")
        forecasts = {
            forecast_id: self._forecasts.forecast(forecast_id)
            for forecast_id in target.considered_forecast_ids
        }
        if any(item is None for item in forecasts.values()):
            raise ValueError("稳定性资本重放缺少正式 Forecast")
        projection_ids = {
            item.payoff_projection_id
            for item in target.candidate_evaluations
            if item.payoff_projection_id is not None
        }
        projections = {
            projection_id: self._projections.get(projection_id)
            for projection_id in projection_ids
        }
        if any(item is None for item in projections.values()):
            raise ValueError("稳定性资本重放缺少正式产品投影")
        return CapitalStabilityReplayInputs(
            target=target,
            account=account,
            forecasts={key: value for key, value in forecasts.items() if value is not None},
            projections={key: value for key, value in projections.items() if value is not None},
        )

    def _target_covering(self, forecast_ids: tuple[str, ...]) -> PortfolioTarget | None:
        target_ids = (
            select(portfolio_target_forecasts.c.target_id)
            .where(portfolio_target_forecasts.c.forecast_id.in_(forecast_ids))
            .group_by(portfolio_target_forecasts.c.target_id)
            .having(
                func.count(func.distinct(portfolio_target_forecasts.c.forecast_id))
                == len(forecast_ids)
            )
        )
        with self._engine.connect() as connection:
            payloads = tuple(
                connection.execute(
                    select(portfolio_targets.c.payload).where(
                        portfolio_targets.c.target_id.in_(target_ids)
                    )
                ).scalars()
            )
        targets = tuple(
            item
            for payload in payloads
            if set(forecast_ids).issubset(
                set((item := load_portfolio_target(payload)).considered_forecast_ids)
            )
        )
        if len(targets) > 1:
            raise ValueError("同一 Context Forecast 组合绑定了多个资本目标")
        return targets[0] if targets else None


def replay_context_forecast_capital_impact(
    *,
    assignment: ContextForecastStabilityAssignment,
    result: ContextForecastStabilityResult,
    inputs: CapitalStabilityReplayInputs,
    decision: PortfolioDecisionEngine,
    capital_policy: CapitalPolicy,
    authorization_by_family: dict[str, CandidateCapitalAuthorization],
) -> CapitalStabilityCase:
    """Substitute replica probabilities and deterministically replay Portfolio."""

    if result.status != ContextForecastStabilityStatus.SUCCEEDED or result.output_json is None:
        raise ValueError("资本稳定性重放只接受成功副本")
    if result.assignment_id != assignment.assignment_id:
        raise ValueError("资本稳定性结果与 assignment 不一致")

    formal_ids = {item.formal_forecast_id for item in assignment.targets}
    formal_sleeves = _sleeves(
        target=inputs.target,
        forecasts=inputs.forecasts,
        projections=inputs.projections,
        fresh_forecast_ids=formal_ids,
        authorization_by_family=authorization_by_family,
    )
    formal_replay = decision.decide(
        cycle_id=inputs.target.cycle_id,
        as_of=inputs.target.as_of,
        account=inputs.account,
        sleeves=formal_sleeves,
        quotes=inputs.target.quotes,
        execution_specs=capital_policy.execution_specs,
    )
    if formal_replay != inputs.target:
        raise ValueError("正式 Portfolio 输入无法逐字段重建权威目标")

    replica_output = ContextForecastStructuredOutput.model_validate_json(result.output_json)
    replica_forecasts, replica_projections = _replica_inputs(
        assignment=assignment,
        output=replica_output,
        forecasts=inputs.forecasts,
        projections=inputs.projections,
    )
    replica_target = decision.decide(
        cycle_id=stable_id(
            "context_forecast_capital_stability_replay",
            assignment.assignment_id,
            result.replica_index,
        ),
        as_of=inputs.target.as_of,
        account=inputs.account,
        sleeves=_sleeves(
            target=inputs.target,
            forecasts=replica_forecasts,
            projections=replica_projections,
            fresh_forecast_ids=formal_ids,
            authorization_by_family=authorization_by_family,
        ),
        quotes=inputs.target.quotes,
        execution_specs=capital_policy.execution_specs,
    )
    if replica_target is None:
        raise ValueError("启用的 Portfolio 行为在副本重放中没有形成目标")

    exposure_by_sleeve = {
        item.sleeve_id: item.economic_exposure_key
        for item in formal_sleeves
    }
    formal_choice = _choice(inputs.target, exposure_by_sleeve=exposure_by_sleeve)
    replica_choice = _choice(replica_target, exposure_by_sleeve=exposure_by_sleeve)
    formal_expressions = _positive_expression_keys(formal_choice)
    replica_expressions = _positive_expression_keys(replica_choice)
    allocation_delta = _maximum_allocation_delta(formal_choice, replica_choice)
    return CapitalStabilityCase(
        assignment_id=assignment.assignment_id,
        replica_index=result.replica_index,
        formal_target_id=inputs.target.target_id,
        formal=formal_choice,
        replica=replica_choice,
        cash_flip=formal_choice.cash != replica_choice.cash,
        expression_flip=formal_expressions != replica_expressions,
        allocation_changed=allocation_delta != 0,
        maximum_allocation_fraction_delta=allocation_delta,
    )


def _sleeves(
    *,
    target: PortfolioTarget,
    forecasts: dict[str, Forecast],
    projections: dict[str, ProductPayoffProjection],
    fresh_forecast_ids: set[str],
    authorization_by_family: dict[str, CandidateCapitalAuthorization],
) -> tuple[PortfolioSleeveInput, ...]:
    assert target.candidate_evaluations is not None
    values = []
    for candidate in target.candidate_evaluations:
        forecast = forecasts[candidate.forecast_id]
        fresh = candidate.forecast_id in fresh_forecast_ids
        authorization = None
        if fresh and isinstance(forecast, BaseForecast):
            authorization = authorization_by_family.get(forecast.outcome_family_id)
            if authorization is None:
                raise ValueError("稳定性资本重放缺少 candidate authorization")
        values.append(
            PortfolioSleeveInput(
                sleeve_id=candidate.sleeve_id,
                forecast=forecast,
                payoff_projection=(
                    projections[candidate.payoff_projection_id]
                    if candidate.payoff_projection_id is not None
                    else None
                ),
                capital_authorization=authorization,
                new_capital_allowed=fresh,
            )
        )
    return tuple(sorted(values, key=lambda item: item.sleeve_id))


def _replica_inputs(
    *,
    assignment: ContextForecastStabilityAssignment,
    output: ContextForecastStructuredOutput,
    forecasts: dict[str, Forecast],
    projections: dict[str, ProductPayoffProjection],
) -> tuple[dict[str, Forecast], dict[str, ProductPayoffProjection]]:
    draft_by_slot = {item.decision_slot_id: item for item in output.forecasts}
    target_by_slot = {item.decision_slot_id: item for item in assignment.targets}
    if set(draft_by_slot) != set(target_by_slot):
        raise ValueError("稳定性资本重放的副本槽集合漂移")
    replaced_forecasts = dict(forecasts)
    replaced_projections = dict(projections)
    for target in assignment.targets:
        formal = forecasts.get(target.formal_forecast_id)
        if not isinstance(formal, BaseForecast):
            raise ValueError("Context 稳定性资本重放只接受 BaseForecast")
        draft = draft_by_slot[target.decision_slot_id]
        probabilities = tuple(
            ForecastBucketProbability(
                bucket_id=item.bucket_id,
                probability=Decimal(item.probability),
            )
            for item in draft.outcome_probabilities
        )
        representatives = dict(target.representative_bps)
        if {item.bucket_id for item in probabilities} != set(representatives):
            raise ValueError("稳定性资本重放的 bucket 集合漂移")
        replica = BaseForecast.model_validate(
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
        replaced_forecasts[formal.forecast_id] = replica
        for projection_id, projection in projections.items():
            if projection.source_forecast_id == formal.forecast_id:
                replaced_projections[projection_id] = _reweight_projection(
                    projection,
                    probabilities=probabilities,
                )
    return replaced_forecasts, replaced_projections


def _reweight_projection(
    projection: ProductPayoffProjection,
    *,
    probabilities: tuple[ForecastBucketProbability, ...],
) -> ProductPayoffProjection:
    probability_by_bucket = {item.bucket_id: item.probability for item in probabilities}
    if {item.source_bucket_id for item in projection.outcome_payoffs} != set(
        probability_by_bucket
    ):
        raise ValueError("稳定性资本重放的产品 bucket 集合漂移")
    buckets = tuple(
        ProductPayoffBucket(
            source_bucket_id=item.source_bucket_id,
            probability=probability_by_bucket[item.source_bucket_id],
            payoff_bps=item.payoff_bps,
            conservative_payoff_bps=item.conservative_payoff_bps,
        )
        for item in projection.outcome_payoffs
    )
    draft = projection.model_copy(
        update={
            "projection_id": "counterfactual",
            "outcome_payoffs": buckets,
            "expected_gross_bps": sum(
                (item.probability * item.payoff_bps for item in buckets),
                Decimal("0"),
            ),
            "conservative_gross_bps": sum(
                (
                    item.probability * item.conservative_payoff_bps
                    for item in buckets
                ),
                Decimal("0"),
            ),
        }
    )
    projection_id = stable_id(
        "product_payoff_projection",
        content_hash(
            draft.model_dump(
                mode="json",
                exclude={"projection_id", "mapping_cohort_id"},
            )
        ),
    )
    return ProductPayoffProjection.model_validate(
        {**draft.model_dump(mode="python"), "projection_id": projection_id}
    )


def _choice(
    target: PortfolioTarget,
    *,
    exposure_by_sleeve: dict[str, str],
) -> CapitalStabilityChoice:
    expressions = tuple(
        CapitalStabilityExpression(
            economic_exposure_id=exposure_by_sleeve[item.sleeve_id],
            instrument_keys=tuple(leg.instrument.key for leg in item.forecast_target.legs),
            directions=tuple(leg.direction for leg in item.forecast_target.legs),
            desired_gross_notional=item.desired_gross_notional,
        )
        for item in target.sleeves
    )
    return CapitalStabilityChoice(
        expressions=expressions,
        reference_equity=target.reference_equity,
    )


def _positive_expression_keys(
    choice: CapitalStabilityChoice,
) -> frozenset[tuple[str, tuple[str, ...], tuple[ExposureDirection, ...]]]:
    return frozenset(
        (item.economic_exposure_id, item.instrument_keys, item.directions)
        for item in choice.expressions
        if item.desired_gross_notional > 0
    )


def _maximum_allocation_delta(
    formal: CapitalStabilityChoice,
    replica: CapitalStabilityChoice,
) -> Decimal:
    formal_by_expression = {
        (item.economic_exposure_id, item.instrument_keys, item.directions): (
            item.desired_gross_notional / formal.reference_equity
        )
        for item in formal.expressions
    }
    replica_by_expression = {
        (item.economic_exposure_id, item.instrument_keys, item.directions): (
            item.desired_gross_notional / replica.reference_equity
        )
        for item in replica.expressions
    }
    keys = set(formal_by_expression) | set(replica_by_expression)
    return max(
        (
            abs(
                formal_by_expression.get(key, Decimal("0"))
                - replica_by_expression.get(key, Decimal("0"))
            )
            for key in keys
        ),
        default=Decimal("0"),
    )


__all__ = [
    "CapitalStabilityCase",
    "CapitalStabilityChoice",
    "CapitalStabilityExpression",
    "CapitalStabilityReplayInputs",
    "PortfolioForecastStabilityEvaluator",
    "PortfolioForecastStabilityReport",
    "replay_context_forecast_capital_impact",
]
