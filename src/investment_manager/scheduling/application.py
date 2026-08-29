from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from investment_manager.scheduling.models import (
    AnalysisEventRule,
    AnalysisTriggerType,
    SetHeartbeat,
    TriggerNow,
    TriggerPlanApplyResult,
    build_initial_trigger_plan,
    build_trigger_plan_patch,
    carry_forward_trigger_plan,
)
from investment_manager.scheduling.repository import SqlTriggerRepository


def ensure_trigger_plans(
    *,
    repository: SqlTriggerRepository,
    symbols: tuple[str, ...],
    pipeline_id: str,
    manifest_id: str,
    heartbeat_seconds: int,
    minimum_intelligence_review_priority: Decimal,
    debounce_seconds: int,
    now: datetime,
) -> None:
    """Create or verify one durable trigger plan per configured symbol and release."""

    previous_plans = repository.current_plans_for_symbols(symbols)
    for symbol in symbols:
        try:
            current = repository.plan_for_scope(
                symbol=symbol,
                pipeline_id=pipeline_id,
            )
        except KeyError:
            predecessors = tuple(item for item in previous_plans if item.symbol == symbol)
            if predecessors:
                plan = carry_forward_trigger_plan(
                    max(
                        predecessors,
                        key=lambda item: (item.updated_at, item.revision, item.pipeline_id),
                    ),
                    pipeline_id=pipeline_id,
                    manifest_id=manifest_id,
                    updated_at=now,
                )
            else:
                plan = build_initial_trigger_plan(
                    symbol=symbol,
                    pipeline_id=pipeline_id,
                    manifest_id=manifest_id,
                    updated_at=now,
                    heartbeat_seconds=heartbeat_seconds,
                    event_rules=(
                        AnalysisEventRule(
                            rule_id="canonical-fact-default",
                            trigger_type=AnalysisTriggerType.CANONICAL_FACT_REVISED,
                            minimum_priority=0,
                        ),
                        AnalysisEventRule(
                            rule_id="intelligence-default",
                            trigger_type=AnalysisTriggerType.INTELLIGENCE_INSERTED,
                            minimum_priority=int(minimum_intelligence_review_priority * 100),
                            coalesce_seconds=debounce_seconds,
                            ordinary_cooldown_seconds=debounce_seconds,
                        ),
                        AnalysisEventRule(
                            rule_id="market-shock-default",
                            trigger_type=AnalysisTriggerType.MARKET_SHOCK,
                            minimum_priority=0,
                        ),
                    ),
                )
            repository.create_plan(plan)
            current = plan
        if current.manifest_id != manifest_id:
            current = repository.rebind_manifest(
                plan_id=current.plan_id,
                manifest_id=manifest_id,
                updated_at=now,
            )


def trigger_now(
    *,
    repository: SqlTriggerRepository,
    symbol: str,
    pipeline_id: str,
    manifest_id: str,
    request_id: str,
    reason: str,
    now: datetime,
    evidence_ids: tuple[str, ...] = (),
) -> TriggerPlanApplyResult:
    """Apply an auditable immediate wake-up through the current durable plan."""

    plan = repository.plan_for_scope(symbol=symbol, pipeline_id=pipeline_id)
    return repository.apply_patch(
        build_trigger_plan_patch(
            plan=plan,
            submitted_at=now,
            operations=(
                TriggerNow(
                    request_id=request_id,
                    reason=reason,
                    evidence_ids=tuple(sorted(set(evidence_ids))),
                ),
            ),
        ),
        now=now,
        current_manifest_id=manifest_id,
    )


def set_trigger_heartbeat(
    *,
    repository: SqlTriggerRepository,
    symbol: str,
    pipeline_id: str,
    manifest_id: str,
    heartbeat_seconds: int | None,
    now: datetime,
) -> TriggerPlanApplyResult:
    """Apply one durable, auditable heartbeat change to the current plan."""

    plan = repository.plan_for_scope(symbol=symbol, pipeline_id=pipeline_id)
    if plan.manifest_id != manifest_id:
        raise ValueError("TriggerPlan 不属于当前 ReleaseManifest")
    if plan.heartbeat_seconds == heartbeat_seconds:
        return TriggerPlanApplyResult(plan=plan)
    return repository.apply_patch(
        build_trigger_plan_patch(
            plan=plan,
            submitted_at=now,
            operations=(SetHeartbeat(heartbeat_seconds=heartbeat_seconds),),
        ),
        now=now,
        current_manifest_id=manifest_id,
    )
