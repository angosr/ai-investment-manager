from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer

from investment_manager.decision_cycle.service import run_trigger_service
from investment_manager.entrypoints.cli.root import app
from investment_manager.entrypoints.cli.support import (
    load_runtime_release,
    require_runtime_database,
    runtime_engine,
)
from investment_manager.forecast.context.analyst import assess_behavior_hash
from investment_manager.forecast.context.application import AssessmentCommand
from investment_manager.forecast.context.service import (
    AssessmentTemporalCoordinator,
    assemble_assessment_service,
    run_assessment_worker_process,
)
from investment_manager.forecast.context.workflow import AssessmentWorkflowRequest
from investment_manager.governance.evaluation.outcome_service import assemble_outcome_evaluation
from investment_manager.governance.models import resolve_manifest_artifact
from investment_manager.governance.policy import DeploymentStage
from investment_manager.information.aggregated_flows import HttpAggregatedEtfFlowSource
from investment_manager.information.collector import (
    EventNormalizer,
    HttpxNewsNowTransport,
    InformationCollector,
    InformationCollectorService,
    NewsNowSource,
    OfficialRssSource,
    StreamableHttpMcpTransport,
    TrendRadarMcpSource,
)
from investment_manager.information.coverage import (
    SqlInformationCoverageStore,
    build_source_poll_record,
)
from investment_manager.information.models import CausalDomain, SourcePollStatus
from investment_manager.information.official.economic_actual_source import (
    HttpEconomicReleaseActualSource,
)
from investment_manager.information.official.publications import OfficialPublicationSource
from investment_manager.information.official.repository import SqlStructuredInformationStore
from investment_manager.information.official.source import (
    HttpEconomicReleaseCalendarSource,
    HttpFederalRegisterSource,
    HttpFedOfficialSource,
    HttpOfficialMetricSource,
    HttpTreasuryBuybackSource,
)
from investment_manager.information.repository import SqlEventStore
from investment_manager.market.perpetual.service import PerpetualRefreshResult
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.market.runtime import (
    CROSS_VENUE_STREAM_ID_BY_VENUE,
    CrossVenueRefreshResult,
    MarketRuntime,
    MarketShockDetector,
    assemble_shadow_market_stream,
)
from investment_manager.platform.orchestration import OrchestrationPolicySnapshot
from investment_manager.scheduling.application import (
    set_trigger_heartbeat as apply_trigger_heartbeat,
)
from investment_manager.scheduling.application import trigger_now as apply_trigger_now
from investment_manager.scheduling.fact_triggers import CanonicalFactTriggerPublisher
from investment_manager.scheduling.repository import SqlTriggerRepository
from investment_manager.settings import load_config
from investment_manager.state.decision.packet import DecisionPacket
from investment_manager.state.economic_releases import (
    EconomicReleaseActualCollectorService,
    SqlEconomicReleaseActualFactIngestor,
)
from investment_manager.state.metric_ingestion import (
    AggregatedEtfFlowCollectorService,
    OfficialMetricCollectorService,
    SqlAggregatedEtfFlowFactIngestor,
    SqlOfficialMetricFactIngestor,
)
from investment_manager.state.official_ingestion import (
    MacroOfficialCollectorService,
    RegulatoryOfficialCollectorService,
    SqlEconomicReleaseCalendarFactIngestor,
    SqlFederalRegisterFactIngestor,
    SqlFedFactIngestor,
    SqlTreasuryBuybackFactIngestor,
    TreasuryBuybackCollectorService,
)
from investment_manager.state.repository import SqlFactStateStore


def assemble_market_service(loaded, engine) -> MarketRuntime:
    """Construct the exact market service graph without starting network I/O."""

    store = SqlMarketDataStore(engine)
    triggers = SqlTriggerRepository(engine, loaded.trigger)
    detector = MarketShockDetector(
        pipeline_id=loaded.pipeline.version,
        relative_move_threshold=loaded.trigger.volatility_jump_threshold,
        window_seconds=loaded.trigger.volatility_window_seconds,
        trigger_expiry_seconds=loaded.trigger.trigger_expiry_seconds,
        sink=triggers,
        analysis_owner_symbol=loaded.assessment.review_trigger_symbol,
        trigger_symbols=loaded.analysis_symbols,
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
                poll_interval_seconds=loaded.market_data.perpetual_poll_seconds,
                latest_publication_at=refresh.latest_publication_at,
                observation_count=refresh.observation_count,
                error_class=refresh.error_class,
            )
        )

    def record_cross_venue_refresh(refresh: CrossVenueRefreshResult) -> None:
        cross_policy = loaded.market_data.cross_venue_spot
        if cross_policy is None:
            raise RuntimeError("收到跨场所行情结果但能力未配置")
        coverage.put(
            build_source_poll_record(
                source_stream_id=CROSS_VENUE_STREAM_ID_BY_VENUE[refresh.venue],
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
                poll_interval_seconds=cross_policy.poll_seconds,
                latest_publication_at=refresh.latest_publication_at,
                observation_count=refresh.observation_count,
                error_class=refresh.error_class,
            )
        )

    return assemble_shadow_market_stream(
        loaded,
        store,
        market_observer=detector.observe,
        perpetual_refresh_observer=record_perpetual_refresh,
        cross_venue_refresh_observer=record_cross_venue_refresh,
    )


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
    engine.dispose()
    assembly = assemble_assessment_service(
        loaded,
        database_url,
        code_version=manifest.code_version,
        manifest_id=manifest.manifest_id,
    )
    assembly.activate()
    run_assessment_worker_process(config=loaded, application=assembly.application)


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
    service = assemble_market_service(loaded, engine)
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
    require_runtime_database(database_url)
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
    evidence_id: Annotated[
        list[str] | None,
        typer.Option("--evidence-id", help="绑定本次复核所依据的不可变 Evidence ID，可重复"),
    ] = None,
    release_manifest: Annotated[
        Path,
        typer.Option("--release-manifest", exists=True, dir_okay=False),
    ] = Path("config/release-manifest.yaml"),
) -> None:
    """在非实盘环境中，经版本化 TriggerPlan 门禁立即触发分析周期。"""

    loaded, manifest = load_runtime_release(config, release_manifest)
    if loaded.deployment.stage not in {DeploymentStage.SHADOW, DeploymentStage.TESTNET}:
        raise ValueError("trigger-now 只允许 SHADOW 或 TESTNET")
    if symbol not in loaded.analysis_symbols:
        raise ValueError("symbol 不在当前分析 Mandate")
    result = apply_trigger_now(
        repository=SqlTriggerRepository(runtime_engine(database_url), loaded.trigger),
        symbol=symbol,
        pipeline_id=loaded.pipeline.version,
        manifest_id=manifest.manifest_id,
        request_id=request_id,
        reason=reason,
        now=datetime.now(UTC),
        evidence_ids=tuple(evidence_id or ()),
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


@app.command("set-trigger-heartbeat")
def set_trigger_heartbeat(
    symbol: Annotated[str, typer.Option("--symbol")],
    heartbeat_minutes: Annotated[int, typer.Option("--heartbeat-minutes", min=1)],
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
    """调整当前非实盘 TriggerPlan 的耐久 heartbeat；不立即触发分析。"""

    loaded, manifest = load_runtime_release(config, release_manifest)
    if loaded.deployment.stage not in {DeploymentStage.SHADOW, DeploymentStage.TESTNET}:
        raise ValueError("set-trigger-heartbeat 只允许 SHADOW 或 TESTNET")
    if symbol not in loaded.analysis_symbols:
        raise ValueError("symbol 不在当前分析 Mandate")
    result = apply_trigger_heartbeat(
        repository=SqlTriggerRepository(runtime_engine(database_url), loaded.trigger),
        symbol=symbol,
        pipeline_id=loaded.pipeline.version,
        manifest_id=manifest.manifest_id,
        heartbeat_seconds=heartbeat_minutes * 60,
        now=datetime.now(UTC),
    )
    typer.echo(
        json.dumps(
            {
                "plan_id": result.plan.plan_id,
                "revision": result.plan.revision,
                "heartbeat_seconds": result.plan.heartbeat_seconds,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


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
    """结算当前 Release 启用的 Forecast、产品映射和配对认知结果。"""

    loaded, manifest = load_runtime_release(config, release_manifest)
    require_runtime_database(database_url)

    async def run() -> None:
        supervisor = assemble_outcome_evaluation(
            loaded,
            database_url,
            release=manifest,
        )
        await supervisor.run(asyncio.Event())

    asyncio.run(run())


@dataclass(frozen=True, slots=True)
class InformationServiceAssembly:
    services: tuple[object, ...]

    async def run(self, stop: asyncio.Event) -> None:
        await asyncio.gather(*(service.run(stop) for service in self.services))


def assemble_information_service(loaded, engine) -> InformationServiceAssembly:
    """Construct the exact collector graph without polling or persisting facts."""

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
    sources.extend(
        OfficialRssSource(
            feed,
            maximum_age_seconds=loaded.trigger.trigger_expiry_seconds,
            timeout_seconds=policy.request_timeout_seconds,
        )
        for feed in policy.official_event_feeds
    )
    publication_sources = tuple(
        OfficialPublicationSource(
            feed,
            maximum_age_seconds=(
                loaded.decision_state.packet_policy.maximum_background_fact_distance_seconds
            ),
            timeout_seconds=policy.request_timeout_seconds,
        )
        for feed in policy.official_publication_feeds
    )
    sources.extend(publication_sources)
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
    coverage_store = SqlInformationCoverageStore(engine)
    collector = InformationCollector(
        tuple(sources),
        EventNormalizer(
            version=policy.normalizer_version,
            universe=loaded.analysis_symbols,
            quote_asset=loaded.binance_testnet.quote_asset,
        ),
        SqlEventStore(
            engine,
            pipeline_id=loaded.pipeline.version,
            trigger_expiry_seconds=loaded.trigger.trigger_expiry_seconds,
            max_visible_events=policy.read_limit,
            analysis_owner_symbol=loaded.assessment.review_trigger_symbol,
        ),
        poll_recorder=coverage_store if publication_sources else None,
        coverage_bindings={
            source.source_id: (source.source_stream_id, source.causal_domain)
            for source in publication_sources
        },
        poll_interval_seconds=policy.collection_interval_seconds,
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
        required_freshness_seconds=(loaded.decision_state.packet_policy.maximum_market_age_seconds),
        analysis_owner_symbol=loaded.assessment.review_trigger_symbol,
    )
    official_service = MacroOfficialCollectorService(
        source=HttpFedOfficialSource(
            timeout_seconds=policy.request_timeout_seconds,
        ),
        ingestor=SqlFedFactIngestor(
            engine,
            loaded.decision_state.official_fact_policy,
        ),
        economic_calendar_source=HttpEconomicReleaseCalendarSource(
            timeout_seconds=policy.request_timeout_seconds,
        ),
        economic_calendar_ingestor=SqlEconomicReleaseCalendarFactIngestor(
            engine,
            loaded.decision_state.official_fact_policy,
        ),
        publish_recent=fact_trigger_publisher.publish_recent,
        monetary_poll_seconds=policy.fed_monetary_poll_seconds,
        calendar_poll_seconds=policy.fed_calendar_poll_seconds,
        economic_calendar_poll_seconds=(policy.economic_release_calendar_poll_seconds),
        poll_recorder=coverage_store,
    )
    economic_actual_service = EconomicReleaseActualCollectorService(
        source=HttpEconomicReleaseActualSource(
            timeout_seconds=policy.request_timeout_seconds,
        ),
        ingestor=SqlEconomicReleaseActualFactIngestor(
            engine,
            policy=loaded.decision_state.official_fact_policy,
        ),
        records=SqlStructuredInformationStore(engine),
        publish_recent=fact_trigger_publisher.publish_recent,
        poll_seconds=policy.economic_release_actual_poll_seconds,
        deadline_seconds=policy.economic_release_actual_deadline_seconds,
        recovery_lookback_seconds=(policy.economic_release_actual_recovery_lookback_seconds),
        poll_recorder=coverage_store,
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
        poll_recorder=coverage_store,
    )
    aggregate_flow_service = AggregatedEtfFlowCollectorService(
        source=HttpAggregatedEtfFlowSource(
            timeout_seconds=policy.request_timeout_seconds,
        ),
        ingestor=SqlAggregatedEtfFlowFactIngestor(
            engine,
            policy=loaded.decision_state.official_fact_policy,
        ),
        publish_recent=fact_trigger_publisher.publish_recent,
        poll_seconds=policy.etf_aggregate_flow_poll_seconds,
        poll_recorder=coverage_store,
    )
    regulatory_service = RegulatoryOfficialCollectorService(
        source=HttpFederalRegisterSource(
            timeout_seconds=policy.request_timeout_seconds,
        ),
        ingestor=SqlFederalRegisterFactIngestor(
            engine,
            loaded.decision_state.official_fact_policy,
        ),
        publish_recent=fact_trigger_publisher.publish_recent,
        poll_seconds=policy.regulatory_poll_seconds,
        poll_recorder=coverage_store,
    )
    treasury_buyback_service = TreasuryBuybackCollectorService(
        source=HttpTreasuryBuybackSource(
            timeout_seconds=policy.request_timeout_seconds,
        ),
        ingestor=SqlTreasuryBuybackFactIngestor(
            engine,
            loaded.decision_state.official_fact_policy,
        ),
        publish_recent=fact_trigger_publisher.publish_recent,
        poll_seconds=policy.treasury_buyback_poll_seconds,
        result_lookback_seconds=policy.treasury_buyback_result_lookback_seconds,
        poll_recorder=coverage_store,
    )

    return InformationServiceAssembly(
        services=(
            service,
            official_service,
            economic_actual_service,
            metric_service,
            aggregate_flow_service,
            regulatory_service,
            treasury_buyback_service,
        )
    )


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
    assembly = assemble_information_service(loaded, runtime_engine(database_url))

    asyncio.run(assembly.run(asyncio.Event()))


@app.command("dashboard-service")
def dashboard_service(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[
        str,
        typer.Option(envvar="INVESTMENT_MANAGER_DATABASE_URL", help="仅从受控环境注入数据库 URL"),
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

    loaded, manifest = load_runtime_release(config, release_manifest)
    frozen_dist = resolve_manifest_artifact(manifest, "web-dist")
    if web_dist is not None and web_dist.resolve() != frozen_dist:
        raise typer.BadParameter(
            "web-dist 必须与 ReleaseManifest 冻结制品路径一致",
            param_hint="web-dist",
        )
    require_runtime_database(database_url)
    application = create_app(
        loaded,
        database_url,
        web_dist=frozen_dist,
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
