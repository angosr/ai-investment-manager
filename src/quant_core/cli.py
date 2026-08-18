from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer

from quant_core.acceptance import PhaseAAuditor
from quant_core.binance_testnet import (
    BinanceApiError,
    BinanceCredentials,
    BinanceTestnetClient,
    SymbolRules,
)
from quant_core.config import AiMode, DeploymentStage, load_config
from quant_core.cycle import AnalysisCycle, CycleInput
from quant_core.governance import load_release_manifest, validate_manifest_against_config
from quant_core.governance_runtime import assemble_governance
from quant_core.ids import stable_id
from quant_core.ingestion import (
    EventNormalizer,
    InformationCollector,
    InformationCollectorService,
    StreamableHttpMcpTransport,
    TrendRadarMcpSource,
)
from quant_core.lifecycle_runtime import (
    LifecycleTemporalWorker,
    assemble_lifecycle_activities,
    assemble_lifecycle_supervisor,
)
from quant_core.market_data import MarketShockDetector, assemble_shadow_market_stream
from quant_core.market_data_sql import SqlMarketDataStore
from quant_core.outcome_evaluation_runtime import assemble_outcome_evaluation
from quant_core.persistence import SqlEventStore, build_engine
from quant_core.reconciliation_runtime import assemble_reconciliation
from quant_core.shadow import SqlShadowStateReader
from quant_core.temporal_runtime import (
    TemporalAnalysisCoordinator,
    assemble_analysis_cycle,
    run_worker_process,
)
from quant_core.trigger import (
    AnalysisEventRule,
    AnalysisTriggerType,
    TriggerDecision,
    TriggerNow,
    TriggerReason,
    build_initial_trigger_plan,
    build_trigger_plan_patch,
)
from quant_core.trigger_runtime import (
    TemporalTriggerDispatcher,
    TriggerAnalysisRequestBuilder,
    TriggerCoordinatorActivities,
    TriggerOutboxDispatcherService,
    TriggerTemporalWorker,
)
from quant_core.trigger_sql import (
    PostgresOutboxListener,
    PostgresTriggerLeadership,
    SqlTriggerRepository,
)
from quant_core.workflow import build_workflow_request

app = typer.Typer(no_args_is_help=True, help="Quant Core Mock 与回放命令")


@app.command("validate-config")
def validate_config(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
) -> None:
    loaded = load_config(config)
    typer.echo(f"OK: pipeline={loaded.pipeline.version}, risk={loaded.risk.version}")


@app.command("run-mock")
def run_mock(
    input_path: Annotated[Path, typer.Option("--input", exists=True, dir_okay=False)],
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
) -> None:
    loaded = load_config(config)
    raw = json.loads(input_path.read_text(encoding="utf-8"))
    cycle_input = CycleInput.model_validate(raw)
    result = AnalysisCycle.create(loaded).run(cycle_input)
    typer.echo(result.model_dump_json(indent=2))


@app.command("phase-a-audit")
def phase_a_audit(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    project_root: Annotated[Path, typer.Option(exists=True, file_okay=False)] = Path("."),
) -> None:
    report = PhaseAAuditor(load_config(config), project_root.resolve()).run()
    typer.echo(report.model_dump_json(indent=2))
    if not report.ready:
        raise typer.Exit(code=1)


@app.command("shadow-audit")
def shadow_audit(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    project_root: Annotated[Path, typer.Option(exists=True, file_okay=False)] = Path("."),
) -> None:
    """验证公开只读 Shadow；真实 Codex 隔离项仍保持 BLOCKED。"""

    loaded = load_config(config)
    report = PhaseAAuditor(loaded, project_root.resolve()).run()
    payload = report.model_dump(mode="json")
    payload["shadow_ready"] = report.shadow_ready
    payload["codex_ready"] = report.ready
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    if loaded.deployment.stage != DeploymentStage.SHADOW or not report.shadow_ready:
        raise typer.Exit(code=1)


@app.command("binance-testnet-audit")
def binance_testnet_audit(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
) -> None:
    """脱敏核验 Spot Testnet 凭证、账户权限和交易规则；不提交订单。"""

    loaded = load_config(config)
    try:
        credentials = BinanceCredentials.from_environment(loaded.binance_testnet)
        client = BinanceTestnetClient(loaded.binance_testnet, credentials)
        client.ping()
        server_time = client.server_time()
        account = client.account()
        balances = {item["asset"]: item for item in account.get("balances", [])}
        quote = balances.get(loaded.binance_testnet.quote_asset)
        quote_balance_present = quote is not None and (
            float(quote["free"]) + float(quote["locked"]) > 0
        )
        rules = tuple(
            SymbolRules.from_exchange_info(client.exchange_info(symbol))
            for symbol in loaded.market_data.symbols
        )
    except BinanceApiError as exc:
        typer.echo(
            json.dumps(
                {
                    "ready": False,
                    "reason": "BINANCE_API_REJECTED",
                    "http_status": exc.status_code,
                    "binance_code": exc.code,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise typer.Exit(code=1) from None
    except (OSError, RuntimeError, ValueError):
        typer.echo(
            json.dumps(
                {"ready": False, "reason": "BINANCE_TESTNET_UNAVAILABLE"},
                ensure_ascii=False,
                indent=2,
            )
        )
        raise typer.Exit(code=1) from None
    typer.echo(
        json.dumps(
            {
                "ready": bool(
                    server_time
                    and account.get("canTrade")
                    and "SPOT" in account.get("permissions", [])
                    and quote_balance_present
                ),
                "environment": os.environ.get("QUANT_CORE_BINANCE_ENVIRONMENT"),
                "account_type": account.get("accountType"),
                "can_trade": account.get("canTrade"),
                "spot_permission": "SPOT" in account.get("permissions", []),
                "quote_balance_present": quote_balance_present,
                "symbols": [
                    {
                        "symbol": item.symbol,
                        "market_order": "MARKET" in item.order_types,
                        "stop_loss": "STOP_LOSS" in item.order_types,
                        "min_notional": str(item.min_notional),
                    }
                    for item in rules
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("binance-testnet-order-test")
def binance_testnet_order_test(
    symbol: Annotated[str, typer.Option("--symbol")],
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
) -> None:
    """调用 Binance /order/test 验证 TRADE 权限；不会进入撮合引擎。"""

    loaded = load_config(config)
    if loaded.deployment.stage != DeploymentStage.TESTNET:
        raise ValueError("order/test 只允许使用显式 TESTNET 配置")
    if symbol not in loaded.market_data.symbols:
        raise ValueError("symbol 不在 Testnet 白名单")
    try:
        credentials = BinanceCredentials.from_environment(
            loaded.binance_testnet,
            require_order_submission=True,
        )
        client = BinanceTestnetClient(loaded.binance_testnet, credentials)
        rules = SymbolRules.from_exchange_info(client.exchange_info(symbol))
        reference_price = client.ticker_price(symbol)
        step = rules.market_step_size if rules.market_step_size > 0 else rules.step_size
        minimum = rules.market_min_quantity if rules.market_min_quantity > 0 else rules.min_quantity
        target = max(
            minimum,
            (rules.min_notional * Decimal("1.1")) / reference_price,
        )
        quantity = rules.quantity(
            target + step,
            market=True,
            reference_price=reference_price,
        )
        client.order_test(
            {
                "symbol": symbol,
                "side": "BUY",
                "type": "MARKET",
                "quantity": format(quantity, "f"),
                "newClientOrderId": stable_id(
                    "permission_test",
                    symbol,
                    client.server_time(),
                )[:36],
                "newOrderRespType": "ACK",
            }
        )
    except BinanceApiError as exc:
        typer.echo(
            json.dumps(
                {
                    "validated": False,
                    "reason": "BINANCE_ORDER_TEST_REJECTED",
                    "http_status": exc.status_code,
                    "binance_code": exc.code,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise typer.Exit(code=1) from None
    except (OSError, RuntimeError, ValueError):
        typer.echo(
            json.dumps(
                {"validated": False, "reason": "BINANCE_TESTNET_UNAVAILABLE"},
                ensure_ascii=False,
                indent=2,
            )
        )
        raise typer.Exit(code=1) from None
    typer.echo(
        json.dumps(
            {
                "validated": True,
                "symbol": symbol,
                "matching_engine_order_created": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("temporal-worker")
def temporal_worker(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[
        str,
        typer.Option(envvar="QUANT_CORE_DATABASE_URL", help="仅从受控环境注入数据库 URL"),
    ],
) -> None:
    """运行持久化 Mock 分析 Worker；进程由 Compose/systemd 等监督器管理。"""

    loaded = load_config(config)
    cycle = assemble_analysis_cycle(loaded, database_url)
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
    request = build_workflow_request(
        cycle_input=cycle_input,
        trigger=TriggerDecision(should_run=True, reason=TriggerReason.HEARTBEAT),
        temporal_policy=loaded.temporal,
        created_at=created_at,
        deadline=created_at + timedelta(minutes=deadline_minutes),
    )

    async def execute():
        coordinator = await TemporalAnalysisCoordinator.connect(loaded.temporal)
        return await coordinator.execute(request)

    result = asyncio.run(execute())
    typer.echo(result.model_dump_json(indent=2))


@app.command("market-stream")
def market_stream(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[
        str,
        typer.Option(envvar="QUANT_CORE_DATABASE_URL", help="仅从受控环境注入数据库 URL"),
    ],
) -> None:
    """运行 Binance 公开只读行情服务；仅显式 SHADOW 配置可以启动。"""

    loaded = load_config(config)
    engine = build_engine(database_url)
    store = SqlMarketDataStore(engine)
    triggers = SqlTriggerRepository(engine, loaded.trigger)
    detector = MarketShockDetector(
        pipeline_id=loaded.pipeline.version,
        relative_move_threshold=loaded.trigger.volatility_jump_threshold,
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

    loaded = load_config(config)
    engine = build_engine(database_url)
    repository = SqlTriggerRepository(engine, loaded.trigger)
    manifest = load_release_manifest(release_manifest)
    validate_manifest_against_config(manifest, loaded)
    now = datetime.now(UTC)
    for symbol in loaded.market_data.symbols:
        try:
            repository.plan_for_scope(symbol=symbol, pipeline_id=loaded.pipeline.version)
        except KeyError:
            repository.create_plan(
                build_initial_trigger_plan(
                    symbol=symbol,
                    pipeline_id=loaded.pipeline.version,
                    manifest_id=manifest.manifest_id,
                    updated_at=now,
                    heartbeat_seconds=loaded.trigger.heartbeat_minutes * 60,
                    event_rules=(
                        AnalysisEventRule(
                            rule_id="intelligence-default",
                            trigger_type=AnalysisTriggerType.INTELLIGENCE_INSERTED,
                            minimum_priority=int(loaded.trigger.high_impact_threshold * 100),
                            coalesce_seconds=loaded.trigger.debounce_seconds,
                            ordinary_cooldown_seconds=loaded.trigger.debounce_seconds,
                        ),
                        AnalysisEventRule(
                            rule_id="market-shock-default",
                            trigger_type=AnalysisTriggerType.MARKET_SHOCK,
                            minimum_priority=0,
                        ),
                        AnalysisEventRule(
                            rule_id="position-recheck-default",
                            trigger_type=AnalysisTriggerType.POSITION_RECHECK,
                            minimum_priority=0,
                        ),
                    ),
                )
            )

    async def run(wakeup: PostgresOutboxListener) -> None:
        temporal = await TemporalAnalysisCoordinator.connect(loaded.temporal)
        activities = TriggerCoordinatorActivities(
            TriggerAnalysisRequestBuilder(
                config=loaded,
                market_store=SqlMarketDataStore(engine),
                event_store=SqlEventStore(
                    engine,
                    pipeline_id=loaded.pipeline.version,
                    trigger_expiry_seconds=loaded.trigger.trigger_expiry_seconds,
                ),
                state=SqlShadowStateReader(
                    engine,
                    maximum_reconciliation_age_seconds=(
                        loaded.reconciliation.maximum_report_age_seconds
                    ),
                ),
                batch_recorder=repository,
            )
        )
        dispatcher = TriggerOutboxDispatcherService(
            repository=repository,
            dispatcher=TemporalTriggerDispatcher(temporal.client, loaded, repository),
            poll_seconds=loaded.trigger.outbox_fallback_poll_seconds,
            wakeup=wakeup,
        )
        async with TriggerTemporalWorker(temporal.client, loaded.temporal, activities):
            await dispatcher.run(asyncio.Event())

    leadership = PostgresTriggerLeadership(
        engine,
        loaded.trigger.dispatcher_advisory_lock_key,
    )
    with leadership, PostgresOutboxListener(engine) as wakeup:
        asyncio.run(run(wakeup))


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

    loaded = load_config(config)
    if (
        loaded.deployment.stage not in {DeploymentStage.SHADOW, DeploymentStage.TESTNET}
        or loaded.pipeline.ai_mode != AiMode.OFF
        or loaded.codex_runtime.enabled
    ):
        raise ValueError("trigger-now 只允许 AI/Codex 均关闭的 SHADOW 或 TESTNET")
    if symbol not in loaded.market_data.symbols:
        raise ValueError("symbol 不在当前行情白名单")
    manifest = load_release_manifest(release_manifest)
    validate_manifest_against_config(manifest, loaded)
    repository = SqlTriggerRepository(build_engine(database_url), loaded.trigger)
    plan = repository.plan_for_scope(symbol=symbol, pipeline_id=loaded.pipeline.version)
    now = datetime.now(UTC)
    result = repository.apply_patch(
        build_trigger_plan_patch(
            plan=plan,
            submitted_at=now,
            operations=(
                TriggerNow(
                    request_id=request_id,
                    reason=reason,
                ),
            ),
        ),
        now=now,
        current_manifest_id=manifest.manifest_id,
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
) -> None:
    """发现未关闭持仓并运行可恢复的 Temporal 生命周期监控。"""

    loaded = load_config(config)

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
) -> None:
    """持续主动对账独立 Mock 交易所与业务事实；差异时冻结新增风险。"""

    loaded = load_config(config)

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
) -> None:
    """在固定窗口和结算宽限期后聚合不可变的运行结果报告。"""

    loaded = load_config(config)

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
    project_root: Annotated[Path, typer.Option(exists=True, file_okay=False)] = Path("."),
) -> None:
    """运行隔离的 Governor 周期；真实 Codex 与三账号隔离门禁未通过时拒绝启动。"""

    loaded = load_config(config)
    root = project_root.resolve()

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
) -> None:
    """持续将 TrendRadar 只读 MCP 事件标准化后写入事实库。"""

    loaded = load_config(config)
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
    collector = InformationCollector(
        (source,),
        EventNormalizer(version=policy.version, universe=loaded.market_data.symbols),
        SqlEventStore(
            build_engine(database_url),
            pipeline_id=loaded.pipeline.version,
            trigger_expiry_seconds=loaded.trigger.trigger_expiry_seconds,
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
    host: Annotated[str, typer.Option(help="仅绑本机；与其余服务一致")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8090,
    web_dist: Annotated[
        Path | None,
        typer.Option(help="前端构建产物目录；不给则自动使用 ./web/dist（存在时）"),
    ] = None,
) -> None:
    """运行只读运行观测台（Web）；同一进程托管前端与只读 API，不写库、不控制。"""

    import uvicorn

    from quant_core.dashboard import create_app

    resolved_dist = web_dist if web_dist is not None else _default_web_dist()
    if resolved_dist is None:
        typer.echo("未找到前端构建产物（web/dist）；仅提供 API。")
        typer.echo("先运行：cd web && npm install && npm run build")
    loaded = load_config(config)
    application = create_app(loaded, database_url, web_dist=resolved_dist)
    typer.echo(f"运行观测台就绪：http://{host}:{port}")
    uvicorn.run(application, host=host, port=port, log_level="info")


def _default_web_dist() -> Path | None:
    """约定优先：默认托管 ./web/dist，让单条命令即可同时提供前端与 API。"""

    candidate = Path("web/dist")
    return candidate if candidate.is_dir() else None


if __name__ == "__main__":
    app()
