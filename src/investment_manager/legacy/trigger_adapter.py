from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import ValidationError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from investment_manager.governance.policy import DeploymentStage
from investment_manager.information.collector import EventStore
from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.legacy.cycle import CycleInput
from investment_manager.legacy.orchestration import WorkflowRequest, build_workflow_request
from investment_manager.legacy.shadow import ShadowStateReader
from investment_manager.market.features import FeatureEngine
from investment_manager.market.repository import MarketDataStore
from investment_manager.risk.protection import PortfolioProtectionStore
from investment_manager.scheduling.models import (
    AnalysisCallAdmission,
    AnalysisTriggerType,
    TriggerBatch,
    TriggerDecision,
    TriggerReason,
)
from investment_manager.scheduling.workflows import BUILD_TRIGGER_REQUEST_ACTIVITY
from investment_manager.settings import AppConfig


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


class TriggerAnalysisRequestBuilder:
    """Adapt an immutable trigger batch to the retiring AnalysisCycle contract."""

    def __init__(
        self,
        *,
        config: AppConfig,
        market_store: MarketDataStore,
        event_store: EventStore,
        state: ShadowStateReader,
        protection: PortfolioProtectionStore,
        batch_recorder: TriggerBatchRecorder | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if config.deployment.stage not in {DeploymentStage.SHADOW, DeploymentStage.TESTNET}:
            raise ValueError("Trigger 分析构建器只允许在 SHADOW 或 TESTNET 阶段启动")
        self._config = config
        self._market_store = market_store
        self._event_store = event_store
        self._state = state
        self._protection = protection
        self._batch_recorder = batch_recorder
        self._clock = clock
        self._features = FeatureEngine(config.feature)

    def build(self, batch: TriggerBatch) -> WorkflowRequest:
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
        request = build_workflow_request(
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
        return request


@dataclass(slots=True)
class TriggerCoordinatorActivities:
    builder: TriggerAnalysisRequestBuilder

    @activity.defn(name=BUILD_TRIGGER_REQUEST_ACTIVITY)
    def build_analysis_request(self, raw_batch: dict[str, Any]) -> dict[str, Any]:
        try:
            batch = TriggerBatch.model_validate(raw_batch)
            request = self.builder.build(batch)
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
        return {"workflow_request": request.model_dump(mode="json")}
