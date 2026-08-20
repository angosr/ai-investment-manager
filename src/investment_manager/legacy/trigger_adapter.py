from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import ValidationError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from investment_manager.forecast.context.analyst import assess_behavior_hash
from investment_manager.forecast.context.application import AssessmentCommand
from investment_manager.forecast.context.workflow import (
    ASSESSMENT_WORKFLOW_NAME,
    AssessmentWorkflowRequest,
)
from investment_manager.governance.policy import DeploymentStage
from investment_manager.information.collector import EventStore
from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.legacy.cycle import CycleInput
from investment_manager.legacy.orchestration import build_workflow_request
from investment_manager.legacy.shadow import ShadowStateReader
from investment_manager.legacy.workflows import ANALYSIS_CYCLE_WORKFLOW_NAME
from investment_manager.market.features import FeatureEngine
from investment_manager.market.repository import MarketDataStore
from investment_manager.platform.orchestration import OrchestrationPolicySnapshot
from investment_manager.risk.protection import PortfolioProtectionStore
from investment_manager.scheduling.models import (
    AnalysisCallAdmission,
    AnalysisDispatchRequest,
    AnalysisTriggerType,
    TriggerBatch,
    TriggerDecision,
    TriggerReason,
)
from investment_manager.scheduling.workflows import BUILD_TRIGGER_DISPATCHES_ACTIVITY
from investment_manager.settings import AppConfig
from investment_manager.state.decision.application import (
    DecisionPacketPreparation,
    PacketPreparationStatus,
)


class TriggerBatchRecorder(Protocol):
    def record_batch(self, batch: TriggerBatch, *, analysis_submitted_at: datetime) -> bool: ...

    def admit_analysis_call(
        self,
        batch: TriggerBatch,
        *,
        requested_at: datetime,
    ) -> AnalysisCallAdmission: ...


class AnalysisCallDeferred(Exception):
    def __init__(self, retry_at: datetime) -> None:
        self.retry_at = require_utc(retry_at)
        super().__init__(f"analysis call deferred until {self.retry_at.isoformat()}")


class TriggerDispatchBuilder:
    """Freeze every enabled consumer of one admitted trigger batch."""

    def __init__(
        self,
        *,
        config: AppConfig,
        market_store: MarketDataStore,
        event_store: EventStore,
        state: ShadowStateReader,
        protection: PortfolioProtectionStore,
        packet_preparation: DecisionPacketPreparation | None = None,
        batch_recorder: TriggerBatchRecorder | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if config.deployment.stage not in {DeploymentStage.SHADOW, DeploymentStage.TESTNET}:
            raise ValueError("Trigger 分析构建器只允许在 SHADOW 或 TESTNET 阶段启动")
        if config.assessment.enabled and packet_preparation is None:
            raise ValueError("启用 ContextAssessment 时必须装配 DecisionPacket preparation")
        self._config = config
        self._market_store = market_store
        self._event_store = event_store
        self._state = state
        self._protection = protection
        self._packet_preparation = packet_preparation
        self._batch_recorder = batch_recorder
        self._clock = clock
        self._features = FeatureEngine(config.feature)

    def build(self, batch: TriggerBatch) -> tuple[AnalysisDispatchRequest, ...]:
        as_of = batch.created_at
        cycle_id = stable_id("triggered_cycle", batch.batch_id)
        market = self._market_store.snapshot(
            cycle_id=cycle_id,
            symbol=batch.symbol,
            interval=self._config.market_data.interval,
            as_of=as_of,
            bar_window=self._config.market_data.bar_window,
            source=self._config.market_data.version,
        )
        self._features.compute(market)
        account = self._state.account_for_cycle(
            cycle_id=cycle_id,
            as_of=as_of,
            initial_quote_balance=self._config.shadow.initial_quote_balance,
        )
        marks = {market.symbol: market.last}
        for position in account.positions:
            if position.symbol in marks:
                continue
            trade = self._market_store.latest_trade(symbol=position.symbol, as_of=as_of)
            age_seconds = (as_of - trade.observed_at).total_seconds()
            if age_seconds > self._config.risk.maximum_market_age_seconds:
                raise ValueError(f"{position.symbol} 持仓盯市成交已过期")
            marks[position.symbol] = trade.price
        account = self._protection.observe(account, marks=marks, as_of=as_of)
        events = self._event_store.visible(symbol=batch.symbol, as_of=as_of)
        trigger_types = {item.trigger_type for item in batch.triggers}
        if AnalysisTriggerType.AGENT_WAKEUP in trigger_types:
            reason = TriggerReason.AGENT_WAKEUP
        elif AnalysisTriggerType.POSITION_RECHECK in trigger_types:
            reason = TriggerReason.POSITION_RECHECK
        elif trigger_types == {AnalysisTriggerType.HEARTBEAT}:
            reason = TriggerReason.HEARTBEAT
        else:
            reason = TriggerReason.EVENT_BATCH
        evidence_ids = tuple(
            sorted({evidence for item in batch.triggers for evidence in item.evidence_ids})
        )
        cycle_input = CycleInput(
            market=market,
            account=account,
            events=events,
            frequency_orders_today=self._state.entry_orders_today(as_of=as_of),
            frequency_last_entry_order_at=self._state.last_entry_order_at(
                symbol=batch.symbol, as_of=as_of
            ),
        )
        legacy_request = build_workflow_request(
            cycle_input=cycle_input,
            trigger=TriggerDecision(
                should_run=True,
                reason=reason,
                evidence_ids=evidence_ids,
            ),
            temporal_policy=self._config.temporal,
            created_at=as_of,
            deadline=batch.deadline,
        )
        dispatches = [
            AnalysisDispatchRequest(
                workflow_name=ANALYSIS_CYCLE_WORKFLOW_NAME,
                workflow_id=legacy_request.workflow_id,
                task_queue=self._config.temporal.task_queue,
                payload=legacy_request.model_dump(mode="json"),
            )
        ]
        if self._config.assessment.enabled:
            assert self._packet_preparation is not None
            prepared = self._packet_preparation.prepare(
                analysis_id=stable_id("assessment_input", batch.batch_id),
                as_of=as_of,
                mandate=self._config.assessment.mandate,
            )
            if prepared.status == PacketPreparationStatus.READY:
                assert prepared.packet is not None
                command = AssessmentCommand.create(
                    packet=prepared.packet,
                    analysis_behavior_hash=assess_behavior_hash(
                        self._config.codex_runtime,
                        prepared.packet,
                    ),
                )
                assessment_request = AssessmentWorkflowRequest.create(
                    command=command,
                    orchestration=OrchestrationPolicySnapshot.from_config(
                        self._config.temporal
                    ),
                    created_at=as_of,
                    deadline=batch.deadline,
                )
                dispatches.append(
                    AnalysisDispatchRequest(
                        workflow_name=ASSESSMENT_WORKFLOW_NAME,
                        workflow_id=assessment_request.workflow_id,
                        task_queue=self._config.temporal.assessment_task_queue,
                        payload=assessment_request.model_dump(mode="json"),
                    )
                )
        if self._batch_recorder is not None:
            submitted_at = max(require_utc(self._clock()), as_of)
            admission = self._batch_recorder.admit_analysis_call(
                batch,
                requested_at=submitted_at,
            )
            if not admission.admitted:
                if admission.retry_at is None:
                    raise RuntimeError("调用准入缺少 retry_at")
                raise AnalysisCallDeferred(admission.retry_at)
            self._batch_recorder.record_batch(batch, analysis_submitted_at=submitted_at)
        return tuple(dispatches)


@dataclass(slots=True)
class TriggerCoordinatorActivities:
    builder: TriggerDispatchBuilder

    @activity.defn(name=BUILD_TRIGGER_DISPATCHES_ACTIVITY)
    def build_analysis_dispatches(self, raw_batch: dict[str, Any]) -> dict[str, Any]:
        try:
            batch = TriggerBatch.model_validate(raw_batch)
            dispatches = self.builder.build(batch)
        except ValidationError as exc:
            raise ApplicationError(
                "TriggerBatch 未通过契约校验",
                type="InvalidTriggerBatch",
                non_retryable=True,
            ) from exc
        except AnalysisCallDeferred as exc:
            return {"deferred_until": exc.retry_at.isoformat()}
        except ValueError as exc:
            raise ApplicationError(
                "TriggerBatch 的行情或账户输入暂不可用",
                type="TriggerInputUnavailable",
            ) from exc
        return {
            "workflow_dispatches": [item.model_dump(mode="json") for item in dispatches]
        }
