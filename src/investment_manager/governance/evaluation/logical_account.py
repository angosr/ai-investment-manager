"""Cost-after producer paths that reuse the authoritative capital semantics."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from investment_manager.execution.group.accounting import ProductAccountProjector
from investment_manager.execution.group.engine import execution_leg_from_order
from investment_manager.execution.group.models import (
    ExecutionGroup,
    ExecutionGroupStatus,
    new_execution_group,
)
from investment_manager.execution.planning.planner import (
    PlannedTradeGroup,
    TradePlan,
    TradePlanner,
)
from investment_manager.execution.venue.observation import ProductOrderObservation
from investment_manager.execution.venue.product_mock import (
    MockSubmitBehavior,
    build_mock_product_order,
)
from investment_manager.forecast.results import BaseForecast
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.market.models import ExecutableQuote, ValuationQuote
from investment_manager.market.perpetual.models import FundingSettlement
from investment_manager.portfolio.decision import (
    PortfolioDecisionEngine,
    PortfolioSleeveInput,
)
from investment_manager.portfolio.models import PortfolioAccountSnapshot, PortfolioTarget
from investment_manager.portfolio.policy import CapitalPolicy
from investment_manager.risk.models import RiskOutcome
from investment_manager.risk.portfolio import (
    ApprovedSleeve,
    PortfolioRiskDecision,
    PortfolioRiskEngine,
    SleeveRiskProfile,
)

LOGICAL_ACCOUNT_EVALUATION_VERSION = "producer-logical-account-v1"


class LogicalAccountStep(FrozenModel):
    """One immutable Forecast-to-account transition in an evaluation-owned path."""

    step_id: str
    evaluation_version: str
    path_id: str
    producer_behavior_id: str
    cycle_id: str
    as_of: datetime
    forecast_ids: tuple[str, ...]
    target: PortfolioTarget | None
    risk_decision: PortfolioRiskDecision | None
    trade_plan: TradePlan | None
    execution_groups: tuple[ExecutionGroup, ...]
    account: PortfolioAccountSnapshot


class LogicalAccountPath(FrozenModel):
    """Content-addressed accumulated result, not a second business ledger."""

    path_result_id: str
    evaluation_version: str
    path_id: str
    producer_behavior_id: str
    initial_cash: Decimal
    account: PortfolioAccountSnapshot
    step_ids: tuple[str, ...]
    gross_turnover: Decimal


class ProducerLogicalAccount:
    """Advance one producer with its own positions through shared capital code.

    The caller owns point-in-time Forecast/product construction. This evaluator
    owns only counterfactual state and never writes Portfolio, Risk, Execution,
    Venue, or market tables.
    """

    def __init__(
        self,
        *,
        producer_behavior_id: str,
        capital_policy: CapitalPolicy,
        initial_cash: Decimal,
    ) -> None:
        if not producer_behavior_id or initial_cash <= 0:
            raise ValueError("逻辑账户行为身份和初始资金必须有效")
        if not capital_policy.enabled:
            raise ValueError("逻辑账户必须复用已启用的 CapitalPolicy")
        self._behavior_id = producer_behavior_id
        self._policy = capital_policy
        self._path_id = stable_id(
            "producer_logical_account",
            LOGICAL_ACCOUNT_EVALUATION_VERSION,
            producer_behavior_id,
            capital_policy.version,
            str(initial_cash),
        )
        self._decision = PortfolioDecisionEngine(capital_policy.decision)
        self._risk = PortfolioRiskEngine(capital_policy.risk)
        self._planner = TradePlanner(capital_policy.planner)
        self._projector = ProductAccountProjector(
            portfolio_id=capital_policy.decision.portfolio_id,
            settlement_asset=capital_policy.settlement_asset,
            initial_cash=initial_cash,
        )
        self._groups: dict[str, ExecutionGroup] = {}
        self._observations: dict[str, ProductOrderObservation] = {}
        self._fee_bps_by_instrument = {
            item.instrument.key: item.fee_bps for item in capital_policy.execution_specs
        }
        self._approved_sleeves: dict[str, ApprovedSleeve] = {}
        self._funding: dict[str, FundingSettlement] = {}
        self._account: PortfolioAccountSnapshot | None = None
        self._steps: list[LogicalAccountStep] = []
        self._initial_cash = initial_cash

    @property
    def path_id(self) -> str:
        return self._path_id

    def advance(
        self,
        *,
        as_of: datetime,
        sleeves: tuple[PortfolioSleeveInput, ...],
        quotes: tuple[ExecutableQuote, ...],
        risk_profiles: tuple[SleeveRiskProfile, ...],
        funding_settlements: tuple[FundingSettlement, ...] = (),
    ) -> LogicalAccountStep:
        """Consume one complete producer decision state at its actual availability."""

        as_of = require_utc(as_of)
        self._require_chronological(as_of)
        self._require_path_forecasts(sleeves)
        self._merge_funding(funding_settlements, as_of=as_of)
        cycle_id = stable_id(
            "producer_logical_account_cycle",
            self._path_id,
            as_of.isoformat(),
            tuple(sorted(item.forecast.forecast_id for item in sleeves)),
            self._account.snapshot_id if self._account is not None else "INITIAL",
        )
        account = self._project_account(
            cycle_id=stable_id("logical_account_mark", cycle_id),
            as_of=as_of,
            quotes=quotes,
        )
        target = self._decision.decide(
            cycle_id=cycle_id,
            as_of=as_of,
            account=account,
            sleeves=sleeves,
            quotes=quotes,
            execution_specs=self._policy.execution_specs,
        )
        risk_decision = None
        trade_plan = None
        executed: list[ExecutionGroup] = []
        if target is not None:
            target_profiles = self._profiles_for_target(target, risk_profiles)
            risk_decision = self._risk.evaluate(
                target=target,
                account=account,
                quotes=target.quotes,
                risk_profiles=target_profiles,
                as_of=as_of,
            )
            approved = risk_decision.approved_target
            if risk_decision.outcome == RiskOutcome.APPROVED and approved is not None:
                trade_plan = self._planner.plan(
                    approved=approved,
                    account=account,
                    quotes=self._quotes_for_target(target, quotes),
                    specs=self._policy.execution_specs,
                    as_of=as_of,
                )
                for approved_sleeve in approved.sleeves:
                    self._approved_sleeves[approved_sleeve.sleeve_id] = approved_sleeve
                for planned in trade_plan.groups:
                    executed.append(
                        self._execute_group(
                            plan=trade_plan,
                            planned=planned,
                            as_of=as_of,
                        )
                    )
                if trade_plan.groups:
                    account = self._project_account(
                        cycle_id=stable_id("logical_account_execution", cycle_id),
                        as_of=as_of,
                        quotes=quotes,
                    )
        self._account = account
        values = {
            "evaluation_version": LOGICAL_ACCOUNT_EVALUATION_VERSION,
            "path_id": self._path_id,
            "producer_behavior_id": self._behavior_id,
            "cycle_id": cycle_id,
            "as_of": as_of,
            "forecast_ids": tuple(sorted(item.forecast.forecast_id for item in sleeves)),
            "target": target,
            "risk_decision": risk_decision,
            "trade_plan": trade_plan,
            "execution_groups": tuple(sorted(executed, key=lambda item: item.group_id)),
            "account": account,
        }
        step = LogicalAccountStep(
            step_id=stable_id("logical_account_step", content_hash(values)),
            **values,
        )
        self._steps.append(step)
        return step

    def result(self) -> LogicalAccountPath:
        if self._account is None:
            raise ValueError("逻辑账户尚未接收任何点时状态")
        observations = tuple(self._observations.values())
        gross_turnover = sum(
            (
                item.order.filled_quantity
                * (item.order.average_fill_price or Decimal("0"))
                * item.order.instrument.contract_multiplier
                for item in observations
            ),
            Decimal("0"),
        )
        values = {
            "evaluation_version": LOGICAL_ACCOUNT_EVALUATION_VERSION,
            "path_id": self._path_id,
            "producer_behavior_id": self._behavior_id,
            "initial_cash": self._initial_cash,
            "account": self._account,
            "step_ids": tuple(item.step_id for item in self._steps),
            "gross_turnover": gross_turnover,
        }
        return LogicalAccountPath(
            path_result_id=stable_id("logical_account_path_result", content_hash(values)),
            **values,
        )

    def _project_account(
        self,
        *,
        cycle_id: str,
        as_of: datetime,
        quotes: tuple[ValuationQuote, ...],
    ) -> PortfolioAccountSnapshot:
        groups = tuple(
            sorted(
                (item for item in self._groups.values() if item.started_at <= as_of),
                key=lambda item: (item.started_at, item.group_id),
            )
        )
        history_by_group = {item.group_id: [] for item in groups}
        for observation in self._observations.values():
            if observation.available_at <= as_of:
                history_by_group[observation.order.group_id].append(observation)
        return self._projector.project(
            cycle_id=cycle_id,
            as_of=as_of,
            groups=groups,
            observation_history_by_group={
                key: tuple(
                    sorted(
                        values,
                        key=lambda item: (
                            item.available_at,
                            item.order.observed_at,
                            item.observation_id,
                        ),
                    )
                )
                for key, values in history_by_group.items()
            },
            funding_settlements=tuple(
                sorted(
                    self._funding.values(),
                    key=lambda item: (
                        item.funding_time,
                        item.rate_type.value,
                        item.settlement_id,
                    ),
                )
            ),
            approved_sleeves=tuple(
                self._approved_sleeves[key] for key in sorted(self._approved_sleeves)
            ),
            quotes=tuple(sorted(quotes, key=lambda item: item.instrument.key)),
            previous=self._account,
        )

    def _execute_group(
        self,
        *,
        plan: TradePlan,
        planned: PlannedTradeGroup,
        as_of: datetime,
    ) -> ExecutionGroup:
        """Apply the same immediate-fill paper order and leg semantics without I/O."""

        if planned not in plan.groups:
            raise ValueError("逻辑账户 PlannedTradeGroup 不属于 TradePlan")
        group = new_execution_group(
            plan_id=plan.plan_id,
            planned=planned,
            started_at=as_of,
        )
        if group.group_id in self._groups:
            raise ValueError("逻辑账户不得重复执行同一 ExecutionGroup")
        filled_legs = []
        for leg in group.target_legs:
            try:
                fee_bps = self._fee_bps_by_instrument[leg.instrument.key]
            except KeyError as exc:
                raise ValueError("逻辑账户产品缺少显式费率") from exc
            order = build_mock_product_order(
                leg,
                behavior=MockSubmitBehavior.FILL,
                observed_at=as_of,
                fee_bps=fee_bps,
            )
            observation_hash = content_hash(order)
            observation = ProductOrderObservation(
                observation_id=stable_id("order_observation", observation_hash),
                observation_hash=observation_hash,
                available_at=as_of,
                order=order,
            )
            self._observations[observation.observation_id] = observation
            filled_legs.append(execution_leg_from_order(leg, order))
        filled = ExecutionGroup.model_validate(
            {
                **group.model_dump(mode="python"),
                "status": ExecutionGroupStatus.HEDGED,
                "target_legs": tuple(filled_legs),
                "updated_at": as_of,
                "revision": 1,
            }
        )
        self._groups[filled.group_id] = filled
        return filled

    def _require_chronological(self, as_of: datetime) -> None:
        if self._account is not None and as_of <= self._account.as_of:
            raise ValueError("逻辑账户决策时点必须严格递增")

    def _require_path_forecasts(
        self,
        sleeves: tuple[PortfolioSleeveInput, ...],
    ) -> None:
        ids = tuple(item.sleeve_id for item in sleeves)
        if tuple(sorted(set(ids))) != ids:
            raise ValueError("逻辑账户 Sleeve 输入必须唯一且排序")
        for sleeve in sleeves:
            if (
                sleeve.new_capital_allowed
                and isinstance(sleeve.forecast, BaseForecast)
                and sleeve.forecast.producer_behavior_id != self._behavior_id
            ):
                raise ValueError("逻辑账户不得借用其他 producer behavior 的新增资本预测")

    def _merge_funding(
        self,
        settlements: tuple[FundingSettlement, ...],
        *,
        as_of: datetime,
    ) -> None:
        for settlement in settlements:
            if settlement.observed_at > as_of or settlement.funding_time >= as_of:
                raise ValueError("逻辑账户 Funding 在当前时点尚不可见")
            existing = self._funding.get(settlement.settlement_id)
            if existing is not None and existing != settlement:
                raise ValueError("逻辑账户 Funding identity 发生冲突")
            self._funding[settlement.settlement_id] = settlement

    @staticmethod
    def _profiles_for_target(
        target: PortfolioTarget,
        profiles: tuple[SleeveRiskProfile, ...],
    ) -> tuple[SleeveRiskProfile, ...]:
        by_id = {item.sleeve_id: item for item in profiles}
        if len(by_id) != len(profiles):
            raise ValueError("逻辑账户 Risk profile 不得重复")
        required = tuple(item.sleeve_id for item in target.sleeves)
        if not set(required).issubset(by_id):
            raise ValueError("逻辑账户 Risk profile 必须覆盖 PortfolioTarget")
        return tuple(by_id[key] for key in required)

    @staticmethod
    def _quotes_for_target(
        target: PortfolioTarget,
        quotes: tuple[ExecutableQuote, ...],
    ) -> tuple[ExecutableQuote, ...]:
        required = {
            leg.instrument.key for sleeve in target.sleeves for leg in sleeve.forecast_target.legs
        }
        return tuple(item for item in quotes if item.instrument.key in required)
