from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from investment_manager.entrypoints.cli.root import app
from investment_manager.entrypoints.cli.support import (
    default_web_dist,
    load_runtime_release,
    require_runtime_database,
    runtime_engine,
)
from investment_manager.execution.lifecycle_runtime import (
    LifecycleTemporalWorker,
    assemble_lifecycle_activities,
    assemble_lifecycle_supervisor,
)
from investment_manager.execution.reconciliation_runtime import assemble_reconciliation
from investment_manager.governance.outcome_runtime import assemble_outcome_evaluation
from investment_manager.governance.policy import DeploymentStage
from investment_manager.governance.runtime import assemble_governance
from investment_manager.information.collector import (
    EventNormalizer,
    HttpxNewsNowTransport,
    InformationCollector,
    InformationCollectorService,
    NewsNowSource,
    StreamableHttpMcpTransport,
    TrendRadarMcpSource,
)
from investment_manager.information.repository import SqlEventStore
from investment_manager.legacy.application import submit_frozen_analysis
from investment_manager.legacy.cycle import CycleInput
from investment_manager.legacy.runtime import (
    TemporalAnalysisCoordinator,
    assemble_analysis_cycle,
    run_worker_process,
)
from investment_manager.legacy.trigger_runtime import run_trigger_service
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.market.runtime import MarketShockDetector, assemble_shadow_market_stream
from investment_manager.scheduling.application import trigger_now as apply_trigger_now
from investment_manager.scheduling.repository import SqlTriggerRepository
from investment_manager.settings import load_config


@app.command("temporal-worker")
def temporal_worker(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[
        str,
        typer.Option(envvar="QUANT_CORE_DATABASE_URL", help="仅从受控环境注入数据库 URL"),
    ],
    release_manifest: Annotated[
        Path,
        typer.Option("--release-manifest", exists=True, dir_okay=False),
    ] = Path("config/release-manifest.yaml"),
) -> None:
    """运行持久化分析 Worker；PROPOSE 模式调用隔离的真实 Codex。"""

    loaded, manifest = load_runtime_release(config, release_manifest)
    require_runtime_database(database_url)
    cycle = assemble_analysis_cycle(
        loaded,
        database_url,
        code_version=manifest.code_version,
    )
    run_worker_process(loaded.temporal, cycle)


@app.command("submit-analysis")
def submit_analysis(
    input_path: Annotated[Path, typer.Option("--input", exists=True, dir_okay=False)],
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    deadline_minutes: Annotated[int, typer.Option(min=1, max=30)] = 5,
) -> None:
    """诊断性提交一个冻结周期；生产触发器应直接调用同一 Coordinator。"""

    loaded = load_config(config)
    cycle_input = CycleInput.model_validate_json(input_path.read_text(encoding="utf-8"))
    created_at = datetime.now(UTC)
    result = asyncio.run(
        submit_frozen_analysis(
            cycle_input=cycle_input,
            temporal_policy=loaded.temporal,
            created_at=created_at,
            deadline_minutes=deadline_minutes,
        )
    )
    typer.echo(result.model_dump_json(indent=2))


@app.command("market-stream")
def market_stream(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[
        str,
        typer.Option(envvar="QUANT_CORE_DATABASE_URL", help="仅从受控环境注入数据库 URL"),
    ],
    release_manifest: Annotated[
        Path,
        typer.Option("--release-manifest", exists=True, dir_okay=False),
    ] = Path("config/release-manifest.yaml"),
) -> None:
    """运行 Binance 公开只读行情服务；仅显式 SHADOW 配置可以启动。"""

    loaded, _ = load_runtime_release(config, release_manifest)
    engine = runtime_engine(database_url)
    store = SqlMarketDataStore(engine)
    triggers = SqlTriggerRepository(engine, loaded.trigger)
    detector = MarketShockDetector(
        pipeline_id=loaded.pipeline.version,
        relative_move_threshold=loaded.trigger.volatility_jump_threshold,
        window_seconds=loaded.trigger.volatility_window_seconds,
        trigger_expiry_seconds=loaded.trigger.trigger_expiry_seconds,
        sink=triggers,
    )
    service = assemble_shadow_market_stream(
        loaded,
        store,
        market_observer=detector.observe,
    )
    asyncio.run(service.run(asyncio.Event()))


@app.command("trigger-service")
def trigger_service(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[
        str,
        typer.Option(envvar="QUANT_CORE_DATABASE_URL", help="仅从受控环境注入数据库 URL"),
    ],
    release_manifest: Annotated[
        Path,
        typer.Option("--release-manifest", exists=True, dir_okay=False),
    ] = Path("config/release-manifest.yaml"),
) -> None:
    """运行唯一 TriggerCoordinator Worker 与可靠 Outbox Dispatcher。"""

    loaded, manifest = load_runtime_release(config, release_manifest)
    run_trigger_service(
        config=loaded,
        manifest=manifest,
        database_url=database_url,
        on_superseded=lambda terminated: typer.echo(
            f"已终止 {len(terminated)} 个旧 pipeline TriggerCoordinator"
        ),
    )


@app.command("trigger-now")
def trigger_now(
    symbol: Annotated[str, typer.Option("--symbol")],
    request_id: Annotated[str, typer.Option("--request-id")],
    reason: Annotated[str, typer.Option("--reason")],
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[
        str,
        typer.Option(envvar="QUANT_CORE_DATABASE_URL", help="仅从受控环境注入数据库 URL"),
    ],
    release_manifest: Annotated[
        Path,
        typer.Option("--release-manifest", exists=True, dir_okay=False),
    ] = Path("config/release-manifest.yaml"),
) -> None:
    """在非实盘环境中，经版本化 TriggerPlan 门禁立即触发分析周期。"""

    loaded, manifest = load_runtime_release(config, release_manifest)
    if loaded.deployment.stage not in {DeploymentStage.SHADOW, DeploymentStage.TESTNET}:
        raise ValueError("trigger-now 只允许 SHADOW 或 TESTNET")
    if symbol not in loaded.market_data.symbols:
        raise ValueError("symbol 不在当前行情白名单")
    result = apply_trigger_now(
        repository=SqlTriggerRepository(runtime_engine(database_url), loaded.trigger),
        symbol=symbol,
        pipeline_id=loaded.pipeline.version,
        manifest_id=manifest.manifest_id,
        request_id=request_id,
        reason=reason,
        now=datetime.now(UTC),
    )
    typer.echo(
        json.dumps(
            {
                "plan_id": result.plan.plan_id,
                "revision": result.plan.revision,
                "trigger_ids": [item.trigger_id for item in result.emitted_triggers],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("lifecycle-service")
def lifecycle_service(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[
        str,
        typer.Option(envvar="QUANT_CORE_DATABASE_URL", help="仅从受控环境注入数据库 URL"),
    ],
    release_manifest: Annotated[
        Path,
        typer.Option("--release-manifest", exists=True, dir_okay=False),
    ] = Path("config/release-manifest.yaml"),
) -> None:
    """发现未关闭持仓并运行可恢复的 Temporal 生命周期监控。"""

    loaded, _ = load_runtime_release(config, release_manifest)
    require_runtime_database(database_url)

    async def run() -> None:
        temporal = await TemporalAnalysisCoordinator.connect(loaded.temporal)
        activities = assemble_lifecycle_activities(loaded, database_url)
        supervisor = assemble_lifecycle_supervisor(
            loaded,
            database_url,
            temporal.client,
        )
        async with LifecycleTemporalWorker(
            temporal.client,
            loaded.temporal,
            activities,
        ):
            await supervisor.run(asyncio.Event())

    asyncio.run(run())


@app.command("reconciliation-service")
def reconciliation_service(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[
        str,
        typer.Option(envvar="QUANT_CORE_DATABASE_URL", help="仅从受控环境注入数据库 URL"),
    ],
    release_manifest: Annotated[
        Path,
        typer.Option("--release-manifest", exists=True, dir_okay=False),
    ] = Path("config/release-manifest.yaml"),
) -> None:
    """持续主动对账独立 Mock 交易所与业务事实；差异时冻结新增风险。"""

    loaded, _ = load_runtime_release(config, release_manifest)
    require_runtime_database(database_url)

    async def run() -> None:
        temporal = await TemporalAnalysisCoordinator.connect(loaded.temporal)
        worker, supervisor = assemble_reconciliation(
            loaded,
            database_url,
            temporal.client,
        )
        async with worker:
            await supervisor.run(asyncio.Event())

    asyncio.run(run())


@app.command("outcome-evaluation-service")
def outcome_evaluation_service(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[
        str,
        typer.Option(envvar="QUANT_CORE_DATABASE_URL", help="仅从受控环境注入数据库 URL"),
    ],
    release_manifest: Annotated[
        Path,
        typer.Option("--release-manifest", exists=True, dir_okay=False),
    ] = Path("config/release-manifest.yaml"),
) -> None:
    """在固定窗口和结算宽限期后聚合不可变的运行结果报告。"""

    loaded, _ = load_runtime_release(config, release_manifest)
    require_runtime_database(database_url)

    async def run() -> None:
        temporal = await TemporalAnalysisCoordinator.connect(loaded.temporal)
        worker, supervisor = assemble_outcome_evaluation(
            loaded,
            database_url,
            temporal.client,
        )
        async with worker:
            await supervisor.run(asyncio.Event())

    asyncio.run(run())


@app.command("governance-service")
def governance_service(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[
        str,
        typer.Option(envvar="QUANT_CORE_DATABASE_URL", help="仅从受控环境注入数据库 URL"),
    ],
    release_manifest: Annotated[
        Path,
        typer.Option("--release-manifest", exists=True, dir_okay=False),
    ] = Path("config/release-manifest.yaml"),
    project_root: Annotated[Path, typer.Option(exists=True, file_okay=False)] = Path("."),
) -> None:
    """运行隔离的 Governor 周期；真实 Codex 与账号隔离门禁未通过时拒绝启动。"""

    loaded, _ = load_runtime_release(config, release_manifest)
    root = project_root.resolve()
    require_runtime_database(database_url)

    async def run() -> None:
        temporal = await TemporalAnalysisCoordinator.connect(loaded.temporal)
        worker, supervisor = assemble_governance(
            loaded,
            database_url,
            temporal.client,
            project_root=root,
        )
        async with worker:
            await supervisor.run(asyncio.Event())

    asyncio.run(run())


@app.command("information-collector")
def information_collector(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[
        str,
        typer.Option(envvar="QUANT_CORE_DATABASE_URL", help="仅从受控环境注入数据库 URL"),
    ],
    release_manifest: Annotated[
        Path,
        typer.Option("--release-manifest", exists=True, dir_okay=False),
    ] = Path("config/release-manifest.yaml"),
) -> None:
    """持续将 TrendRadar 只读 MCP 事件标准化后写入事实库。"""

    loaded, _ = load_runtime_release(config, release_manifest)
    policy = loaded.information
    transport = StreamableHttpMcpTransport(
        policy.trendradar_mcp_url,
        timeout_seconds=policy.request_timeout_seconds,
    )
    source = TrendRadarMcpSource(
        transport,
        platforms=policy.platforms,
        limit=policy.read_limit,
        source_timezone=policy.source_timezone,
    )
    sources = [source]
    if policy.newsnow_sources:
        sources.append(
            NewsNowSource(
                HttpxNewsNowTransport(
                    policy.newsnow_base_url,
                    timeout_seconds=policy.request_timeout_seconds,
                ),
                sources=policy.newsnow_sources,
                maximum_age_seconds=loaded.trigger.trigger_expiry_seconds,
            )
        )
    collector = InformationCollector(
        tuple(sources),
        EventNormalizer(
            version=policy.version,
            universe=loaded.market_data.symbols,
            quote_asset=loaded.binance_testnet.quote_asset,
        ),
        SqlEventStore(
            runtime_engine(database_url),
            pipeline_id=loaded.pipeline.version,
            trigger_expiry_seconds=loaded.trigger.trigger_expiry_seconds,
            max_visible_events=policy.read_limit,
        ),
    )
    service = InformationCollectorService(
        collector,
        interval_seconds=policy.collection_interval_seconds,
    )
    asyncio.run(service.run(asyncio.Event()))


@app.command("dashboard-service")
def dashboard_service(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[
        str,
        typer.Option(envvar="QUANT_CORE_DATABASE_URL", help="仅从受控环境注入数据库 URL"),
    ],
    release_manifest: Annotated[
        Path,
        typer.Option("--release-manifest", exists=True, dir_okay=False),
    ] = Path("config/release-manifest.yaml"),
    host: Annotated[str, typer.Option(help="仅绑本机；与其余服务一致")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8090,
    web_dist: Annotated[
        Path | None,
        typer.Option(help="前端构建产物目录；不给则自动使用 ./web/dist（存在时）"),
    ] = None,
) -> None:
    """运行只读运行观测台（Web）；同一进程托管前端与只读 API，不写库、不控制。"""

    import uvicorn

    from investment_manager.entrypoints.dashboard import create_app

    resolved_dist = web_dist if web_dist is not None else default_web_dist()
    if resolved_dist is None:
        typer.echo("未找到前端构建产物（web/dist）；仅提供 API。")
        typer.echo("先运行：cd web && npm install && npm run build")
    loaded, _ = load_runtime_release(config, release_manifest)
    require_runtime_database(database_url)
    application = create_app(loaded, database_url, web_dist=resolved_dist)
    typer.echo(f"运行观测台就绪：http://{host}:{port}")
    # EventSource 是无限响应；有界等待后取消连接，避免服务重启被浏览器永久阻塞。
    uvicorn.run(
        application,
        host=host,
        port=port,
        log_level="info",
        timeout_graceful_shutdown=5,
    )
