"""Temporal 历史兼容边界。"""

from temporalio import workflow


def default_activity_versioning_intent() -> workflow.VersioningIntent | None:
    """让新 Activity 可跨 Worker 升级接手，同时保持旧历史可重放。"""

    if workflow.patched("activity-routing-default-v1"):
        return workflow.VersioningIntent.DEFAULT
    return None
