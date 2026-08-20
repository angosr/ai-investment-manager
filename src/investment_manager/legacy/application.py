from __future__ import annotations

from datetime import datetime, timedelta

from investment_manager.legacy.cycle import CycleInput
from investment_manager.legacy.orchestration import (
    WorkflowExecution,
    build_workflow_request,
)
from investment_manager.legacy.runtime import TemporalAnalysisCoordinator
from investment_manager.scheduling.models import TriggerDecision, TriggerReason
from investment_manager.scheduling.policy import TemporalPolicy


async def submit_frozen_analysis(
    *,
    cycle_input: CycleInput,
    temporal_policy: TemporalPolicy,
    created_at: datetime,
    deadline_minutes: int,
) -> WorkflowExecution:
    """Submit one diagnostic frozen input through the authoritative old workflow."""

    request = build_workflow_request(
        cycle_input=cycle_input,
        trigger=TriggerDecision(should_run=True, reason=TriggerReason.HEARTBEAT),
        temporal_policy=temporal_policy,
        created_at=created_at,
        deadline=created_at + timedelta(minutes=deadline_minutes),
    )
    coordinator = await TemporalAnalysisCoordinator.connect(temporal_policy)
    return await coordinator.execute(request)
