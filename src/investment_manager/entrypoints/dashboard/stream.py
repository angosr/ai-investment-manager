"""SSE：按主题推送轻量刷新信号，前端只重取受影响端点。

高时效状态走快速 tick，历史时间线与权益走低频 tick，避免每个浏览器按同一频率重读
全部事实。可靠性不依赖推送；EventSource 重连后会重新收到完整首帧。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

FAST_TOPICS = ("health", "capital", "accounts", "resources")
SLOW_TOPICS = ("cycles", "events", "equity")


async def refresh_events(
    *, interval_seconds: float, slow_refresh_every_ticks: int = 5
) -> AsyncIterator[bytes]:
    if interval_seconds <= 0:
        raise ValueError("SSE 刷新间隔必须为正数")
    if slow_refresh_every_ticks < 1:
        raise ValueError("SSE 慢速刷新倍数必须至少为 1")
    sequence = 0
    while True:
        sequence += 1
        topics = list(FAST_TOPICS)
        if sequence == 1 or sequence % slow_refresh_every_ticks == 0:
            topics.extend(SLOW_TOPICS)
        payload = json.dumps({"seq": sequence, "topics": topics}, separators=(",", ":"))
        yield f"event: refresh\ndata: {payload}\n\n".encode()
        await asyncio.sleep(interval_seconds)
