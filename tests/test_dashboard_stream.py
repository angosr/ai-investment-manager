from __future__ import annotations

import asyncio
import json

from investment_manager.entrypoints.dashboard.stream import FAST_TOPICS, SLOW_TOPICS, refresh_events


def test_refresh_stream_separates_fast_and_slow_topics() -> None:
    async def collect() -> list[dict]:
        stream = refresh_events(interval_seconds=0.001, slow_refresh_every_ticks=3)
        payloads = []
        for _ in range(4):
            frame = (await anext(stream)).decode()
            payloads.append(json.loads(frame.split("data: ", 1)[1]))
        await stream.aclose()
        return payloads

    first, second, third, fourth = asyncio.run(collect())

    assert first == {"seq": 1, "topics": [*FAST_TOPICS, *SLOW_TOPICS]}
    assert second == {"seq": 2, "topics": list(FAST_TOPICS)}
    assert third == {"seq": 3, "topics": [*FAST_TOPICS, *SLOW_TOPICS]}
    assert fourth == {"seq": 4, "topics": list(FAST_TOPICS)}
