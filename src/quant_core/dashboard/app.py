"""Starlette 应用：只读 JSON 端点 + SSE + 前端静态资源。

每个端点都是「读事实 → 投影 DTO → JSON」，无写路径、无控制动作。DB 调用放到线程池，
避免阻塞事件循环。构造只需 ``AppConfig`` 与数据库 URL。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.staticfiles import StaticFiles

from quant_core.config import AppConfig
from quant_core.dashboard import serializers as ser
from quant_core.dashboard.health import assemble_health
from quant_core.dashboard.read_models import DashboardReader
from quant_core.dashboard.resources import prime_cpu_sampler, sample_host_resources
from quant_core.dashboard.stream import refresh_events
from quant_core.persistence import build_engine

_DEFAULT_LIMIT = 30
_MAX_LIMIT = 100
_EQUITY_WINDOWS = {"6h": 6, "24h": 24, "7d": 168, "30d": 720}


def create_app(
    config: AppConfig,
    database_url: str,
    *,
    web_dist: Path | None = None,
    stream_interval_seconds: float = 3.0,
) -> Starlette:
    engine = build_engine(database_url)
    reader = DashboardReader(engine, config)
    prime_cpu_sampler()

    async def health(_request: Request) -> JSONResponse:
        now = datetime.now(UTC)
        data = await run_in_threadpool(assemble_health, reader, config, now=now)
        return JSONResponse(
            {
                "stage": config.deployment.stage,
                "pipeline_version": config.pipeline.version,
                "server_time": now.isoformat(),
                **data,
            }
        )

    async def cycles(request: Request) -> JSONResponse:
        before = _parse_before(request)
        limit = _parse_limit(request)
        rows = await run_in_threadpool(reader.list_cycles, before=before, limit=limit)
        return JSONResponse({"cycles": [ser.cycle_row(row) for row in rows]})

    async def cycle_detail(request: Request) -> JSONResponse:
        cycle_id = request.path_params["cycle_id"]
        facts = await run_in_threadpool(reader.get_cycle, cycle_id)
        if facts is None:
            return JSONResponse({"error": "周期不存在"}, status_code=404)
        return JSONResponse(ser.cycle_detail(facts))

    async def events(request: Request) -> JSONResponse:
        before = _parse_before(request)
        limit = _parse_limit(request)
        found = await run_in_threadpool(reader.list_events, before=before, limit=limit)
        return JSONResponse({"events": [ser.world_event(event) for event in found]})

    async def positions(_request: Request) -> JSONResponse:
        records = await run_in_threadpool(reader.open_positions)
        marks = await run_in_threadpool(reader.latest_prices)
        sides = await run_in_threadpool(
            reader.entry_sides, [record.lifecycle.cycle_id for record in records]
        )
        return JSONResponse(
            {
                "positions": [
                    ser.position(
                        record,
                        mark=marks.get(record.lifecycle.symbol),
                        side=sides.get(record.lifecycle.cycle_id),
                    )
                    for record in records
                ]
            }
        )

    async def equity(request: Request) -> JSONResponse:
        window_key = request.query_params.get("window", "24h")
        hours = _EQUITY_WINDOWS.get(window_key, 24)
        now = datetime.now(UTC)
        window = await run_in_threadpool(reader.equity_window, now=now, hours=hours)
        return JSONResponse({"window": window_key, **ser.equity(window)})

    async def accounts(_request: Request) -> JSONResponse:
        now = datetime.now(UTC)
        statuses = await run_in_threadpool(reader.accounts, now=now)
        calls = await run_in_threadpool(reader.ai_calls_last_hour, now=now)
        return JSONResponse(
            {
                "accounts": [ser.account_status(status) for status in statuses],
                "hourly_budget": {
                    "used": calls,
                    "cap": config.trigger.maximum_ai_calls_per_hour,
                    "minimum_interval_seconds": config.trigger.minimum_call_interval_seconds,
                },
            }
        )

    async def resources(_request: Request) -> JSONResponse:
        data = await run_in_threadpool(sample_host_resources)
        return JSONResponse(data)

    async def reconciliation(_request: Request) -> JSONResponse:
        now = datetime.now(UTC)
        report = await run_in_threadpool(reader.latest_reconciliation, now=now)
        return JSONResponse({"reconciliation": ser.reconciliation(report)})

    async def stream(_request: Request) -> StreamingResponse:
        return StreamingResponse(
            refresh_events(interval_seconds=stream_interval_seconds),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    routes = [
        Route("/api/health", health),
        Route("/api/cycles", cycles),
        Route("/api/cycles/{cycle_id}", cycle_detail),
        Route("/api/events", events),
        Route("/api/positions", positions),
        Route("/api/equity", equity),
        Route("/api/accounts", accounts),
        Route("/api/resources", resources),
        Route("/api/reconciliation", reconciliation),
        Route("/api/stream", stream),
    ]
    app = Starlette(routes=routes)
    if web_dist is not None and web_dist.is_dir():
        app.mount("/", StaticFiles(directory=str(web_dist), html=True), name="web")
    return app


def _parse_limit(request: Request) -> int:
    raw = request.query_params.get("limit")
    if raw is None:
        return _DEFAULT_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_LIMIT
    return max(1, min(_MAX_LIMIT, value))


def _parse_before(request: Request) -> datetime | None:
    raw = request.query_params.get("before")
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
