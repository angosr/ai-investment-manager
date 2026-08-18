"""SSE：向前端推送「该刷新了」的轻量信号，前端据此重取受影响端点。

v1 用固定间隔 tick（端点都很轻，重取成本低）。可靠性不依赖推送——断线时前端按上次
数据显示「实时中断」，重连后自然恢复。低延迟 NOTIFY 唤醒是后续增强，不改变契约。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator


async def refresh_events(*, interval_seconds: float) -> AsyncIterator[bytes]:
    sequence = 0
    while True:
        sequence += 1
        payload = json.dumps({"seq": sequence})
        yield f"event: refresh\ndata: {payload}\n\n".encode()
        await asyncio.sleep(interval_seconds)
