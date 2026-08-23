"""Starlette 应用：只读 JSON 端点 + SSE + 前端静态资源。

每个端点都是「读事实 → 投影 DTO → JSON」，无写路径、无控制动作。DB 调用放到线程池，
避免阻塞事件循环。构造只需 ``AppConfig`` 与数据库 URL。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.staticfiles import StaticFiles
from temporalio.client import Client

from investment_manager.entrypoints.dashboard import serializers as ser
from investment_manager.entrypoints.dashboard.capital import (
    CapitalDashboardReader,
    serialize_capital_activity,
    serialize_capital_overview,
)
from investment_manager.entrypoints.dashboard.health import assemble_health
from investment_manager.entrypoints.dashboard.pagination import (
    InvalidPageCursor,
    PageCursor,
    decode_page_cursor,
    page_slice,
)
from investment_manager.entrypoints.dashboard.read_models import DashboardReader
from investment_manager.entrypoints.dashboard.resources import (
    prime_cpu_sampler,
    sample_host_resources,
)
from investment_manager.entrypoints.dashboard.stream import refresh_events
from investment_manager.forecast.context.repository import SqlContextAssessmentStore
from investment_manager.platform.database import build_engine
from investment_manager.scheduling.workflows import coordinator_workflow_id
from investment_manager.settings import AppConfig

_DEFAULT_LIMIT = 30
_MAX_LIMIT = 100
_EQUITY_WINDOWS = {"6h": 6, "24h": 24, "7d": 168, "30d": 720}


def create_app(
    config: AppConfig,
    database_url: str,
    *,
    assessment_database_url: str | None = None,
    assessment_config: AppConfig | None = None,
    web_dist: Path | None = None,
    stream_interval_seconds: float = 3.0,
    slow_refresh_every_ticks: int = 5,
) -> Starlette:
    if stream_interval_seconds <= 0:
        raise ValueError("Dashboard SSE 刷新间隔必须为正数")
    if slow_refresh_every_ticks < 1:
        raise ValueError("Dashboard 慢速刷新倍数必须至少为 1")
    if (assessment_database_url is None) != (assessment_config is None):
        raise ValueError("Assessment 历史库与冻结配置必须同时提供")
    engine = build_engine(database_url)
    reader = DashboardReader(engine, config)
    assessment_engine = (
        build_engine(assessment_database_url)
        if assessment_database_url is not None
        else None
    )
    assessment_reader = (
        DashboardReader(assessment_engine, assessment_config)
        if assessment_engine is not None
        else None
    )
    assessment_store = (
        SqlContextAssessmentStore(assessment_engine)
        if assessment_engine is not None
        else None
    )
    capital_reader = CapitalDashboardReader(engine, config)
    prime_cpu_sampler()
    temporal_client = None
    temporal_lock = asyncio.Lock()

    async def coordinator_statuses() -> tuple[dict, ...]:
        nonlocal temporal_client

        async def read() -> tuple[dict, ...]:
            nonlocal temporal_client
            if temporal_client is None:
                async with temporal_lock:
                    if temporal_client is None:
                        temporal_client = await Client.connect(
                            config.temporal.address,
                            namespace=config.temporal.namespace,
                        )

            async def query(symbol: str) -> dict:
                workflow_id = coordinator_workflow_id(symbol, config.pipeline.version)
                try:
                    status = await temporal_client.get_workflow_handle(workflow_id).query("status")
                except Exception as exc:
                    return {"symbol": symbol, "error": type(exc).__name__}
                return {"symbol": symbol, **status}

            return tuple(
                await asyncio.gather(*(query(symbol) for symbol in config.market_data.symbols))
            )

        try:
            return await asyncio.wait_for(read(), timeout=2.0)
        except Exception as exc:
            temporal_client = None
            return ({"error": type(exc).__name__},)

    async def health(_request: Request) -> JSONResponse:
        now = datetime.now(UTC)

        coordinator_facts = await coordinator_statuses()

        def read_health() -> dict:
            capital_overview = capital_reader.overview(now=now)
            quality_reader = assessment_reader or (
                reader if config.assessment.enabled else None
            )
            assessment_quality = (
                quality_reader.assessment_quality_status(now=now)
                if quality_reader is not None
                else None
            )
            return assemble_health(
                reader,
                config,
                now=now,
                capital_overview=capital_overview,
                assessment_quality=assessment_quality,
                host_resources=sample_host_resources(),
                coordinator_statuses=coordinator_facts,
            )

        data = await run_in_threadpool(read_health)
        return _json(
            {
                "stage": config.deployment.stage,
                "pipeline_version": config.pipeline.version,
                "capital_enabled": config.capital.enabled,
                "server_time": now.isoformat(),
                **data,
            }
        )

    async def capital(_request: Request) -> JSONResponse:
        now = datetime.now(UTC)
        overview = await run_in_threadpool(capital_reader.overview, now=now)
        return _json(serialize_capital_overview(overview))

    async def capital_activity(request: Request) -> JSONResponse:
        limit = _parse_limit(request)
        items = await run_in_threadpool(
            capital_reader.activity,
            cursor=_parse_cursor(request),
            limit=limit + 1,
        )
        page = page_slice(
            items,
            limit=limit,
            cursor_for=lambda item: PageCursor(item.at, item.activity_id),
        )
        return _json(
            {
                **serialize_capital_activity(page.items),
                "next_cursor": page.next_cursor,
            }
        )

    async def assessment_cycles(request: Request) -> JSONResponse:
        if assessment_reader is None:
            return _json({"cycles": [], "next_cursor": None})
        limit = _parse_limit(request)
        rows = await run_in_threadpool(
            assessment_reader.list_cycles,
            cursor=_parse_cursor(request),
            limit=limit + 1,
        )
        page = page_slice(
            rows,
            limit=limit,
            cursor_for=lambda item: PageCursor(item.as_of, item.cycle_id),
        )
        return _json(
            {
                "cycles": [ser.cycle_row(row) for row in page.items],
                "next_cursor": page.next_cursor,
            }
        )

    async def assessment_cycle_detail(request: Request) -> JSONResponse:
        if assessment_reader is None:
            return _json({"detail": "assessment archive is not configured"}, status_code=404)
        facts = await run_in_threadpool(
            assessment_reader.get_cycle,
            request.path_params["cycle_id"],
        )
        if facts is None:
            return _json({"detail": "cycle not found"}, status_code=404)
        return _json(ser.cycle_detail(facts))

    async def assessment_records(request: Request) -> JSONResponse:
        if assessment_reader is None:
            return _json({"assessments": [], "quality": None, "next_cursor": None})
        limit = _parse_limit(request)
        rows, quality = await asyncio.gather(
            run_in_threadpool(
                assessment_reader.list_assessments,
                cursor=_parse_cursor(request),
                limit=limit + 1,
            ),
            run_in_threadpool(
                assessment_reader.assessment_quality_status,
                now=datetime.now(UTC),
            ),
        )
        page = page_slice(
            rows,
            limit=limit,
            cursor_for=lambda item: PageCursor(
                item.assessment.available_at,
                item.assessment.assessment_id,
            ),
        )
        return _json(
            {
                "assessments": [ser.assessment_row(record) for record in page.items],
                "quality": ser.assessment_quality(quality),
                "next_cursor": page.next_cursor,
            }
        )

    async def assessment_record_detail(request: Request) -> JSONResponse:
        if assessment_reader is None:
            return _json(
                {"detail": "assessment archive is not configured"},
                status_code=404,
            )
        record = await run_in_threadpool(
            assessment_reader.get_assessment,
            request.path_params["assessment_id"],
        )
        if record is None:
            return _json({"detail": "assessment not found"}, status_code=404)
        observations = (
            await run_in_threadpool(
                assessment_store.mechanism_observations,
                record.assessment.assessment_id,
            )
            if assessment_store is not None
            else ()
        )
        return _json(ser.assessment_detail(record, observations=observations))

    async def cycles(request: Request) -> JSONResponse:
        cursor = _parse_cursor(request)
        limit = _parse_limit(request)
        rows = await run_in_threadpool(
            reader.list_cycles,
            cursor=cursor,
            limit=limit + 1,
        )
        page = page_slice(
            rows,
            limit=limit,
            cursor_for=lambda item: PageCursor(item.as_of, item.cycle_id),
        )
        return _json(
            {
                "cycles": [ser.cycle_row(row) for row in page.items],
                "next_cursor": page.next_cursor,
            }
        )

    async def cycle_detail(request: Request) -> JSONResponse:
        cycle_id = request.path_params["cycle_id"]
        facts = await run_in_threadpool(reader.get_cycle, cycle_id)
        if facts is None:
            return _json({"error": "周期不存在"}, status_code=404)
        return _json(ser.cycle_detail(facts))

    async def events(request: Request) -> JSONResponse:
        cursor = _parse_cursor(request)
        limit = _parse_limit(request)
        primary, assessment = await asyncio.gather(
            run_in_threadpool(reader.list_events, cursor=cursor, limit=limit + 1),
            (
                run_in_threadpool(
                    assessment_reader.list_events,
                    cursor=cursor,
                    limit=limit + 1,
                )
                if assessment_reader is not None
                else asyncio.sleep(0, result=[])
            ),
        )
        found = _merge_events(primary, assessment, limit=limit + 1)
        page = page_slice(
            found,
            limit=limit,
            cursor_for=lambda item: PageCursor(item.at, item.event_id),
        )
        return _json(
            {
                "events": [ser.world_event(event) for event in page.items],
                "next_cursor": page.next_cursor,
            }
        )

    async def positions(_request: Request) -> JSONResponse:
        records = await run_in_threadpool(reader.open_positions)
        marks, sides = await asyncio.gather(
            run_in_threadpool(reader.latest_prices),
            run_in_threadpool(
                reader.entry_sides, [record.lifecycle.cycle_id for record in records]
            ),
        )
        return _json(
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
        requested = request.query_params.get("window", "24h")
        window_key = requested if requested in _EQUITY_WINDOWS else "24h"
        hours = _EQUITY_WINDOWS[window_key]
        now = datetime.now(UTC)
        window = await run_in_threadpool(reader.equity_window, now=now, hours=hours)
        return _json({"window": window_key, **ser.equity(window)})

    async def accounts(_request: Request) -> JSONResponse:
        now = datetime.now(UTC)
        account_reader = assessment_reader or reader
        account_config = assessment_config or config
        statuses, calls = await asyncio.gather(
            run_in_threadpool(account_reader.accounts, now=now),
            run_in_threadpool(account_reader.ai_calls_last_hour, now=now),
        )
        return _json(
            {
                "accounts": [ser.account_status(status) for status in statuses],
                "call_activity": {
                    "last_hour": calls,
                    "minimum_interval_seconds": (
                        account_config.trigger.minimum_call_interval_seconds
                    ),
                },
            }
        )

    async def resources(_request: Request) -> JSONResponse:
        data = await run_in_threadpool(sample_host_resources)
        return _json(data)

    async def reconciliation(_request: Request) -> JSONResponse:
        now = datetime.now(UTC)
        report = await run_in_threadpool(reader.latest_reconciliation, now=now)
        return _json({"reconciliation": ser.reconciliation(report)})

    async def stream(_request: Request) -> StreamingResponse:
        return StreamingResponse(
            refresh_events(
                interval_seconds=stream_interval_seconds,
                slow_refresh_every_ticks=slow_refresh_every_ticks,
            ),
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
        Route("/api/capital", capital),
        Route("/api/capital/activity", capital_activity),
        Route("/api/assessment/cycles", assessment_cycles),
        Route(
            "/api/assessment/cycles/{cycle_id}",
            assessment_cycle_detail,
        ),
        Route("/api/assessment/records", assessment_records),
        Route(
            "/api/assessment/records/{assessment_id}",
            assessment_record_detail,
        ),
        Route("/api/stream", stream),
    ]
    app = Starlette(
        routes=routes,
        exception_handlers={InvalidPageCursor: _invalid_page_cursor},
    )
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


def _parse_cursor(request: Request) -> PageCursor | None:
    raw = request.query_params.get("cursor")
    if raw is None:
        return None
    return decode_page_cursor(raw)


def _merge_events(primary, assessment, *, limit: int):
    """Merge the capital and assessment ledgers without duplicating shared facts."""

    unique = {event.event_id: event for event in (*primary, *assessment)}
    return sorted(
        unique.values(),
        key=lambda event: (event.at, event.event_id),
        reverse=True,
    )[:limit]


async def _invalid_page_cursor(_request: Request, exc: Exception) -> JSONResponse:
    return _json({"detail": str(exc)}, status_code=400)


def _json(payload, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code, headers={"Cache-Control": "no-store"})
