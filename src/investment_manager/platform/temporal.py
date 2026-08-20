from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Self

from temporalio import workflow
from temporalio.client import Client
from temporalio.worker import Worker


def default_activity_versioning_intent() -> workflow.VersioningIntent | None:
    """让新 Activity 可跨 Worker 升级接手，同时保持旧历史可重放。"""

    if workflow.patched("activity-routing-default-v1"):
        return workflow.VersioningIntent.DEFAULT
    return None


class SingleActivityWorker:
    """单活动 Temporal Worker：单工作线程、单并发活动。

    汇总各领域 ``*TemporalWorker`` 中逐字重复的 executor + Worker 构造和异步上下文
    生命周期。此处不含任何 workflow-id 或内容哈希逻辑，workflow/activity 注册原样
    透传，因此不影响决定性重放或幂等边界；各领域子类只需提供自己的 task queue、
    workflow/activity 列表和线程名前缀。
    """

    def __init__(
        self,
        client: Client,
        *,
        task_queue: str,
        workflows: list[Any],
        activities: list[Any],
        thread_name_prefix: str,
        max_concurrent_activities: int = 1,
    ) -> None:
        if max_concurrent_activities < 1:
            raise ValueError("Activity 并发数必须为正数")
        self._executor = ThreadPoolExecutor(
            max_workers=max_concurrent_activities,
            thread_name_prefix=thread_name_prefix,
        )
        self._worker = Worker(
            client,
            task_queue=task_queue,
            workflows=workflows,
            activities=activities,
            activity_executor=self._executor,
            max_concurrent_activities=max_concurrent_activities,
        )

    async def __aenter__(self) -> Self:
        await self._worker.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            await self._worker.__aexit__(exc_type, exc, tb)
        finally:
            self._executor.shutdown(wait=True, cancel_futures=True)
