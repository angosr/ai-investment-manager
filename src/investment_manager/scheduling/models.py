from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.scheduling.policy import TriggerPolicy


class TriggerReason(StrEnum):
    HEARTBEAT = "HEARTBEAT"
    EVENT_BATCH = "EVENT_BATCH"
    AGENT_WAKEUP = "AGENT_WAKEUP"
    POSITION_RECHECK = "POSITION_RECHECK"


class TriggerDecision(FrozenModel):
    should_run: bool
    reason: TriggerReason
    evidence_ids: tuple[str, ...] = ()


class AnalysisCallAdmission(FrozenModel):
    admitted: bool
    admitted_at: datetime | None = None
    retry_at: datetime | None = None

    _utc_admitted_at = field_validator("admitted_at")(
        lambda value: require_utc(value) if value is not None else None
    )
    _utc_retry_at = field_validator("retry_at")(
        lambda value: require_utc(value) if value is not None else None
    )

    @model_validator(mode="after")
    def result_has_exactly_one_time(self):
        if self.admitted != (self.admitted_at is not None and self.retry_at is None):
            raise ValueError("调用准入结果必须且只能包含对应时间")
        if not self.admitted and self.retry_at is None:
            raise ValueError("调用被延迟时必须给出 retry_at")
        return self


class AnalysisDispatchRequest(FrozenModel):
    """Infrastructure-neutral handoff from trigger admission to one Workflow."""

    workflow_name: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    task_queue: str = Field(min_length=1)
    payload: dict[str, Any]

    @model_validator(mode="after")
    def payload_identity_must_match(self):
        if self.payload.get("workflow_id") != self.workflow_id:
            raise ValueError("Workflow dispatch 的 payload/workflow_id 不一致")
        return self


def decide_analysis_call_admission(
    *,
    requested_at: datetime,
    last_admitted_at: datetime | None,
    minimum_call_interval_seconds: int,
) -> AnalysisCallAdmission:
    """Pure global de-duplication interval shared by production and replay."""

    requested_at = require_utc(requested_at)
    if minimum_call_interval_seconds < 0:
        raise ValueError("minimum_call_interval_seconds 不能为负数")
    last = require_utc(last_admitted_at) if last_admitted_at is not None else None
    if last is not None and last > requested_at:
        raise ValueError("last_admitted_at 不能晚于 requested_at")
    retry_at = (
        max(requested_at, last + timedelta(seconds=minimum_call_interval_seconds))
        if last is not None
        else requested_at
    )
    if retry_at > requested_at:
        return AnalysisCallAdmission(admitted=False, retry_at=retry_at)
    return AnalysisCallAdmission(admitted=True, admitted_at=requested_at)


class AnalysisTriggerType(StrEnum):
    CANONICAL_FACT_REVISED = "CANONICAL_FACT_REVISED"
    INTELLIGENCE_INSERTED = "INTELLIGENCE_INSERTED"
    MARKET_SHOCK = "MARKET_SHOCK"
    POSITION_RECHECK = "POSITION_RECHECK"
    AGENT_WAKEUP = "AGENT_WAKEUP"
    HEARTBEAT = "HEARTBEAT"
    WORLD_MODEL_UPDATED = "WORLD_MODEL_UPDATED"


class TriggerOutboxKind(StrEnum):
    TRIGGER_CREATED = "TRIGGER_CREATED"
    PLAN_REVISED = "PLAN_REVISED"


class AnalysisTriggerEvent(FrozenModel):
    trigger_id: str
    trigger_type: AnalysisTriggerType
    symbol: str
    pipeline_id: str
    occurred_at: datetime
    observed_at: datetime
    priority: int = Field(ge=0, le=100)
    dedup_key: str
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=100)
    review_reason: str | None = Field(default=None, min_length=1, max_length=500)
    expires_at: datetime | None = None
    plan_revision: int | None = Field(default=None, ge=1)

    _utc_occurred_at = field_validator("occurred_at")(require_utc)
    _utc_observed_at = field_validator("observed_at")(require_utc)

    @field_validator("expires_at")
    @classmethod
    def expires_at_must_be_utc(cls, value: datetime | None) -> datetime | None:
        return require_utc(value) if value is not None else None

    @model_validator(mode="after")
    def fact_and_identity_must_be_consistent(self):
        if self.observed_at < self.occurred_at:
            raise ValueError("触发 observed_at 不能早于 occurred_at")
        if self.expires_at is not None and self.expires_at <= self.observed_at:
            raise ValueError("触发 expires_at 必须晚于 observed_at")
        expected = stable_id(
            "analysis_trigger",
            self.trigger_type.value,
            self.symbol,
            self.pipeline_id,
            self.dedup_key,
        )
        if self.trigger_id != expected:
            raise ValueError("AnalysisTriggerEvent trigger_id 与事实身份不一致")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("触发 evidence_ids 不得重复")
        if (
            self.trigger_type != AnalysisTriggerType.AGENT_WAKEUP
            and self.review_reason is not None
        ):
            raise ValueError("只有 AGENT_WAKEUP 可以包含 review_reason")
        return self


def build_trigger_event(
    *,
    trigger_type: AnalysisTriggerType,
    symbol: str,
    pipeline_id: str,
    occurred_at: datetime,
    observed_at: datetime,
    priority: int,
    dedup_key: str,
    evidence_ids: tuple[str, ...] = (),
    review_reason: str | None = None,
    expires_at: datetime | None = None,
    plan_revision: int | None = None,
) -> AnalysisTriggerEvent:
    if (trigger_type == AnalysisTriggerType.AGENT_WAKEUP) != (
        review_reason is not None
    ):
        raise ValueError("新 AGENT_WAKEUP 必须且只能携带 review_reason")
    return AnalysisTriggerEvent(
        trigger_id=stable_id(
            "analysis_trigger",
            trigger_type.value,
            symbol,
            pipeline_id,
            dedup_key,
        ),
        trigger_type=trigger_type,
        symbol=symbol,
        pipeline_id=pipeline_id,
        occurred_at=occurred_at,
        observed_at=observed_at,
        priority=priority,
        dedup_key=dedup_key,
        evidence_ids=tuple(sorted(evidence_ids)),
        review_reason=review_reason,
        expires_at=expires_at,
        plan_revision=plan_revision,
    )


class AnalysisEventRule(FrozenModel):
    rule_id: str
    trigger_type: AnalysisTriggerType
    enabled: bool = True
    minimum_priority: int = Field(default=0, ge=0, le=100)
    coalesce_seconds: int = Field(default=0, ge=0, le=3600)
    ordinary_cooldown_seconds: int = Field(default=0, ge=0, le=3600)

    @model_validator(mode="after")
    def only_external_events_are_subscribable(self):
        if self.trigger_type in {
            AnalysisTriggerType.AGENT_WAKEUP,
            AnalysisTriggerType.HEARTBEAT,
            AnalysisTriggerType.WORLD_MODEL_UPDATED,
        }:
            raise ValueError("AGENT_WAKEUP 与 HEARTBEAT 不通过事件规则订阅")
        return self


class ScheduledWakeup(FrozenModel):
    wakeup_id: str
    wake_at: datetime
    expires_at: datetime
    reason: str = Field(min_length=1, max_length=500)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=100)
    hypothesis: str = Field(default="", max_length=2_000)
    required_freshness_seconds: int = Field(default=180, ge=1, le=86_400)

    _utc_wake_at = field_validator("wake_at")(require_utc)
    _utc_expires_at = field_validator("expires_at")(require_utc)

    @model_validator(mode="after")
    def window_must_be_valid(self):
        if self.expires_at <= self.wake_at:
            raise ValueError("ScheduledWakeup expires_at 必须晚于 wake_at")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("ScheduledWakeup evidence_ids 不得重复")
        return self


class TriggerPlanOrigin(StrEnum):
    UNKNOWN = "UNKNOWN"
    INITIAL_DEFAULT = "INITIAL_DEFAULT"
    CARRIED_FORWARD = "CARRIED_FORWARD"
    RELEASE_REBOUND = "RELEASE_REBOUND"
    DYNAMIC_PATCH = "DYNAMIC_PATCH"


class AnalysisTriggerPlan(FrozenModel):
    plan_id: str
    revision: int = Field(ge=1)
    symbol: str
    pipeline_id: str
    manifest_id: str
    ai_paused: bool = False
    heartbeat_seconds: int | None = Field(default=None, ge=1, le=86_400)
    event_rules: tuple[AnalysisEventRule, ...] = ()
    scheduled_wakeups: tuple[ScheduledWakeup, ...] = ()
    updated_at: datetime
    applied_patch_id: str | None = None
    origin: TriggerPlanOrigin = TriggerPlanOrigin.UNKNOWN

    _utc_updated_at = field_validator("updated_at")(require_utc)

    @model_validator(mode="after")
    def identity_and_members_must_be_unique(self):
        expected = stable_id("analysis_trigger_plan", self.symbol, self.pipeline_id)
        if self.plan_id != expected:
            raise ValueError("AnalysisTriggerPlan plan_id 与作用域不一致")
        rule_ids = [item.rule_id for item in self.event_rules]
        enabled_rule_levels = [
            (item.trigger_type, item.minimum_priority) for item in self.event_rules if item.enabled
        ]
        wakeup_ids = [item.wakeup_id for item in self.scheduled_wakeups]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("AnalysisTriggerPlan rule_id 不得重复")
        if len(enabled_rule_levels) != len(set(enabled_rule_levels)):
            raise ValueError("同类启用事件规则的 minimum_priority 不得重复")
        if len(wakeup_ids) != len(set(wakeup_ids)):
            raise ValueError("AnalysisTriggerPlan wakeup_id 不得重复")
        return self


def build_initial_trigger_plan(
    *,
    symbol: str,
    pipeline_id: str,
    manifest_id: str,
    updated_at: datetime,
    heartbeat_seconds: int | None,
    event_rules: tuple[AnalysisEventRule, ...] = (),
) -> AnalysisTriggerPlan:
    return AnalysisTriggerPlan(
        plan_id=stable_id("analysis_trigger_plan", symbol, pipeline_id),
        revision=1,
        symbol=symbol,
        pipeline_id=pipeline_id,
        manifest_id=manifest_id,
        heartbeat_seconds=heartbeat_seconds,
        event_rules=event_rules,
        updated_at=updated_at,
        origin=TriggerPlanOrigin.INITIAL_DEFAULT,
    )


def carry_forward_trigger_plan(
    previous: AnalysisTriggerPlan,
    *,
    pipeline_id: str,
    manifest_id: str,
    updated_at: datetime,
) -> AnalysisTriggerPlan:
    """发布新代际时保留主 Agent 动态计划，并重新绑定新身份。"""

    updated_at = require_utc(updated_at)
    return AnalysisTriggerPlan(
        plan_id=stable_id("analysis_trigger_plan", previous.symbol, pipeline_id),
        revision=1,
        symbol=previous.symbol,
        pipeline_id=pipeline_id,
        manifest_id=manifest_id,
        ai_paused=previous.ai_paused,
        heartbeat_seconds=previous.heartbeat_seconds,
        event_rules=previous.event_rules,
        scheduled_wakeups=tuple(
            item for item in previous.scheduled_wakeups if item.expires_at > updated_at
        ),
        updated_at=updated_at,
        origin=TriggerPlanOrigin.CARRIED_FORWARD,
    )


def rebind_trigger_plan_manifest(
    current: AnalysisTriggerPlan,
    *,
    manifest_id: str,
    updated_at: datetime,
) -> AnalysisTriggerPlan:
    """Bind a new deployment to unchanged scheduling behavior and state."""

    updated_at = require_utc(updated_at)
    if current.manifest_id == manifest_id:
        return current
    return current.model_copy(
        update={
            "revision": current.revision + 1,
            "manifest_id": manifest_id,
            "scheduled_wakeups": tuple(
                item for item in current.scheduled_wakeups if item.expires_at > updated_at
            ),
            "updated_at": updated_at,
            "applied_patch_id": None,
            "origin": TriggerPlanOrigin.RELEASE_REBOUND,
        }
    )


class AddWakeup(FrozenModel):
    operation: Literal["ADD_WAKEUP"] = "ADD_WAKEUP"
    wakeup: ScheduledWakeup


class UpdateWakeup(FrozenModel):
    operation: Literal["UPDATE_WAKEUP"] = "UPDATE_WAKEUP"
    wakeup: ScheduledWakeup


class DeleteWakeup(FrozenModel):
    operation: Literal["DELETE_WAKEUP"] = "DELETE_WAKEUP"
    wakeup_id: str


class TriggerNow(FrozenModel):
    operation: Literal["TRIGGER_NOW"] = "TRIGGER_NOW"
    request_id: str
    reason: str = Field(min_length=1, max_length=500)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=100)


class UpsertEventRule(FrozenModel):
    operation: Literal["UPSERT_EVENT_RULE"] = "UPSERT_EVENT_RULE"
    rule: AnalysisEventRule


class DeleteEventRule(FrozenModel):
    operation: Literal["DELETE_EVENT_RULE"] = "DELETE_EVENT_RULE"
    rule_id: str


class SetAiPaused(FrozenModel):
    operation: Literal["SET_AI_PAUSED"] = "SET_AI_PAUSED"
    paused: bool


class SetHeartbeat(FrozenModel):
    operation: Literal["SET_HEARTBEAT"] = "SET_HEARTBEAT"
    heartbeat_seconds: int | None = Field(default=None, ge=1, le=86_400)


TriggerPlanOperation = Annotated[
    AddWakeup
    | UpdateWakeup
    | DeleteWakeup
    | TriggerNow
    | UpsertEventRule
    | DeleteEventRule
    | SetAiPaused
    | SetHeartbeat,
    Field(discriminator="operation"),
]


class TriggerPlanPatch(FrozenModel):
    patch_id: str
    plan_id: str
    expected_revision: int = Field(ge=1)
    manifest_id: str
    submitted_at: datetime
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=100)
    operations: tuple[TriggerPlanOperation, ...] = Field(min_length=1, max_length=100)

    _utc_submitted_at = field_validator("submitted_at")(require_utc)

    @model_validator(mode="after")
    def identity_must_match(self):
        expected = stable_id("trigger_plan_patch", content_hash(_patch_identity(self)))
        if self.patch_id != expected:
            raise ValueError("TriggerPlanPatch patch_id 与内容不一致")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("TriggerPlanPatch evidence_ids 不得重复")
        return self


def build_trigger_plan_patch(
    *,
    plan: AnalysisTriggerPlan,
    submitted_at: datetime,
    operations: tuple[TriggerPlanOperation, ...],
    evidence_ids: tuple[str, ...] = (),
) -> TriggerPlanPatch:
    normalized = TriggerPlanPatch.model_construct(
        patch_id="pending",
        plan_id=plan.plan_id,
        expected_revision=plan.revision,
        manifest_id=plan.manifest_id,
        submitted_at=require_utc(submitted_at),
        evidence_ids=tuple(sorted(evidence_ids)),
        operations=operations,
    )
    return TriggerPlanPatch(
        **normalized.model_dump(exclude={"patch_id"}),
        patch_id=stable_id("trigger_plan_patch", content_hash(_patch_identity(normalized))),
    )


def _patch_identity(patch: TriggerPlanPatch) -> dict:
    return {
        "plan_id": patch.plan_id,
        "expected_revision": patch.expected_revision,
        "manifest_id": patch.manifest_id,
        "submitted_at": patch.submitted_at.isoformat(),
        "evidence_ids": sorted(patch.evidence_ids),
        "operations": [item.model_dump(mode="json") for item in patch.operations],
    }


class TriggerPlanApplyResult(FrozenModel):
    plan: AnalysisTriggerPlan
    emitted_triggers: tuple[AnalysisTriggerEvent, ...] = ()


class TriggerBatch(FrozenModel):
    batch_id: str
    symbol: str
    pipeline_id: str
    plan_revision: int = Field(ge=1)
    created_at: datetime
    deadline: datetime
    triggers: tuple[AnalysisTriggerEvent, ...] = Field(min_length=1, max_length=1000)

    _utc_created_at = field_validator("created_at")(require_utc)
    _utc_deadline = field_validator("deadline")(require_utc)

    @model_validator(mode="after")
    def identity_and_scope_must_match(self):
        if self.deadline <= self.created_at:
            raise ValueError("TriggerBatch deadline 必须晚于 created_at")
        ordered = tuple(sorted(self.triggers, key=lambda item: item.trigger_id))
        if self.triggers != ordered:
            raise ValueError("TriggerBatch triggers 必须按 trigger_id 排序")
        if any(
            item.symbol != self.symbol or item.pipeline_id != self.pipeline_id
            for item in self.triggers
        ):
            raise ValueError("TriggerBatch 内触发作用域不一致")
        expected = stable_id(
            "trigger_batch",
            self.symbol,
            self.pipeline_id,
            self.plan_revision,
            *(item.trigger_id for item in self.triggers),
            self.deadline.isoformat(),
        )
        if self.batch_id != expected:
            raise ValueError("TriggerBatch batch_id 与内容不一致")
        return self


@dataclass(frozen=True, slots=True)
class TriggerTiming:
    """Pure scheduler result shared by Temporal and historical replay."""

    reconsider_at: datetime


def trigger_plan_accepts(
    plan: Mapping[str, Any], trigger: Mapping[str, Any]
) -> bool:
    trigger_type = trigger.get("trigger_type")
    if trigger_type == AnalysisTriggerType.AGENT_WAKEUP.value:
        return True
    if bool(plan.get("ai_paused")):
        return False
    if trigger_type in {
        AnalysisTriggerType.HEARTBEAT.value,
        AnalysisTriggerType.WORLD_MODEL_UPDATED.value,
    }:
        return True
    return any(
        rule.get("enabled")
        and rule.get("trigger_type") == trigger_type
        and int(trigger.get("priority", 0)) >= int(rule.get("minimum_priority", 0))
        for rule in plan.get("event_rules", [])
    )


def trigger_rule_value(
    plan: Mapping[str, Any],
    trigger: Mapping[str, Any],
    field: Literal["coalesce_seconds", "ordinary_cooldown_seconds"],
) -> int:
    priority = int(trigger.get("priority", 0))
    matches = [
        rule
        for rule in plan.get("event_rules", [])
        if rule.get("trigger_type") == trigger.get("trigger_type")
        and rule.get("enabled")
        and priority >= int(rule.get("minimum_priority", 0))
    ]
    if not matches:
        return 0
    most_specific = max(matches, key=lambda rule: int(rule.get("minimum_priority", 0)))
    return int(most_specific.get(field, 0))


def trigger_reconsideration(
    *,
    plan: Mapping[str, Any],
    pending: Sequence[Mapping[str, Any]],
    now: datetime,
    last_analysis_at: datetime | None,
    input_retry_not_before: datetime | None,
    wake_at_expiry: bool,
) -> TriggerTiming:
    """Return the next wake time; reaching an expiry means discard, not execute."""

    current = require_utc(now)
    if not pending:
        raise ValueError("触发重算至少需要一个待处理事件")
    last = require_utc(last_analysis_at) if last_analysis_at is not None else None
    retry_at = (
        require_utc(input_retry_not_before)
        if input_retry_not_before is not None
        else None
    )
    earliest = current
    if retry_at is not None:
        earliest = max(earliest, retry_at)
    if int(pending[0].get("priority", 0)) < 100:
        first_seen = min(_trigger_payload_time(item["observed_at"]) for item in pending)
        coalesce = max(
            trigger_rule_value(plan, item, "coalesce_seconds") for item in pending
        )
        earliest = max(earliest, first_seen + timedelta(seconds=coalesce))
        if last is not None:
            cooldown = max(
                trigger_rule_value(plan, item, "ordinary_cooldown_seconds")
                for item in pending
            )
            earliest = max(earliest, last + timedelta(seconds=cooldown))
    if wake_at_expiry:
        expiries = [
            _trigger_payload_time(item["expires_at"])
            for item in pending
            if item.get("expires_at") is not None
        ]
        if expiries:
            earliest = min(earliest, min(expiries))
    return TriggerTiming(reconsider_at=max(earliest, current))


def _trigger_payload_time(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return require_utc(parsed)


def build_trigger_batch(
    *,
    plan: AnalysisTriggerPlan,
    triggers: tuple[AnalysisTriggerEvent, ...],
    created_at: datetime,
    deadline: datetime,
) -> TriggerBatch:
    ordered = tuple(sorted(triggers, key=lambda item: item.trigger_id))
    return TriggerBatch(
        batch_id=stable_id(
            "trigger_batch",
            plan.symbol,
            plan.pipeline_id,
            plan.revision,
            *(item.trigger_id for item in ordered),
            require_utc(deadline).isoformat(),
        ),
        symbol=plan.symbol,
        pipeline_id=plan.pipeline_id,
        plan_revision=plan.revision,
        created_at=created_at,
        deadline=deadline,
        triggers=ordered,
    )


class TriggerPlanGate:
    """只执行调度契约和硬资源边界，不替主 Agent 判断经济价值。"""

    def __init__(self, policy: TriggerPolicy) -> None:
        self._policy = policy

    def apply(
        self,
        current: AnalysisTriggerPlan,
        patch: TriggerPlanPatch,
        *,
        now: datetime,
        current_manifest_id: str,
    ) -> TriggerPlanApplyResult:
        now = require_utc(now)
        if patch.plan_id != current.plan_id:
            raise ValueError("TriggerPlanPatch 目标计划不一致")
        if patch.expected_revision != current.revision:
            raise ValueError("TriggerPlanPatch revision 已过期")
        if patch.manifest_id != current.manifest_id or patch.manifest_id != current_manifest_id:
            raise ValueError("TriggerPlanPatch 不属于当前 Manifest")
        if patch.submitted_at > now:
            raise ValueError("TriggerPlanPatch submitted_at 不能来自未来")

        wakeups = {item.wakeup_id: item for item in current.scheduled_wakeups}
        rules = {item.rule_id: item for item in current.event_rules}
        paused = current.ai_paused
        heartbeat = current.heartbeat_seconds
        emitted: list[AnalysisTriggerEvent] = []
        for operation in patch.operations:
            if isinstance(operation, AddWakeup):
                if operation.wakeup.wake_at <= now:
                    raise ValueError("ADD_WAKEUP 目标必须位于未来")
                if operation.wakeup.wakeup_id in wakeups:
                    raise ValueError("ADD_WAKEUP 目标已经存在")
                wakeups[operation.wakeup.wakeup_id] = operation.wakeup
            elif isinstance(operation, UpdateWakeup):
                if operation.wakeup.wake_at <= now:
                    raise ValueError("UPDATE_WAKEUP 目标必须位于未来")
                if operation.wakeup.wakeup_id not in wakeups:
                    raise ValueError("UPDATE_WAKEUP 目标不存在")
                wakeups[operation.wakeup.wakeup_id] = operation.wakeup
            elif isinstance(operation, DeleteWakeup):
                if wakeups.pop(operation.wakeup_id, None) is None:
                    raise ValueError("DELETE_WAKEUP 目标不存在")
            elif isinstance(operation, TriggerNow):
                emitted.append(
                    build_trigger_event(
                        trigger_type=AnalysisTriggerType.AGENT_WAKEUP,
                        symbol=current.symbol,
                        pipeline_id=current.pipeline_id,
                        occurred_at=patch.submitted_at,
                        observed_at=now,
                        priority=100,
                        dedup_key=operation.request_id,
                        evidence_ids=operation.evidence_ids,
                        review_reason=operation.reason,
                        expires_at=now + timedelta(seconds=self._policy.trigger_expiry_seconds),
                        plan_revision=current.revision + 1,
                    )
                )
            elif isinstance(operation, UpsertEventRule):
                rules[operation.rule.rule_id] = operation.rule
            elif isinstance(operation, DeleteEventRule):
                if rules.pop(operation.rule_id, None) is None:
                    raise ValueError("DELETE_EVENT_RULE 目标不存在")
            elif isinstance(operation, SetAiPaused):
                paused = operation.paused
            elif isinstance(operation, SetHeartbeat):
                heartbeat = operation.heartbeat_seconds

        retained_wakeups = tuple(
            sorted(
                (item for item in wakeups.values() if item.expires_at > now),
                key=lambda item: (item.wake_at, item.wakeup_id),
            )
        )
        if len(retained_wakeups) > self._policy.maximum_scheduled_wakeups:
            raise ValueError("计划唤醒点超过硬上限")
        if len(rules) > self._policy.maximum_event_rules:
            raise ValueError("事件规则超过硬上限")
        if heartbeat is not None and heartbeat < self._policy.minimum_call_interval_seconds:
            raise ValueError("Heartbeat 短于硬最小调用间隔")
        if len({item.trigger_id for item in emitted}) != len(emitted):
            raise ValueError("同一 Patch 的 TRIGGER_NOW request_id 不得重复")
        planned_calls = sorted(
            [item.wake_at for item in retained_wakeups if item.wake_at > now]
            + [now for _item in emitted]
        )
        for left, right in pairwise(planned_calls):
            if (right - left).total_seconds() < self._policy.minimum_call_interval_seconds:
                raise ValueError("计划调用点短于硬最小调用间隔")
        revised = AnalysisTriggerPlan(
            plan_id=current.plan_id,
            revision=current.revision + 1,
            symbol=current.symbol,
            pipeline_id=current.pipeline_id,
            manifest_id=current.manifest_id,
            ai_paused=paused,
            heartbeat_seconds=heartbeat,
            event_rules=tuple(sorted(rules.values(), key=lambda item: item.rule_id)),
            scheduled_wakeups=retained_wakeups,
            updated_at=now,
            applied_patch_id=patch.patch_id,
            origin=TriggerPlanOrigin.DYNAMIC_PATCH,
        )
        return TriggerPlanApplyResult(plan=revised, emitted_triggers=tuple(emitted))
