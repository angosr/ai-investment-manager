from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from investment_manager.temporal_compat import default_activity_versioning_intent

RECONCILIATION_ACTIVITY_NAME = "reconcile-trading-state-v1"


@workflow.defn(name="ReconciliationWorkflow")
class ReconciliationWorkflow:
    def __init__(self) -> None:
        self._input_hash: str | None = None

    @workflow.query
    def input_hash(self) -> str | None:
        return self._input_hash

    @workflow.run
    async def run(self, request: dict[str, Any]) -> dict[str, Any]:
        workflow_id = str(request.get("workflow_id") or workflow.info().workflow_id)
        raw_hash = request.get("input_hash")
        if not isinstance(raw_hash, str) or not raw_hash:
            return _failed(workflow_id, "INVALID_WORKFLOW_INPUT")
        self._input_hash = raw_hash
        try:
            orchestration = request["orchestration"]
            retry_policy = RetryPolicy(
                initial_interval=timedelta(seconds=int(orchestration["retry_initial_seconds"])),
                maximum_interval=timedelta(seconds=int(orchestration["retry_maximum_seconds"])),
                backoff_coefficient=float(orchestration["retry_backoff_coefficient"]),
                maximum_attempts=int(orchestration["retry_maximum_attempts"]),
                non_retryable_error_types=["InvalidReconciliationInput"],
            )
            start_to_close = timedelta(
                seconds=int(orchestration["activity_start_to_close_seconds"])
            )
            schedule_to_close = timedelta(
                seconds=int(orchestration["activity_schedule_to_close_seconds"])
            )
        except (KeyError, TypeError, ValueError):
            return _failed(workflow_id, "INVALID_WORKFLOW_INPUT")
        try:
            result = await workflow.execute_activity(
                RECONCILIATION_ACTIVITY_NAME,
                request,
                result_type=dict,
                start_to_close_timeout=start_to_close,
                schedule_to_close_timeout=schedule_to_close,
                retry_policy=retry_policy,
                versioning_intent=default_activity_versioning_intent(),
                summary="主动对账订单、成交、余额和仓位",
            )
        except ActivityError:
            return _failed(workflow_id, "RECONCILIATION_ACTIVITY_FAILED")
        report = result.get("report") if isinstance(result, dict) else None
        attempt = result.get("attempt") if isinstance(result, dict) else None
        if not isinstance(report, dict) or not isinstance(attempt, int) or attempt < 1:
            return _failed(workflow_id, "INVALID_ACTIVITY_RESULT")
        return {
            "workflow_id": workflow_id,
            "status": "COMPLETED",
            "reason_code": str(report.get("status", "UNKNOWN")),
            "attempt": attempt,
            "report": report,
        }


def _failed(workflow_id: str, reason_code: str) -> dict[str, Any]:
    return {
        "workflow_id": workflow_id,
        "status": "FAILED",
        "reason_code": reason_code,
        "attempt": 0,
        "report": None,
    }
