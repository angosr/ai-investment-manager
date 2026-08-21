from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer

from investment_manager.decision_cycle.service import run_trigger_service
from investment_manager.entrypoints.cli.root import app
from investment_manager.entrypoints.cli.support import (
    default_web_dist,
    load_read_only_release_identity,
    load_runtime_release,
    require_runtime_database,
    runtime_engine,
)
from investment_manager.execution.lifecycle.service import (
    LifecycleTemporalWorker,
    assemble_lifecycle_activities,
    assemble_lifecycle_supervisor,
)
from investment_manager.execution.reconciliation.service import assemble_reconciliation
from investment_manager.forecast.context.analyst import assess_behavior_hash
from investment_manager.forecast.context.application import AssessmentCommand
from investment_manager.forecast.context.service import (
    AssessmentTemporalCoordinator,
    assemble_assessment_application,
    run_assessment_worker_process,
)
from investment_manager.forecast.context.workflow import AssessmentWorkflowRequest
from investment_manager.governance.change.service import assemble_governance
from investment_manager.governance.evaluation.assessment import (
    validate_assessment_runtime_plan,
)
from investment_manager.governance.evaluation.outcome_service import assemble_outcome_evaluation
from investment_manager.governance.models import evaluation_plan_invalidation_id
from investment_manager.governance.policy import DeploymentStage
from investment_manager.governance.repository import SqlGovernanceRepository
from investment_manager.information.collector import (
    EventNormalizer,
    HttpxNewsNowTransport,
    InformationCollector,
    InformationCollectorService,
    NewsNowSource,
    StreamableHttpMcpTransport,
    TrendRadarMcpSource,
)
from investment_manager.information.coverage import (
    SqlInformationCoverageStore,
    build_source_poll_record,
)
from investment_manager.information.models import CausalDomain, SourcePollStatus
from investment_manager.information.official.source import (
    HttpFedOfficialSource,
    HttpOfficialMetricSource,
)
from investment_manager.information.repository import SqlEventStore
from investment_manager.legacy.application import submit_frozen_analysis
from investment_manager.legacy.cycle import CycleInput
from investment_manager.legacy.runtime import (
    TemporalAnalysisCoordinator,
    assemble_analysis_cycle,
    run_worker_process,
)
from investment_manager.market.perpetual.service import PerpetualRefreshResult
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.market.runtime import MarketShockDetector, assemble_shadow_market_stream
from investment_manager.platform.orchestration import OrchestrationPolicySnapshot
from investment_manager.scheduling.application import trigger_now as apply_trigger_now
from investment_manager.scheduling.fact_triggers import CanonicalFactTriggerPublisher
from investment_manager.scheduling.repository import SqlTriggerRepository
from investment_manager.settings import load_config
from investment_manager.state.decision.packet import DecisionPacket
from investment_manager.state.metric_ingestion import (
    OfficialMetricCollectorService,
    SqlOfficialMetricFactIngestor,
)
from investment_manager.state.official_ingestion import (
    FedOfficialCollectorService,
    SqlFedFactIngestor,
)
from investment_manager.state.repository import SqlFactStateStore


@app.command("temporal-worker")
def temporal_worker(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[
        str,
        typer.Option(envvar="INVESTMENT_MANAGER_DATABASE_URL", help="仅从受控环境注入数据库 URL"),
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


@app.command("assessment-worker")
def assessment_worker(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[
        str,
        typer.Option(envvar="INVESTMENT_MANAGER_DATABASE_URL", help="仅从受控环境注入数据库 URL"),
    ],
    release_manifest: Annotated[
        Path,
        typer.Option("--release-manifest", exists=True, dir_okay=False),
    ] = Path("config/release-manifest.yaml"),
) -> None:
    """运行无交易权限的 ContextAssessment Worker。"""

    loaded, manifest = load_runtime_release(config, release_manifest)
    engine = runtime_engine(database_url)
    governance = SqlGovernanceRepository(engine)
    plans = tuple(
        plan
        for plan in governance.plans_for_manifest(manifest.manifest_id)
        if governance.get_failed_experiment(
            evaluation_plan_invalidation_id(plan.plan_id)
        )
        is None
    )
    validate_assessment_runtime_plan(
        config=loaded,
        manifest=manifest,
        plans=plans,
        started_at=datetime.now(UTC),
    )
    engine.dispose()
    application = assemble_assessment_application(
        loaded,
        database_url,
        code_version=manifest.code_version,
    )
    run_assessment_worker_process(config=loaded, application=application)


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


@app.command("submit-context-assessment")
def submit_context_assessment(
    input_path: Annotated[Path, typer.Option("--input", exists=True, dir_okay=False)],
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    deadline_minutes: Annotated[int, typer.Option(min=1, max=30)] = 5,
) -> None:
    """诊断性提交一个已冻结 DecisionPacket，不产生交易动作。"""

    loaded = load_config(config)
    packet = DecisionPacket.model_validate_json(input_path.read_text(encoding="utf-8"))
    command = AssessmentCommand.create(
        packet=packet,
        analysis_behavior_hash=assess_behavior_hash(loaded.codex_runtime, packet),
    )
    created_at = datetime.now(UTC)
    request = AssessmentWorkflowRequest.create(
        command=command,
        orchestration=OrchestrationPolicySnapshot.from_config(loaded.temporal),
        created_at=created_at,
        deadline=created_at + timedelta(minutes=deadline_minutes),
    )

    async def execute():
        coordinator = await AssessmentTemporalCoordinator.connect(loaded.temporal)
        return await coordinator.execute(request)

    typer.echo(asyncio.run(execute()).model_dump_json(indent=2))


@app.command("market-stream")
def market_stream(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[
        str,
        typer.Option(envvar="INVESTMENT_MANAGER_DATABASE_URL", help="仅从受控环境注入数据库 URL"),
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
    coverage = SqlInformationCoverageStore(engine)

    def record_perpetual_refresh(refresh: PerpetualRefreshResult) -> None:
        coverage.put(
            build_source_poll_record(
                source_stream_id="binance-usdm-market",
                domain=CausalDomain.SPOT_DERIVATIVES,
                status=(
                    SourcePollStatus.FAILED
                    if not refresh.succeeded
                    else (
                        SourcePollStatus.CHANGED
                        if refresh.changed_count
                        else SourcePollStatus.UNCHANGED
                    )
                ),
                started_at=refresh.started_at,
                completed_at=refresh.completed_at,
                latest_publication_at=refresh.latest_publication_at,
                observation_count=refresh.observation_count,
                error_class=refresh.error_class,
            )
        )

    service = assemble_shadow_market_stream(
        loaded,
        store,
        market_observer=detector.observe,
        perpetual_refresh_observer=record_perpetual_refresh,
    )
    asyncio.run(service.run(asyncio.Event()))


@app.command("trigger-service")
def trigger_service(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[
        str,
        typer.Option(envvar="INVESTMENT_MANAGER_DATABASE_URL", help="仅从受控环境注入数据库 URL"),
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
        typer.Option(envvar="INVESTMENT_MANAGER_DATABASE_URL", help="仅从受控环境注入数据库 URL"),
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
        typer.Option(envvar="INVESTMENT_MANAGER_DATABASE_URL", help="仅从受控环境注入数据库 URL"),
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
        typer.Option(envvar="INVESTMENT_MANAGER_DATABASE_URL", help="仅从受控环境注入数据库 URL"),
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
        typer.Option(envvar="INVESTMENT_MANAGER_DATABASE_URL", help="仅从受控环境注入数据库 URL"),
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
        typer.Option(envvar="INVESTMENT_MANAGER_DATABASE_URL", help="仅从受控环境注入数据库 URL"),
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
        typer.Option(envvar="INVESTMENT_MANAGER_DATABASE_URL", help="仅从受控环境注入数据库 URL"),
    ],
    release_manifest: Annotated[
        Path,
        typer.Option("--release-manifest", exists=True, dir_okay=False),
    ] = Path("config/release-manifest.yaml"),
) -> None:
    """持续采集新闻聚合与 Fed 一手事实，并发布可靠分析触发。"""

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
    engine = runtime_engine(database_url)
    collector = InformationCollector(
        tuple(sources),
        EventNormalizer(
            version=policy.normalizer_version,
            universe=loaded.market_data.symbols,
            quote_asset=loaded.binance_testnet.quote_asset,
        ),
        SqlEventStore(
            engine,
            pipeline_id=loaded.pipeline.version,
            trigger_expiry_seconds=loaded.trigger.trigger_expiry_seconds,
            max_visible_events=policy.read_limit,
        ),
    )
    service = InformationCollectorService(
        collector,
        interval_seconds=policy.collection_interval_seconds,
    )
    trigger_repository = SqlTriggerRepository(engine, loaded.trigger)
    fact_trigger_publisher = CanonicalFactTriggerPublisher(
        facts=SqlFactStateStore(engine),
        triggers=trigger_repository,
        mandate=loaded.assessment.mandate,
        delta_policy=loaded.decision_state.delta_policy,
        pipeline_id=loaded.pipeline.version,
        trigger_expiry_seconds=loaded.trigger.trigger_expiry_seconds,
        required_freshness_seconds=loaded.risk.maximum_market_age_seconds,
    )
    official_service = FedOfficialCollectorService(
        source=HttpFedOfficialSource(
            timeout_seconds=policy.request_timeout_seconds,
        ),
        ingestor=SqlFedFactIngestor(
            engine,
            loaded.decision_state.official_fact_policy,
        ),
        publish_recent=fact_trigger_publisher.publish_recent,
        monetary_poll_seconds=policy.fed_monetary_poll_seconds,
        calendar_poll_seconds=policy.fed_calendar_poll_seconds,
        poll_recorder=SqlInformationCoverageStore(engine),
    )
    metric_service = OfficialMetricCollectorService(
        source=HttpOfficialMetricSource(
            timeout_seconds=policy.request_timeout_seconds,
        ),
        ingestor=SqlOfficialMetricFactIngestor(
            engine,
            policy=loaded.decision_state.official_fact_policy,
        ),
        publish_recent=fact_trigger_publisher.publish_recent,
        fast_poll_seconds=policy.official_metric_poll_seconds,
        slow_poll_seconds=policy.official_metric_slow_poll_seconds,
        poll_recorder=SqlInformationCoverageStore(engine),
    )

    async def run() -> None:
        stop = asyncio.Event()
        await asyncio.gather(
            service.run(stop),
            official_service.run(stop),
            metric_service.run(stop),
        )

    asyncio.run(run())


@app.command("dashboard-service")
def dashboard_service(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[
        str,
        typer.Option(envvar="INVESTMENT_MANAGER_DATABASE_URL", help="仅从受控环境注入数据库 URL"),
    ],
    assessment_database_url: Annotated[
        str | None,
        typer.Option(
            envvar="INVESTMENT_MANAGER_ASSESSMENT_DATABASE_URL",
            help="可选的只读 Assessment 历史库；仅用于分层展示，不参与资本核算",
        ),
    ] = None,
    assessment_config: Annotated[
        Path | None,
        typer.Option(
            "--assessment-config",
            exists=True,
            dir_okay=False,
            help="Assessment 历史库对应的冻结配置；只用于正确解释只读事实",
        ),
    ] = None,
    assessment_release_manifest: Annotated[
        Path | None,
        typer.Option(
            "--assessment-release-manifest",
            exists=True,
            dir_okay=False,
            help="Assessment 历史库对应的 ReleaseManifest",
        ),
    ] = None,
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
    assessment_loaded = None
    assessment_identity_args = (
        assessment_database_url,
        assessment_config,
        assessment_release_manifest,
    )
    if any(item is not None for item in assessment_identity_args):
        if not all(item is not None for item in assessment_identity_args):
            raise typer.BadParameter(
                "Assessment 历史库、配置和 ReleaseManifest 必须同时提供"
            )
        assert assessment_config is not None
        assert assessment_release_manifest is not None
        assert assessment_database_url is not None
        assessment_loaded, _ = load_read_only_release_identity(
            assessment_config,
            assessment_release_manifest,
        )
        require_runtime_database(assessment_database_url)
    application = create_app(
        loaded,
        database_url,
        assessment_database_url=assessment_database_url,
        assessment_config=assessment_loaded,
        web_dist=resolved_dist,
    )
    typer.echo(f"运行观测台就绪：http://{host}:{port}")
    # EventSource 是无限响应；有界等待后取消连接，避免服务重启被浏览器永久阻塞。
    uvicorn.run(
        application,
        host=host,
        port=port,
        log_level="info",
        timeout_graceful_shutdown=5,
    )
