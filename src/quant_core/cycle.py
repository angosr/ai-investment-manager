from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from pydantic import Field, field_validator

from quant_core.analyst import Analyst, ProposalNormalizer
from quant_core.calibration import EdgeCalibrationBook
from quant_core.config import AiMode, AppConfig
from quant_core.decision import (
    FrequencyController,
    FrequencyState,
    HighestNetEdgeComposer,
    freeze_candidate_cost_basis,
)
from quant_core.domain import (
    AccountSnapshot,
    AnalysisProposal,
    CycleOutcome,
    DecisionOutcome,
    FrozenModel,
    IntelligenceEvent,
    MarketSnapshot,
    MetricObservation,
    Order,
    OrderStatus,
    PanelSnapshot,
    PositionLifecycle,
    RiskDecision,
    RiskOutcome,
    SignalCandidate,
    TradeIntent,
    _require_utc,
)
from quant_core.execution import ExecutionExchange, MockExchange, entry_client_order_id
from quant_core.execution_contract import (
    ExecutionRequest,
    RiskTransition,
    build_execution_request,
    build_execution_result,
)
from quant_core.features import FeatureEngine
from quant_core.ids import content_hash, stable_id
from quant_core.ledger import (
    CycleFacts,
    FactLedger,
    InMemoryFactLedger,
    RiskReservationRejected,
)
from quant_core.lifecycle import PositionLifecycleManager
from quant_core.metrics import observation
from quant_core.panel import PanelBuilder
from quant_core.reconciliation import MockReconciler
from quant_core.risk import RiskEngine
from quant_core.risk_budget import InMemoryRiskBudgetStore, RiskBudgetStore
from quant_core.strategy import PriceTrendStrategy, Strategy
from quant_core.trigger import TriggerDecision


class CycleInput(FrozenModel):
    market: MarketSnapshot
    account: AccountSnapshot
    events: tuple[IntelligenceEvent, ...] = ()
    frequency_orders_today: int = Field(default=0, ge=0)
    frequency_last_entry_order_at: datetime | None = None

    @field_validator("frequency_last_entry_order_at")
    @classmethod
    def last_entry_order_must_be_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc(value)

    @field_validator("account")
    @classmethod
    def cycle_ids_must_match(cls, account: AccountSnapshot, info):
        market = info.data.get("market")
        if market is not None and account.cycle_id != market.cycle_id:
            raise ValueError("MarketSnapshot 与 AccountSnapshot 的 cycle_id 必须一致")
        return account


class CycleResult(FrozenModel):
    cycle_id: str
    outcome: CycleOutcome
    reason_code: str
    panel: PanelSnapshot
    analysis_proposal: AnalysisProposal | None = None
    candidates: tuple[SignalCandidate, ...]
    intent: TradeIntent | None = None
    risk_decision: RiskDecision | None = None
    execution_request: ExecutionRequest | None = None
    order: Order | None = None
    account_after: AccountSnapshot | None = None
    position_lifecycle: PositionLifecycle | None = None
    exit_order: Order | None = None
    decision_outcome: DecisionOutcome | None = None
    metrics: tuple[MetricObservation, ...] = ()
    outcome_metrics: tuple[MetricObservation, ...] = ()


@dataclass(slots=True)
class AnalysisCycle:
    config: AppConfig
    ledger: FactLedger
    exchange: ExecutionExchange
    risk_budget: RiskBudgetStore
    strategy: Strategy
    calibrations: EdgeCalibrationBook
    analyst: Analyst | None = None

    @classmethod
    def create(
        cls,
        config: AppConfig,
        *,
        strategy: Strategy | None = None,
        analyst: Analyst | None = None,
    ) -> AnalysisCycle:
        ledger = InMemoryFactLedger()
        return cls(
            config=config,
            ledger=ledger,
            exchange=MockExchange(config.execution),
            risk_budget=ledger.risk_budget,
            strategy=(strategy if strategy is not None else PriceTrendStrategy(config.strategy)),
            calibrations=EdgeCalibrationBook(config.calibration),
            analyst=analyst,
        )

    @classmethod
    def with_adapters(
        cls,
        config: AppConfig,
        *,
        ledger: FactLedger,
        exchange: ExecutionExchange,
        risk_budget: RiskBudgetStore | None = None,
        strategy: Strategy | None = None,
        analyst: Analyst | None = None,
    ) -> AnalysisCycle:
        effective_risk_budget = risk_budget or (
            ledger.risk_budget
            if isinstance(ledger, InMemoryFactLedger)
            else InMemoryRiskBudgetStore()
        )
        if isinstance(ledger, InMemoryFactLedger):
            ledger.risk_budget = effective_risk_budget
        return cls(
            config=config,
            ledger=ledger,
            exchange=exchange,
            risk_budget=effective_risk_budget,
            strategy=(strategy if strategy is not None else PriceTrendStrategy(config.strategy)),
            calibrations=EdgeCalibrationBook(config.calibration),
            analyst=analyst,
        )

    def run(self, cycle_input: CycleInput) -> CycleResult:
        prepared = self.prepare(cycle_input)
        if prepared.outcome != CycleOutcome.EXECUTION_PENDING:
            return prepared
        assert prepared.execution_request is not None
        return self.execute(prepared.execution_request)

    def prepare(
        self,
        cycle_input: CycleInput,
        *,
        trigger: TriggerDecision | None = None,
    ) -> CycleResult:
        market = cycle_input.market
        features = FeatureEngine(self.config.feature).compute(market)
        panel = PanelBuilder(self.config.panel).build(
            market=market,
            account=cycle_input.account,
            features=features,
            events=cycle_input.events,
        )
        existing = self.ledger.get(market.cycle_id)
        if existing is not None:
            if existing.panel != panel:
                raise ValueError(f"cycle_id {market.cycle_id} 已存在，但冻结输入或面板哈希不同")
            return self._from_facts(existing)

        program_candidates = tuple(
            freeze_candidate_cost_basis(
                self.calibrations.apply(candidate),
                market=market,
                frequency=self.config.frequency,
                execution=self.config.execution,
            )
            for candidate in self.strategy.evaluate(
                market=market, account=cycle_input.account, features=features
            )
        )
        proposal: AnalysisProposal | None = None
        candidates = program_candidates
        decision_at = market.as_of
        account_age_seconds = max(
            0, int((market.as_of - cycle_input.account.observed_at).total_seconds())
        )
        metrics: list[MetricObservation] = [
            observation(
                "market_data_age_seconds",
                features.market_age_seconds,
                cycle_id=market.cycle_id,
                observed_at=market.as_of,
            ),
            observation(
                "account_data_age_seconds",
                account_age_seconds,
                cycle_id=market.cycle_id,
                observed_at=market.as_of,
            ),
            observation(
                "account_reconciled",
                1 if cycle_input.account.reconciled else 0,
                cycle_id=market.cycle_id,
                observed_at=market.as_of,
            ),
        ]
        if self.config.pipeline.ai_mode == AiMode.PROPOSE:
            if self.analyst is None:
                metrics.append(
                    observation(
                        "codex_analysis_success",
                        0,
                        cycle_id=market.cycle_id,
                        observed_at=market.as_of,
                        dimensions={"reason": "CODEX_ANALYST_UNAVAILABLE"},
                    )
                )
            else:
                analyst_result = (
                    self.analyst.analyze(panel, trigger=trigger)
                    if trigger is not None
                    else self.analyst.analyze(panel)
                )
                if analyst_result.completed_at is not None:
                    decision_at = _require_utc(analyst_result.completed_at)
                    if decision_at < market.as_of:
                        raise ValueError("Codex 完成时间不能早于冻结行情")
                metrics.append(
                    observation(
                        "codex_analysis_success",
                        1 if analyst_result.success else 0,
                        cycle_id=market.cycle_id,
                        observed_at=decision_at,
                        dimensions={"reason": analyst_result.reason_code},
                    )
                )
                if analyst_result.success and analyst_result.proposal is not None:
                    raw_proposal = analyst_result.proposal
                    proposal = raw_proposal.model_copy(
                        update={
                            "proposal_id": stable_id(
                                "proposal", market.cycle_id, content_hash(raw_proposal)
                            )
                        }
                    )
                    try:
                        ai_candidates = tuple(
                            freeze_candidate_cost_basis(
                                self.calibrations.apply(candidate),
                                market=market,
                                frequency=self.config.frequency,
                                execution=self.config.execution,
                            )
                            for candidate in ProposalNormalizer(self.config.proposal).normalize(
                                proposal,
                                panel,
                                signal_observed_at=decision_at,
                            )
                        )
                    except ValueError:
                        metrics.append(
                            observation(
                                "codex_proposal_normalization_success",
                                0,
                                cycle_id=market.cycle_id,
                                observed_at=decision_at,
                                dimensions={"reason": "CODEX_DETERMINISTIC_VALIDATION"},
                            )
                        )
                    else:
                        metrics.append(
                            observation(
                                "codex_proposal_normalization_success",
                                1,
                                cycle_id=market.cycle_id,
                                observed_at=decision_at,
                            )
                        )
                        candidates = program_candidates + ai_candidates
        metrics.append(
            observation(
            "signal_count",
            len(candidates),
            cycle_id=market.cycle_id,
            observed_at=decision_at,
                dimensions={"pipeline": self.config.pipeline.version},
            )
        )
        composed = HighestNetEdgeComposer(
            self.config.composition, self.config.pipeline.version
        ).compose(candidates, as_of=decision_at)
        if composed.intent is None:
            return self._finish(
                panel=panel,
                proposal=proposal,
                candidates=candidates,
                intent=None,
                risk=None,
                order=None,
                metrics=metrics,
                outcome=CycleOutcome.NO_ACTION,
                reason_code=composed.reason_code,
            )

        intent = composed.intent
        frequency = FrequencyController(self.config.frequency, self.config.execution).evaluate(
            intent,
            as_of=decision_at,
            spread_bps=features.spread_bps,
            current_price=market.ask if intent.side.value == "BUY" else market.bid,
            state=FrequencyState(
                orders_today=cycle_input.frequency_orders_today,
                last_order_at=cycle_input.frequency_last_entry_order_at,
            ),
        )
        metrics.append(
            observation(
                "expected_net_edge_bps",
                frequency.expected_net_edge_bps,
                cycle_id=market.cycle_id,
                observed_at=decision_at,
            )
        )
        metrics.extend(
            (
                observation(
                    "remaining_gross_edge_bps",
                    frequency.remaining_gross_edge_bps,
                    cycle_id=market.cycle_id,
                    observed_at=decision_at,
                ),
                observation(
                    "price_move_consumed_bps",
                    frequency.price_move_consumed_bps,
                    cycle_id=market.cycle_id,
                    observed_at=decision_at,
                ),
                observation(
                    "signal_age_seconds",
                    frequency.signal_age_seconds,
                    cycle_id=market.cycle_id,
                    observed_at=decision_at,
                ),
            )
        )
        if not frequency.allowed:
            return self._finish(
                panel=panel,
                proposal=proposal,
                candidates=candidates,
                intent=intent,
                risk=None,
                order=None,
                metrics=metrics,
                outcome=CycleOutcome.NO_TRADE,
                reason_code=frequency.reason_code,
            )

        risk = RiskEngine(self.config.risk).evaluate(
            intent=intent,
            market=market,
            account=cycle_input.account,
            as_of=decision_at,
        )
        metrics.append(
            observation(
                "risk_approved",
                1 if risk.outcome == RiskOutcome.APPROVED else 0,
                cycle_id=market.cycle_id,
                observed_at=decision_at,
            )
        )
        if risk.outcome != RiskOutcome.APPROVED:
            failed = next(
                (item.reason_code for item in risk.rule_results if item.state.value != "PASS"),
                "RISK_REJECTED",
            )
            return self._finish(
                panel=panel,
                proposal=proposal,
                candidates=candidates,
                intent=intent,
                risk=risk,
                order=None,
                metrics=metrics,
                outcome=CycleOutcome.RISK_REJECTED,
                reason_code=failed,
            )

        request = build_execution_request(
            intent=intent,
            risk_decision=risk,
            market=market,
            account=cycle_input.account,
            execution_policy_version=self.config.execution.version,
            created_at=decision_at,
        )
        try:
            return self._finish(
                panel=panel,
                proposal=proposal,
                candidates=candidates,
                intent=intent,
                risk=risk,
                execution_request=request,
                order=None,
                metrics=metrics,
                outcome=CycleOutcome.EXECUTION_PENDING,
                reason_code="RISK_RESERVED_EXECUTION_PENDING",
                maximum_total_risk=(
                    cycle_input.account.quote_balance * self.config.risk.maximum_total_risk_fraction
                ),
            )
        except RiskReservationRejected as exc:
            return self._finish(
                panel=panel,
                proposal=proposal,
                candidates=candidates,
                intent=intent,
                risk=risk,
                order=None,
                metrics=metrics,
                outcome=CycleOutcome.NO_TRADE,
                reason_code=exc.reason_code,
            )

    def execute(
        self,
        request: ExecutionRequest,
        *,
        observed_at: datetime | None = None,
    ) -> CycleResult:
        existing = self.ledger.get(request.cycle_id)
        if existing is None:
            raise ValueError("ExecutionRequest 对应的分析事实不存在")
        if existing.execution_request != request:
            raise ValueError("ExecutionRequest 与已冻结的待执行事实不一致")
        if existing.outcome != CycleOutcome.EXECUTION_PENDING.value:
            return self._from_facts(existing)

        intent = request.intent
        risk = request.risk_decision
        market = request.market
        execution_at = _require_utc(observed_at or market.as_of)
        existing_order = self.exchange.query_entry_order(
            intent=intent,
            risk=risk,
            market=market,
        )
        reservation = risk.reservation
        assert reservation is not None and risk.quantity is not None
        request_expired = execution_at >= min(intent.valid_until, reservation.expires_at)
        skipped_submission = existing_order is None and request_expired
        if skipped_submission:
            client_order_id = entry_client_order_id(intent, risk)
            order = Order(
                order_id=stable_id("expired_order", client_order_id),
                client_order_id=client_order_id,
                cycle_id=request.cycle_id,
                intent_id=intent.intent_id,
                symbol=intent.symbol,
                side=intent.side,
                order_type=intent.entry.order_type,
                requested_quantity=risk.quantity,
                limit_price=intent.entry.price,
                status=OrderStatus.EXPIRED,
            )
        else:
            order = existing_order or self.exchange.submit(
                intent=intent,
                risk=risk,
                market=market,
            )
        if order.status.value in {"NEW", "PARTIALLY_FILLED"}:
            order = self.exchange.cancel_remaining(order)

        account_after: AccountSnapshot | None = None
        lifecycle: PositionLifecycle | None = None
        exit_order: Order | None = None
        decision_outcome: DecisionOutcome | None = None
        outcome_metrics: tuple[MetricObservation, ...] = ()
        if order.fills:
            reconciler = MockReconciler()
            account_after = reconciler.apply(request.account, order)
            lifecycle_result = PositionLifecycleManager(
                exchange=self.exchange,
                reconciler=reconciler,
                risk_budget=None,
            ).open(
                intent=intent,
                risk=risk,
                entry_order=order,
                account_after_entry=account_after,
                market=market,
            )
            account_after = lifecycle_result.account
            lifecycle = lifecycle_result.lifecycle
            exit_order = lifecycle_result.exit_order
            decision_outcome = lifecycle_result.outcome
            outcome_metrics = lifecycle_result.metrics
        execution_metrics: list[MetricObservation] = [
            observation(
                "executed_order_count",
                0 if skipped_submission else 1,
                cycle_id=market.cycle_id,
                observed_at=execution_at,
                dimensions={"status": order.status.value},
            ),
            observation(
                "execution_handoff_age_seconds",
                max(
                    Decimal("0"),
                    Decimal(str((execution_at - request.created_at).total_seconds())),
                ),
                cycle_id=market.cycle_id,
                observed_at=execution_at,
            ),
        ]
        if lifecycle is not None:
            execution_metrics.append(
                observation(
                    "position_protected",
                    1 if lifecycle.protection_id else 0,
                    cycle_id=market.cycle_id,
                    observed_at=market.as_of,
                    dimensions={"status": lifecycle.status.value},
                )
            )
        if skipped_submission:
            cycle_reason = "EXECUTION_SIGNAL_EXPIRED"
        elif (
            decision_outcome is not None
            and decision_outcome.exit_reason.value == "PROTECTION_FAILURE"
        ):
            cycle_reason = "PROTECTION_FAILURE_EMERGENCY_EXIT"
        else:
            cycle_reason = order.status.value
        transition = (
            RiskTransition.CONSUMED
            if lifecycle is not None and lifecycle.status.value != "CLOSED"
            else RiskTransition.RELEASED
        )
        execution_result = build_execution_result(
            execution_id=request.execution_id,
            cycle_id=request.cycle_id,
            outcome=(CycleOutcome.EXECUTED if order.fills else CycleOutcome.NO_TRADE),
            reason_code=cycle_reason,
            risk_transition=transition,
            order=order,
            account_after=account_after,
            position_lifecycle=lifecycle,
            exit_order=exit_order,
            decision_outcome=decision_outcome,
            outcome_metrics=outcome_metrics,
            metrics=tuple(execution_metrics),
        )
        return self._from_facts(self.ledger.complete_execution(execution_result))

    def _finish(
        self,
        *,
        panel: PanelSnapshot,
        proposal: AnalysisProposal | None,
        candidates: tuple[SignalCandidate, ...],
        intent: TradeIntent | None,
        risk: RiskDecision | None,
        execution_request: ExecutionRequest | None = None,
        order: Order | None,
        metrics: list[MetricObservation],
        outcome: CycleOutcome,
        reason_code: str,
        account_after: AccountSnapshot | None = None,
        position_lifecycle: PositionLifecycle | None = None,
        exit_order: Order | None = None,
        decision_outcome: DecisionOutcome | None = None,
        outcome_metrics: tuple[MetricObservation, ...] = (),
        maximum_total_risk: Decimal | None = None,
    ) -> CycleResult:
        result = CycleResult(
            cycle_id=panel.cycle_id,
            outcome=outcome,
            reason_code=reason_code,
            panel=panel,
            analysis_proposal=proposal,
            candidates=candidates,
            intent=intent,
            risk_decision=risk,
            execution_request=execution_request,
            order=order,
            account_after=account_after,
            position_lifecycle=position_lifecycle,
            exit_order=exit_order,
            decision_outcome=decision_outcome,
            metrics=tuple(metrics),
            outcome_metrics=outcome_metrics,
        )
        self.ledger.record(
            CycleFacts(
                cycle_id=result.cycle_id,
                pipeline_version=self.config.pipeline.version,
                panel=panel,
                analysis_proposal=proposal,
                candidates=candidates,
                intent=intent,
                risk_decision=risk,
                execution_request=execution_request,
                order=order,
                account_after=account_after,
                position_lifecycle=position_lifecycle,
                exit_order=exit_order,
                decision_outcome=decision_outcome,
                metrics=tuple(metrics),
                outcome_metrics=outcome_metrics,
                outcome=outcome.value,
                reason_code=reason_code,
            ),
            maximum_total_risk=maximum_total_risk,
        )
        return result

    @staticmethod
    def _from_facts(facts: CycleFacts) -> CycleResult:
        return CycleResult(
            cycle_id=facts.cycle_id,
            outcome=CycleOutcome(facts.outcome),
            reason_code=facts.reason_code,
            panel=facts.panel,
            analysis_proposal=facts.analysis_proposal,
            candidates=facts.candidates,
            intent=facts.intent,
            risk_decision=facts.risk_decision,
            execution_request=facts.execution_request,
            order=facts.order,
            account_after=facts.account_after,
            position_lifecycle=facts.position_lifecycle,
            exit_order=facts.exit_order,
            decision_outcome=facts.decision_outcome,
            metrics=facts.metrics,
            outcome_metrics=facts.outcome_metrics,
        )
