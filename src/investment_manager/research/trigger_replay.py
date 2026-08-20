from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from pydantic import Field, field_validator, model_validator

from investment_manager.config import AppConfig
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.research.dataset import HistoricalEventDataset
from investment_manager.trigger import (
    AnalysisTriggerEvent,
    AnalysisTriggerPlan,
    AnalysisTriggerType,
    TriggerBatch,
    build_trigger_batch,
    build_trigger_event,
    decide_analysis_call_admission,
    trigger_plan_accepts,
    trigger_reconsideration,
)

TRIGGER_REPLAY_VERSION = "portfolio-trigger-replay-v3"


class TriggerReplayInitialScopeState(FrozenModel):
    symbol: str
    last_analysis_at: datetime | None = None

    _utc_last_analysis = field_validator("last_analysis_at")(
        lambda value: require_utc(value) if value is not None else None
    )
class ExternalTriggerReplaySpec(FrozenModel):
    """冻结生产触发规则及跨品种最小调用间隔。"""

    version: str = TRIGGER_REPLAY_VERSION
    plans: tuple[AnalysisTriggerPlan, ...] = Field(min_length=1)
    trigger_policy_version: str
    minimum_call_interval_seconds: int = Field(ge=0)
    maximum_batch_size: int = Field(gt=0)
    maximum_pending_triggers: int = Field(gt=0)
    trigger_expiry_seconds: int = Field(gt=0)
    analysis_deadline_seconds: int = Field(gt=0)
    analysis_duration_seconds: int = Field(ge=0)
    admission_order: tuple[str, ...] = Field(min_length=1)
    initial_global_last_admitted_at: datetime | None = None
    initial_scopes: tuple[TriggerReplayInitialScopeState, ...] = ()
    initial_state_source: Literal["EMPTY", "CYCLE_PERSISTENCE_PROXY", "EXACT"] = "EMPTY"

    _utc_initial_global = field_validator("initial_global_last_admitted_at")(
        lambda value: require_utc(value) if value is not None else None
    )

    @model_validator(mode="after")
    def scopes_and_order_are_unique(self):
        scopes = tuple((item.symbol, item.pipeline_id) for item in self.plans)
        if len(scopes) != len(set(scopes)):
            raise ValueError("触发回放计划作用域不得重复")
        symbols = tuple(item.symbol for item in self.plans)
        if len(symbols) != len(set(symbols)):
            raise ValueError("同一回放暂不允许单品种绑定多个 Pipeline")
        if len(self.admission_order) != len(set(self.admission_order)):
            raise ValueError("跨品种准入顺序不得重复")
        if set(self.admission_order) != set(symbols):
            raise ValueError("跨品种准入顺序必须完整覆盖冻结计划")
        initial_symbols = tuple(item.symbol for item in self.initial_scopes)
        if len(initial_symbols) != len(set(initial_symbols)):
            raise ValueError("触发回放初始品种状态不得重复")
        if not set(initial_symbols).issubset(symbols):
            raise ValueError("触发回放初始状态不得超出冻结计划")
        if self.initial_state_source == "EMPTY" and (
            self.initial_global_last_admitted_at is not None or self.initial_scopes
        ):
            raise ValueError("非空初始状态必须声明来源")
        return self

    @classmethod
    def freeze(
        cls,
        *,
        plans: tuple[AnalysisTriggerPlan, ...],
        config: AppConfig,
        analysis_duration_seconds: int,
        initial_global_last_admitted_at: datetime | None = None,
        initial_scopes: tuple[TriggerReplayInitialScopeState, ...] = (),
        initial_state_source: Literal["EMPTY", "CYCLE_PERSISTENCE_PROXY", "EXACT"] = "EMPTY",
        admission_order: tuple[str, ...] | None = None,
    ) -> ExternalTriggerReplaySpec:
        by_symbol = {item.symbol: item for item in plans}
        order = admission_order or tuple(
            symbol for symbol in config.market_data.symbols if symbol in by_symbol
        )
        if set(order) != set(by_symbol):
            raise ValueError("冻结计划必须属于 MarketDataPolicy 的品种范围")
        return cls(
            plans=tuple(by_symbol[symbol] for symbol in order),
            trigger_policy_version=config.trigger.version,
            minimum_call_interval_seconds=config.trigger.minimum_call_interval_seconds,
            maximum_batch_size=config.trigger.maximum_batch_size,
            maximum_pending_triggers=config.trigger.maximum_pending_triggers,
            trigger_expiry_seconds=config.trigger.trigger_expiry_seconds,
            analysis_deadline_seconds=config.shadow.analysis_deadline_seconds,
            analysis_duration_seconds=analysis_duration_seconds,
            admission_order=order,
            initial_global_last_admitted_at=initial_global_last_admitted_at,
            initial_scopes=initial_scopes,
            initial_state_source=initial_state_source,
        )

class ReplayedTriggerBatch(FrozenModel):
    batch: TriggerBatch
    analysis_completed_at: datetime

    _utc_completed_at = field_validator("analysis_completed_at")(require_utc)

    @model_validator(mode="after")
    def completion_follows_submission(self):
        if self.analysis_completed_at < self.batch.created_at:
            raise ValueError("回放分析完成时间不能早于批次创建时间")
        return self


class TriggerReplayScopeSummary(FrozenModel):
    symbol: str
    pipeline_id: str
    source_event_count: int = Field(ge=0)
    accepted_trigger_count: int = Field(ge=0)
    rejected_trigger_count: int = Field(ge=0)
    capacity_dropped_count: int = Field(ge=0)
    expired_trigger_count: int = Field(ge=0)
    unprocessed_trigger_count: int = Field(ge=0)
    batch_count: int = Field(ge=0)
    batched_trigger_count: int = Field(ge=0)

    @model_validator(mode="after")
    def counts_are_conserved(self):
        if self.source_event_count != (self.accepted_trigger_count + self.rejected_trigger_count):
            raise ValueError("品种触发来源事件计数不守恒")
        if self.accepted_trigger_count != (
            self.batched_trigger_count
            + self.capacity_dropped_count
            + self.expired_trigger_count
            + self.unprocessed_trigger_count
        ):
            raise ValueError("品种已接受事件计数不守恒")
        return self


ReplayLimitation = Literal[
    "EXTERNAL_EVENTS_ONLY",
    "FIXED_ANALYSIS_DURATION",
    "SIMULTANEOUS_ADMISSION_ORDER_ASSUMPTION",
    "PLAN_POSTDATES_REPLAY_START",
    "INITIAL_LOCAL_STATE_FROM_PERSISTENCE_PROXY",
]


class ExternalTriggerReplay(FrozenModel):
    replay_id: str
    version: str = TRIGGER_REPLAY_VERSION
    spec: ExternalTriggerReplaySpec
    spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_dataset_id: str
    replay_start: datetime
    replay_end: datetime
    scopes: tuple[TriggerReplayScopeSummary, ...]
    batches: tuple[ReplayedTriggerBatch, ...]
    limitations: tuple[ReplayLimitation, ...]

    _utc_replay_start = field_validator("replay_start")(require_utc)
    _utc_replay_end = field_validator("replay_end")(require_utc)

    @model_validator(mode="after")
    def identity_scope_and_order_match(self):
        if self.replay_start >= self.replay_end:
            raise ValueError("触发回放起点必须早于终点")
        order = tuple(
            (item.batch.created_at, item.batch.symbol, item.batch.batch_id) for item in self.batches
        )
        if order != tuple(sorted(order)):
            raise ValueError("触发回放批次必须按创建时间和品种排序")
        if self.spec_hash != content_hash(self.spec):
            raise ValueError("触发回放规格哈希不一致")
        plans = {(item.symbol, item.pipeline_id): item for item in self.spec.plans}
        summaries = {(item.symbol, item.pipeline_id): item for item in self.scopes}
        if set(plans) != set(summaries):
            raise ValueError("触发回放汇总作用域与冻结计划不一致")
        for scope, summary in summaries.items():
            scoped_batches = [
                item
                for item in self.batches
                if (item.batch.symbol, item.batch.pipeline_id) == scope
            ]
            if summary.batch_count != len(scoped_batches) or summary.batched_trigger_count != sum(
                len(item.batch.triggers) for item in scoped_batches
            ):
                raise ValueError("触发回放批次与品种汇总不一致")
        if any(
            (item.batch.symbol, item.batch.pipeline_id) not in plans
            or item.batch.plan_revision
            != plans[(item.batch.symbol, item.batch.pipeline_id)].revision
            or not self.replay_start <= item.batch.created_at < self.replay_end
            for item in self.batches
        ):
            raise ValueError("触发回放批次作用域或窗口不一致")
        payload = self.model_dump(mode="json", exclude={"replay_id"})
        if self.replay_id != stable_id("external_trigger_replay", content_hash(payload)):
            raise ValueError("触发回放 ID 与内容不一致")
        return self


@dataclass(slots=True)
class _ReplayState:
    plan: AnalysisTriggerPlan
    raw_plan: dict
    triggers: tuple[AnalysisTriggerEvent, ...]
    cursor: int = 0
    pending: dict[str, dict] = field(default_factory=dict)
    last_analysis_at: datetime | None = None
    input_retry_not_before: datetime | None = None
    busy_until: datetime | None = None
    accepted: int = 0
    rejected: int = 0
    capacity_dropped: int = 0
    expired: int = 0


def run_external_trigger_replay(
    *,
    event_dataset: HistoricalEventDataset,
    spec: ExternalTriggerReplaySpec,
    replay_start: datetime,
    replay_end: datetime,
) -> ExternalTriggerReplay:
    """在一个离散时钟中回放所有品种协调器及其共享防重复间隔。"""

    start = require_utc(replay_start)
    end = require_utc(replay_end)
    if start >= end:
        raise ValueError("触发回放起点必须早于终点")
    if event_dataset.manifest.requested_start > start or event_dataset.manifest.requested_end < end:
        raise ValueError("历史事件数据集必须覆盖完整触发回放窗口")
    initial_by_symbol = {item.symbol: item for item in spec.initial_scopes}
    if (
        spec.initial_global_last_admitted_at is not None
        and spec.initial_global_last_admitted_at >= start
    ):
        raise ValueError("初始全局准入必须早于回放起点")
    if any(
        item.last_analysis_at is not None and item.last_analysis_at >= start
        for item in spec.initial_scopes
    ):
        raise ValueError("初始最后分析时间必须早于回放起点")

    states = {
        plan.symbol: _ReplayState(
            plan=plan,
            raw_plan=plan.model_dump(mode="json"),
            triggers=tuple(
                build_trigger_event(
                    trigger_type=AnalysisTriggerType.INTELLIGENCE_INSERTED,
                    symbol=plan.symbol,
                    pipeline_id=plan.pipeline_id,
                    occurred_at=event.event_time,
                    observed_at=event.observed_at,
                    priority=int(event.impact * 100),
                    dedup_key=event.evidence_id,
                    evidence_ids=(event.evidence_id,),
                    expires_at=event.observed_at + timedelta(seconds=spec.trigger_expiry_seconds),
                )
                for event in event_dataset.events
                if plan.symbol in event.symbols and start <= event.observed_at < end
            ),
            last_analysis_at=(
                initial_by_symbol[plan.symbol].last_analysis_at
                if plan.symbol in initial_by_symbol
                else None
            ),
        )
        for plan in spec.plans
    }
    global_last_admitted_at = spec.initial_global_last_admitted_at
    batches: list[ReplayedTriggerBatch] = []
    now = start

    while now < end:
        for symbol in spec.admission_order:
            _deliver_observed(states[symbol], now, spec)

        progressed = True
        while progressed:
            progressed = False
            for symbol in spec.admission_order:
                state = states[symbol]
                if state.busy_until is not None and state.busy_until <= now:
                    state.last_analysis_at = state.busy_until
                    state.busy_until = None
                    progressed = True
                if state.busy_until is None:
                    state.expired += _discard_expired(state.pending, now)

            for symbol in spec.admission_order:
                state = states[symbol]
                if state.busy_until is not None or not state.pending:
                    continue
                eligible = _eligible(tuple(state.pending.values()))
                timing = trigger_reconsideration(
                    plan=state.raw_plan,
                    pending=eligible,
                    now=now,
                    last_analysis_at=state.last_analysis_at,
                    input_retry_not_before=state.input_retry_not_before,
                    wake_at_expiry=True,
                )
                if timing.reconsider_at > now:
                    continue
                admission = decide_analysis_call_admission(
                    requested_at=now,
                    last_admitted_at=global_last_admitted_at,
                    minimum_call_interval_seconds=spec.minimum_call_interval_seconds,
                )
                if not admission.admitted:
                    state.input_retry_not_before = admission.retry_at
                    continue

                selected = eligible[: spec.maximum_batch_size]
                batch = build_trigger_batch(
                    plan=state.plan,
                    triggers=tuple(
                        AnalysisTriggerEvent.model_validate(item)
                        for item in sorted(selected, key=lambda item: str(item["trigger_id"]))
                    ),
                    created_at=now,
                    deadline=now + timedelta(seconds=spec.analysis_deadline_seconds),
                )
                for item in selected:
                    state.pending.pop(str(item["trigger_id"]), None)
                state.input_retry_not_before = None
                state.busy_until = now + timedelta(seconds=spec.analysis_duration_seconds)
                global_last_admitted_at = now
                batches.append(
                    ReplayedTriggerBatch(
                        batch=batch,
                        analysis_completed_at=state.busy_until,
                    )
                )
                progressed = True

        next_times = [end]
        for state in states.values():
            if state.cursor < len(state.triggers):
                next_times.append(state.triggers[state.cursor].observed_at)
            if state.busy_until is not None:
                next_times.append(state.busy_until)
            elif state.pending:
                timing = trigger_reconsideration(
                    plan=state.raw_plan,
                    pending=_eligible(tuple(state.pending.values())),
                    now=now,
                    last_analysis_at=state.last_analysis_at,
                    input_retry_not_before=state.input_retry_not_before,
                    wake_at_expiry=True,
                )
                next_times.append(timing.reconsider_at)
        future = [item for item in next_times if item > now]
        if not future:
            raise RuntimeError("触发回放时钟未前进")
        now = min(future)

    ordered_batches = tuple(
        sorted(
            batches,
            key=lambda item: (
                item.batch.created_at,
                item.batch.symbol,
                item.batch.batch_id,
            ),
        )
    )
    summaries = tuple(
        _scope_summary(states[symbol], ordered_batches) for symbol in spec.admission_order
    )
    limitations: list[ReplayLimitation] = [
        "EXTERNAL_EVENTS_ONLY",
        "FIXED_ANALYSIS_DURATION",
    ]
    if len(spec.plans) > 1:
        limitations.append("SIMULTANEOUS_ADMISSION_ORDER_ASSUMPTION")
    if any(plan.updated_at > start for plan in spec.plans):
        limitations.append("PLAN_POSTDATES_REPLAY_START")
    if spec.initial_state_source == "CYCLE_PERSISTENCE_PROXY":
        limitations.append("INITIAL_LOCAL_STATE_FROM_PERSISTENCE_PROXY")
    payload = {
        "version": TRIGGER_REPLAY_VERSION,
        "spec": spec,
        "spec_hash": content_hash(spec),
        "event_dataset_id": event_dataset.manifest.dataset_id,
        "replay_start": start,
        "replay_end": end,
        "scopes": summaries,
        "batches": ordered_batches,
        "limitations": tuple(limitations),
    }
    return ExternalTriggerReplay(
        replay_id=stable_id("external_trigger_replay", content_hash(payload)),
        **payload,
    )


def _deliver_observed(
    state: _ReplayState,
    now: datetime,
    spec: ExternalTriggerReplaySpec,
) -> None:
    while state.cursor < len(state.triggers) and state.triggers[state.cursor].observed_at <= now:
        trigger = state.triggers[state.cursor]
        state.cursor += 1
        raw = trigger.model_dump(mode="json")
        if not trigger_plan_accepts(state.raw_plan, raw):
            state.rejected += 1
            continue
        state.accepted += 1
        state.pending[trigger.trigger_id] = raw
        if len(state.pending) > spec.maximum_pending_triggers:
            retained = _eligible(tuple(state.pending.values()))[: spec.maximum_pending_triggers]
            state.capacity_dropped += len(state.pending) - len(retained)
            state.pending = {str(item["trigger_id"]): item for item in retained}


def _discard_expired(pending: dict[str, dict], now: datetime) -> int:
    expired_ids = [
        key
        for key, item in pending.items()
        if item.get("expires_at") is not None and _payload_time(item["expires_at"]) <= now
    ]
    for key in expired_ids:
        pending.pop(key)
    return len(expired_ids)


def _scope_summary(
    state: _ReplayState,
    batches: tuple[ReplayedTriggerBatch, ...],
) -> TriggerReplayScopeSummary:
    scoped = tuple(item for item in batches if item.batch.symbol == state.plan.symbol)
    return TriggerReplayScopeSummary(
        symbol=state.plan.symbol,
        pipeline_id=state.plan.pipeline_id,
        source_event_count=len(state.triggers),
        accepted_trigger_count=state.accepted,
        rejected_trigger_count=state.rejected,
        capacity_dropped_count=state.capacity_dropped,
        expired_trigger_count=state.expired,
        unprocessed_trigger_count=len(state.pending) + len(state.triggers) - state.cursor,
        batch_count=len(scoped),
        batched_trigger_count=sum(len(item.batch.triggers) for item in scoped),
    )


def _eligible(pending: tuple[dict, ...]) -> list[dict]:
    return sorted(
        pending,
        key=lambda item: (
            -int(item.get("priority", 0)),
            str(item.get("observed_at", "")),
            str(item.get("trigger_id", "")),
        ),
    )


def _payload_time(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return require_utc(parsed)
