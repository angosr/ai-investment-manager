from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import Field, field_validator, model_validator

from quant_core.config import AppConfig
from quant_core.domain import FrozenModel, _require_utc
from quant_core.ids import content_hash, stable_id
from quant_core.research.dataset import HistoricalEventDataset
from quant_core.trigger import (
    AnalysisTriggerEvent,
    AnalysisTriggerPlan,
    AnalysisTriggerType,
    TriggerBatch,
    build_trigger_batch,
    build_trigger_event,
    trigger_plan_accepts,
    trigger_reconsideration,
)

TRIGGER_REPLAY_VERSION = "external-trigger-replay-v1"


class ExternalTriggerReplaySpec(FrozenModel):
    version: str = TRIGGER_REPLAY_VERSION
    plan: AnalysisTriggerPlan
    trigger_policy_version: str
    minimum_call_interval_seconds: int = Field(ge=0)
    maximum_ai_calls_per_hour: int = Field(gt=0)
    maximum_batch_size: int = Field(gt=0)
    maximum_pending_triggers: int = Field(gt=0)
    trigger_expiry_seconds: int = Field(gt=0)
    analysis_deadline_seconds: int = Field(gt=0)
    analysis_duration_seconds: int = Field(ge=0)

    @classmethod
    def freeze(
        cls,
        *,
        plan: AnalysisTriggerPlan,
        config: AppConfig,
        analysis_duration_seconds: int,
    ) -> ExternalTriggerReplaySpec:
        return cls(
            plan=plan,
            trigger_policy_version=config.trigger.version,
            minimum_call_interval_seconds=config.trigger.minimum_call_interval_seconds,
            maximum_ai_calls_per_hour=config.trigger.maximum_ai_calls_per_hour,
            maximum_batch_size=config.trigger.maximum_batch_size,
            maximum_pending_triggers=config.trigger.maximum_pending_triggers,
            trigger_expiry_seconds=config.trigger.trigger_expiry_seconds,
            analysis_deadline_seconds=config.shadow.analysis_deadline_seconds,
            analysis_duration_seconds=analysis_duration_seconds,
        )


class ReplayedTriggerBatch(FrozenModel):
    batch: TriggerBatch
    analysis_completed_at: datetime

    _utc_completed_at = field_validator("analysis_completed_at")(_require_utc)

    @model_validator(mode="after")
    def completion_follows_submission(self):
        if self.analysis_completed_at < self.batch.created_at:
            raise ValueError("回放分析完成时间不能早于批次创建时间")
        return self


class ExternalTriggerReplay(FrozenModel):
    replay_id: str
    version: str = TRIGGER_REPLAY_VERSION
    spec: ExternalTriggerReplaySpec
    spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_dataset_id: str
    symbol: str
    replay_start: datetime
    replay_end: datetime
    source_event_count: int = Field(ge=0)
    accepted_trigger_count: int = Field(ge=0)
    rejected_trigger_count: int = Field(ge=0)
    capacity_dropped_count: int = Field(ge=0)
    expired_trigger_count: int = Field(ge=0)
    unprocessed_trigger_count: int = Field(ge=0)
    batches: tuple[ReplayedTriggerBatch, ...]
    limitations: tuple[
        Literal[
            "EXTERNAL_EVENTS_ONLY",
            "FIXED_ANALYSIS_DURATION",
            "GLOBAL_CROSS_SYMBOL_ADMISSION_NOT_REPLAYED",
            "PLAN_POSTDATES_REPLAY_START",
        ],
        ...,
    ]

    _utc_replay_start = field_validator("replay_start")(_require_utc)
    _utc_replay_end = field_validator("replay_end")(_require_utc)

    @model_validator(mode="after")
    def identity_and_order_match(self):
        if self.replay_start >= self.replay_end:
            raise ValueError("触发回放起点必须早于终点")
        order = tuple(
            (item.batch.created_at, item.batch.batch_id) for item in self.batches
        )
        if order != tuple(sorted(order)):
            raise ValueError("触发回放批次必须按创建时间排序")
        if self.spec_hash != content_hash(self.spec):
            raise ValueError("触发回放规格哈希不一致")
        if self.source_event_count != (
            self.accepted_trigger_count + self.rejected_trigger_count
        ):
            raise ValueError("触发回放来源事件计数不守恒")
        batched = sum(len(item.batch.triggers) for item in self.batches)
        if self.accepted_trigger_count != (
            batched
            + self.capacity_dropped_count
            + self.expired_trigger_count
            + self.unprocessed_trigger_count
        ):
            raise ValueError("触发回放已接受事件计数不守恒")
        if any(
            item.batch.symbol != self.symbol
            or item.batch.pipeline_id != self.spec.plan.pipeline_id
            or item.batch.plan_revision != self.spec.plan.revision
            or not self.replay_start <= item.batch.created_at < self.replay_end
            for item in self.batches
        ):
            raise ValueError("触发回放批次作用域或窗口不一致")
        payload = self.model_dump(mode="json", exclude={"replay_id"})
        if self.replay_id != stable_id("external_trigger_replay", content_hash(payload)):
            raise ValueError("触发回放 ID 与内容不一致")
        return self


def run_external_trigger_replay(
    *,
    event_dataset: HistoricalEventDataset,
    spec: ExternalTriggerReplaySpec,
    symbol: str,
    replay_start: datetime,
    replay_end: datetime,
) -> ExternalTriggerReplay:
    """Replay production coordinator timing for external events on one symbol."""

    start = _require_utc(replay_start)
    end = _require_utc(replay_end)
    if start >= end:
        raise ValueError("触发回放起点必须早于终点")
    if spec.plan.symbol != symbol:
        raise ValueError("触发回放品种与计划不一致")
    if (
        event_dataset.manifest.requested_start > start
        or event_dataset.manifest.requested_end < end
    ):
        raise ValueError("历史事件数据集必须覆盖完整触发回放窗口")

    triggers = tuple(
        build_trigger_event(
            trigger_type=AnalysisTriggerType.INTELLIGENCE_INSERTED,
            symbol=symbol,
            pipeline_id=spec.plan.pipeline_id,
            occurred_at=event.event_time,
            observed_at=event.observed_at,
            priority=int(event.impact * 100),
            dedup_key=event.evidence_id,
            evidence_ids=(event.evidence_id,),
            expires_at=event.observed_at
            + timedelta(seconds=spec.trigger_expiry_seconds),
        )
        for event in event_dataset.events
        if symbol in event.symbols and start <= event.observed_at < end
    )
    raw_plan = spec.plan.model_dump(mode="json")
    pending: dict[str, dict] = {}
    cursor = 0
    now = start
    last_analysis_at: datetime | None = None
    call_times: list[datetime] = []
    accepted = rejected = capacity_dropped = expired = 0
    batches: list[ReplayedTriggerBatch] = []

    while now < end:
        while cursor < len(triggers) and triggers[cursor].observed_at <= now:
            trigger = triggers[cursor]
            cursor += 1
            raw = trigger.model_dump(mode="json")
            if not trigger_plan_accepts(raw_plan, raw):
                rejected += 1
                continue
            accepted += 1
            pending[trigger.trigger_id] = raw
            if len(pending) > spec.maximum_pending_triggers:
                ordered = _eligible(tuple(pending.values()))
                retained = ordered[: spec.maximum_pending_triggers]
                capacity_dropped += len(pending) - len(retained)
                pending = {str(item["trigger_id"]): item for item in retained}

        before = len(pending)
        pending = {
            key: item
            for key, item in pending.items()
            if item.get("expires_at") is None
            or _payload_time(item["expires_at"]) > now
        }
        expired += before - len(pending)

        eligible = _eligible(tuple(pending.values()))
        if eligible:
            timing = trigger_reconsideration(
                plan=raw_plan,
                pending=eligible,
                now=now,
                last_analysis_at=last_analysis_at,
                call_times=call_times,
                input_retry_not_before=None,
                minimum_call_interval_seconds=spec.minimum_call_interval_seconds,
                maximum_ai_calls_per_hour=spec.maximum_ai_calls_per_hour,
                wake_at_expiry=True,
            )
            call_times = list(timing.retained_call_times)
            if timing.reconsider_at <= now:
                selected = eligible[: spec.maximum_batch_size]
                batch = build_trigger_batch(
                    plan=spec.plan,
                    triggers=tuple(
                        AnalysisTriggerEvent.model_validate(item)
                        for item in sorted(
                            selected, key=lambda item: str(item["trigger_id"])
                        )
                    ),
                    created_at=now,
                    deadline=now + timedelta(seconds=spec.analysis_deadline_seconds),
                )
                for item in selected:
                    pending.pop(str(item["trigger_id"]), None)
                completed_at = now + timedelta(seconds=spec.analysis_duration_seconds)
                batches.append(
                    ReplayedTriggerBatch(
                        batch=batch,
                        analysis_completed_at=completed_at,
                    )
                )
                last_analysis_at = completed_at
                call_times.append(completed_at)
                now = completed_at
                continue
            next_wakeup = timing.reconsider_at
        else:
            next_wakeup = end

        if cursor < len(triggers):
            next_wakeup = min(next_wakeup, triggers[cursor].observed_at)
        if next_wakeup <= now:
            raise RuntimeError("触发回放时钟未前进")
        now = min(next_wakeup, end)

    unprocessed = len(pending) + len(triggers) - cursor
    limitations = [
        "EXTERNAL_EVENTS_ONLY",
        "FIXED_ANALYSIS_DURATION",
        "GLOBAL_CROSS_SYMBOL_ADMISSION_NOT_REPLAYED",
    ]
    if spec.plan.updated_at > start:
        limitations.append("PLAN_POSTDATES_REPLAY_START")
    payload = {
        "version": TRIGGER_REPLAY_VERSION,
        "spec": spec,
        "spec_hash": content_hash(spec),
        "event_dataset_id": event_dataset.manifest.dataset_id,
        "symbol": symbol,
        "replay_start": start,
        "replay_end": end,
        "source_event_count": len(triggers),
        "accepted_trigger_count": accepted,
        "rejected_trigger_count": rejected,
        "capacity_dropped_count": capacity_dropped,
        "expired_trigger_count": expired,
        "unprocessed_trigger_count": unprocessed,
        "batches": tuple(batches),
        "limitations": tuple(limitations),
    }
    return ExternalTriggerReplay(
        replay_id=stable_id("external_trigger_replay", content_hash(payload)),
        **payload,
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
    return _require_utc(parsed)
