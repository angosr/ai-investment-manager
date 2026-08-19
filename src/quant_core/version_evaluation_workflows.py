from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from quant_core.temporal_compat import default_activity_versioning_intent

RUN_EVALUATION_STAGE_ACTIVITY = "run-version-evaluation-stage-v1"
FINALIZE_EVALUATION_ACTIVITY = "finalize-version-evaluation-v1"


@workflow.defn(name="VersionEvaluationWorkflow")
class VersionEvaluationWorkflow:
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
            stages = request["target"]["plan"]["required_stages"]
            orchestration = request["orchestration"]
            retry_policy = RetryPolicy(
                initial_interval=timedelta(seconds=int(orchestration["retry_initial_seconds"])),
                maximum_interval=timedelta(seconds=int(orchestration["retry_maximum_seconds"])),
                backoff_coefficient=float(orchestration["retry_backoff_coefficient"]),
                maximum_attempts=int(orchestration["retry_maximum_attempts"]),
                non_retryable_error_types=["InvalidEvaluationInput"],
            )
            start_to_close = timedelta(
                seconds=int(orchestration["activity_start_to_close_seconds"])
            )
            schedule_to_close = timedelta(
                seconds=int(orchestration["activity_schedule_to_close_seconds"])
            )
            if not isinstance(stages, list) or not stages:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            return _failed(workflow_id, "INVALID_WORKFLOW_INPUT")

        results: list[dict[str, Any]] = []
        try:
            for stage in stages:
                raw_result = await workflow.execute_activity(
                    RUN_EVALUATION_STAGE_ACTIVITY,
                    {"request": request, "stage": stage},
                    result_type=dict,
                    start_to_close_timeout=start_to_close,
                    schedule_to_close_timeout=schedule_to_close,
                    retry_policy=retry_policy,
                    versioning_intent=default_activity_versioning_intent(),
                    summary=f"运行预登记评估阶段 {stage}",
                )
                stage_result = raw_result.get("stage_result")
                if not isinstance(stage_result, dict):
                    return _failed(workflow_id, "INVALID_STAGE_RESULT")
                results.append(stage_result)
                if stage_result.get("outcome") != "PASSED":
                    break
            finalized = await workflow.execute_activity(
                FINALIZE_EVALUATION_ACTIVITY,
                {
                    "request": request,
                    "stage_results": results,
                    "completed_at": workflow.now().isoformat(),
                },
                result_type=dict,
                start_to_close_timeout=start_to_close,
                schedule_to_close_timeout=schedule_to_close,
                retry_policy=retry_policy,
                versioning_intent=default_activity_versioning_intent(),
                summary="冻结并登记版本评估结果",
            )
        except ActivityError:
            return _failed(workflow_id, "EVALUATION_ACTIVITY_FAILED")
        evaluation = finalized.get("evaluation")
        if not isinstance(evaluation, dict):
            return _failed(workflow_id, "INVALID_EVALUATION_RESULT")
        return {
            "workflow_id": workflow_id,
            "status": "COMPLETED",
            "reason_code": (
                "ALL_REQUIRED_STAGES_PASSED"
                if len(results) == len(stages)
                and all(item.get("outcome") == "PASSED" for item in results)
                else "EVALUATION_STAGE_FAILED"
            ),
            "evaluation": evaluation,
        }


def _failed(workflow_id: str, reason_code: str) -> dict[str, Any]:
    return {
        "workflow_id": workflow_id,
        "status": "FAILED",
        "reason_code": reason_code,
        "evaluation": None,
    }
