from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

import typer

from quant_core.acceptance import PhaseAAuditor
from quant_core.analyst import audit_codex_isolation
from quant_core.binance_testnet import (
    BinanceApiError,
    BinanceCredentials,
    BinanceTestnetClient,
    SymbolRules,
)
from quant_core.calibration import (
    CalibrationBuildSpec,
    EdgeCalibrationBuilder,
    uncalibrated_ref,
)
from quant_core.candidate_evaluation import SqlCandidateOutcomeStore
from quant_core.config import DeploymentStage, load_config
from quant_core.cycle import AnalysisCycle, CycleInput
from quant_core.domain import Side
from quant_core.governance import (
    load_release_manifest,
    validate_manifest_against_config,
    validate_manifest_code_version,
)
from quant_core.governance_runtime import assemble_governance
from quant_core.ids import content_hash, stable_id
from quant_core.ingestion import (
    EventNormalizer,
    HttpxNewsNowTransport,
    InformationCollector,
    InformationCollectorService,
    NewsNowSource,
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
from quant_core.persistence import (
    SqlEventStore,
    SqlGovernanceRepository,
    build_engine,
    require_current_schema,
)
from quant_core.portfolio_protection import SqlPortfolioProtectionStore
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
    carry_forward_trigger_plan,
)
from quant_core.trigger_runtime import (
    TemporalTriggerDispatcher,
    TriggerAnalysisRequestBuilder,
    TriggerCoordinatorActivities,
    TriggerOutboxDispatcherService,
    TriggerTemporalWorker,
    terminate_superseded_trigger_coordinators,
)
from quant_core.trigger_sql import (
    PostgresOutboxListener,
    PostgresTriggerLeadership,
    SqlTriggerRepository,
)
from quant_core.workflow import build_workflow_request

app = typer.Typer(no_args_is_help=True, help="Quant Core 事件驱动交易与回放命令")


def _runtime_engine(database_url: str):
    engine = build_engine(database_url)
    require_current_schema(engine)
    return engine


def _require_runtime_database(database_url: str) -> None:
    engine = _runtime_engine(database_url)
    engine.dispose()


def _parse_utc_option(value: str, *, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise typer.BadParameter(f"{name} 必须是 ISO-8601 时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise typer.BadParameter(f"{name} 必须包含时区")
    return parsed.astimezone(UTC)


def _load_runtime_release(config: Path, release_manifest: Path):
    loaded = load_config(config)
    manifest = load_release_manifest(release_manifest)
    validate_manifest_against_config(
        manifest,
        loaded,
        require_configuration_hash=True,
    )
    validate_manifest_code_version(manifest)
    return loaded, manifest


@app.command("build-edge-calibration")
def build_edge_calibration(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[
        str,
        typer.Option(envvar="QUANT_CORE_DATABASE_URL", help="仅从受控环境注入数据库 URL"),
    ],
    producer_id: Annotated[str, typer.Option()],
    producer_version: Annotated[str, typer.Option()],
    symbol: Annotated[str, typer.Option()],
    side: Annotated[Side, typer.Option()],
    horizon_minutes: Annotated[int, typer.Option(min=1)],
    training_start: Annotated[str, typer.Option(help="带时区的 ISO-8601 时间")],
    training_end: Annotated[str, typer.Option(help="带时区的 ISO-8601 时间")],
    published_at: Annotated[str, typer.Option(help="带时区的 ISO-8601 时间")],
    valid_from: Annotated[str, typer.Option(help="带时区的 ISO-8601 时间")],
    valid_until: Annotated[str, typer.Option(help="带时区的 ISO-8601 时间")],
    evaluation_version: Annotated[str | None, typer.Option()] = None,
    source_calibration_ref: Annotated[str | None, typer.Option()] = None,
    source_execution_policy_version: Annotated[str | None, typer.Option()] = None,
    source_frequency_policy_version: Annotated[str | None, typer.Option()] = None,
) -> None:
    """从点时可见的成熟 Shadow 标签生成制品；只输出，不发布或改库。"""

    loaded = load_config(config)
    training_start_at = _parse_utc_option(training_start, name="training_start")
    training_end_at = _parse_utc_option(training_end, name="training_end")
    publication_time = _parse_utc_option(published_at, name="published_at")
    validity_start = _parse_utc_option(valid_from, name="valid_from")
    validity_end = _parse_utc_option(valid_until, name="valid_until")
    engine = _runtime_engine(database_url)
    outcomes = SqlCandidateOutcomeStore(engine).settled_visible_for_calibration(
        training_start=training_start_at,
        training_end=training_end_at,
        published_at=publication_time,
    )
    artifact = EdgeCalibrationBuilder(loaded.calibration).build(
        outcomes,
        CalibrationBuildSpec(
            producer_id=producer_id,
            producer_version=producer_version,
            symbol=symbol,
            side=side,
            horizon_minutes=horizon_minutes,
            evaluation_version=(evaluation_version or loaded.outcome_evaluation.version),
            source_calibration_ref=(source_calibration_ref or uncalibrated_ref(producer_version)),
            source_execution_policy_version=(
                source_execution_policy_version or loaded.execution.version
            ),
            source_frequency_policy_version=(
                source_frequency_policy_version or loaded.frequency.version
            ),
            training_start=training_start_at,
            training_end=training_end_at,
            published_at=publication_time,
            valid_from=validity_start,
            valid_until=validity_end,
        ),
    )
    typer.echo(
        json.dumps(
            artifact.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )


@app.command("evaluate-ai-forecasts")
def evaluate_ai_forecasts(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[
        str,
        typer.Option(envvar="QUANT_CORE_DATABASE_URL", help="仅从受控环境注入数据库 URL"),
    ],
    window_start: Annotated[str, typer.Option(help="带时区的 ISO-8601 时间（含）")],
    window_end: Annotated[str, typer.Option(help="带时区的 ISO-8601 时间（不含）")],
    published_at: Annotated[str, typer.Option(help="评价事实发布时间")],
    pipeline_version: Annotated[str | None, typer.Option()] = None,
    analysis_behavior_hash: Annotated[str | None, typer.Option()] = None,
    minimum_non_overlapping_samples: Annotated[int, typer.Option(min=2)] = 30,
) -> None:
    """评价结果发生前冻结的 AI 方向预测；不把方向收益冒充可交易 PnL。"""

    from quant_core.forecast_evaluation import (
        AnalysisForecastEvaluator,
        SqlAnalysisForecastOutcomeStore,
    )

    loaded = load_config(config)
    start = _parse_utc_option(window_start, name="window_start")
    end = _parse_utc_option(window_end, name="window_end")
    publication = _parse_utc_option(published_at, name="published_at")
    if pipeline_version is not None and analysis_behavior_hash is not None:
        raise typer.BadParameter(
            "pipeline-version 与 analysis-behavior-hash 只能指定一个"
        )
    pipeline = None
    if analysis_behavior_hash is None:
        pipeline = pipeline_version or loaded.pipeline.version
    store = SqlAnalysisForecastOutcomeStore(_runtime_engine(database_url))
    outcomes = store.visible_outcomes(
        pipeline_version=pipeline,
        analysis_behavior_hash=analysis_behavior_hash,
        window_start=start,
        window_end=end,
        published_at=publication,
    )
    report = AnalysisForecastEvaluator(
        minimum_non_overlapping_samples=minimum_non_overlapping_samples
    ).evaluate(
        outcomes=outcomes,
        outcome_evaluation_version=loaded.outcome_evaluation.forecast_version,
        pipeline_version=pipeline,
        analysis_behavior_hash=analysis_behavior_hash,
        window_start=start,
        window_end=end,
        published_at=publication,
    )
    typer.echo(
        json.dumps(
            report.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )


@app.command("validate-config")
def validate_config(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
) -> None:
    loaded = load_config(config)
    typer.echo(f"OK: pipeline={loaded.pipeline.version}, risk={loaded.risk.version}")


@app.command("reset-portfolio-protection")
def reset_portfolio_protection(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[
        str,
        typer.Option(envvar="QUANT_CORE_DATABASE_URL", help="仅从受控环境注入数据库 URL"),
    ],
    reason: Annotated[str, typer.Option("--reason")],
    acknowledge_risk: Annotated[
        bool,
        typer.Option(
            "--acknowledge-risk",
            help="确认已人工复核风险，并以当前权益重置高水位",
        ),
    ] = False,
) -> None:
    """人工解除持久熔断；不会覆盖配置中的静态 kill switch。"""

    if not acknowledge_risk:
        typer.echo("拒绝恢复：必须显式提供 --acknowledge-risk")
        raise typer.Exit(code=2)
    loaded = load_config(config)
    store = SqlPortfolioProtectionStore(
        _runtime_engine(database_url),
        policy=loaded.risk,
        initial_equity=loaded.shadow.initial_quote_balance,
    )
    state = store.reset(reset_at=datetime.now(UTC), reason=reason)
    typer.echo(
        json.dumps(
            {
                "portfolio_id": state.portfolio_id,
                "kill_switch_active": state.kill_switch_active,
                "equity_baseline": str(state.high_water_equity),
                "reset_at": state.last_reset_at.isoformat() if state.last_reset_at else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


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


@app.command("fetch-binance-history")
def fetch_binance_history_command(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    symbol: Annotated[str, typer.Option()],
    start: Annotated[str, typer.Option(help="带时区的 ISO-8601 起点（含）")],
    end: Annotated[str, typer.Option(help="带时区的 ISO-8601 终点（不含）")],
    candidate: Annotated[str, typer.Option()] = "configured",
    funding_dataset_id: Annotated[str | None, typer.Option()] = None,
    catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(".runtime/datasets"),
    funding_catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/funding-datasets"
    ),
) -> None:
    """抓取并内容寻址保存 Binance 官方已收盘 K 线；不生成 Markdown 报告。"""

    from quant_core.research.candidates import resolve_research_candidate
    from quant_core.research.dataset import (
        HistoricalDatasetCatalog,
        HistoricalFundingDatasetCatalog,
        fetch_binance_history,
    )

    funding_dataset = (
        HistoricalFundingDatasetCatalog(funding_catalog).load(funding_dataset_id)
        if funding_dataset_id is not None
        else None
    )
    try:
        loaded, _ = resolve_research_candidate(
            candidate,
            load_config(config),
            funding_dataset=funding_dataset,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="candidate") from exc
    canonical_symbol = symbol.upper()
    if canonical_symbol not in loaded.market_data.symbols:
        raise typer.BadParameter("symbol 必须在当前 MarketDataPolicy 中显式登记")
    dataset = asyncio.run(
        fetch_binance_history(
            base_url=loaded.market_data.rest_base_url,
            symbol=canonical_symbol,
            interval=loaded.market_data.interval,
            start=_parse_utc_option(start, name="start"),
            end=_parse_utc_option(end, name="end"),
            timeout_seconds=loaded.market_data.rest_timeout_seconds,
        )
    )
    target = HistoricalDatasetCatalog(catalog).store(dataset)
    typer.echo(
        json.dumps(
            {
                "dataset_id": dataset.manifest.dataset_id,
                "symbol": dataset.manifest.symbol,
                "interval": dataset.manifest.interval,
                "bar_count": dataset.manifest.bar_count,
                "first_open_time": dataset.manifest.first_open_time.isoformat(),
                "last_close_time": dataset.manifest.last_close_time.isoformat(),
                "bars_hash": dataset.manifest.bars_hash,
                "path": str(target),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("fetch-binance-funding-history")
def fetch_binance_funding_history_command(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    symbol: Annotated[str, typer.Option()],
    start: Annotated[str, typer.Option(help="带时区的 ISO-8601 起点（含）")],
    end: Annotated[str, typer.Option(help="带时区的 ISO-8601 终点（不含）")],
    catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/funding-datasets"
    ),
) -> None:
    """冻结 Binance 官方校验的 USD-M 资金费率；不调用模型或生成报告。"""

    from quant_core.research.dataset import (
        HistoricalFundingDatasetCatalog,
        fetch_binance_funding_history,
    )

    loaded = load_config(config)
    canonical_symbol = symbol.upper()
    if canonical_symbol not in loaded.market_data.symbols:
        raise typer.BadParameter("symbol 必须在当前 MarketDataPolicy 中显式登记")
    dataset = asyncio.run(
        fetch_binance_funding_history(
            base_url="https://data.binance.vision",
            symbol=canonical_symbol,
            start=_parse_utc_option(start, name="start"),
            end=_parse_utc_option(end, name="end"),
            timeout_seconds=loaded.market_data.rest_timeout_seconds,
        )
    )
    target = HistoricalFundingDatasetCatalog(catalog).store(dataset)
    typer.echo(
        json.dumps(
            {
                "dataset_id": dataset.manifest.dataset_id,
                "symbol": dataset.manifest.symbol,
                "observation_count": dataset.manifest.observation_count,
                "first_available_at": dataset.manifest.first_available_at.isoformat(),
                "last_available_at": dataset.manifest.last_available_at.isoformat(),
                "observations_hash": dataset.manifest.observations_hash,
                "source_artifact_count": len(dataset.manifest.source_artifacts),
                "path": str(target),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("walk-forward")
def walk_forward_command(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[
        str,
        typer.Option(envvar="QUANT_CORE_DATABASE_URL", help="EvaluationPlan 事实库"),
    ],
    dataset_id: Annotated[str, typer.Option()],
    plan_id: Annotated[str, typer.Option()],
    training_bars: Annotated[int, typer.Option(min=2)],
    test_bars: Annotated[int, typer.Option(min=2)],
    blind_bars: Annotated[int, typer.Option(min=0)] = 0,
    candidate: Annotated[str, typer.Option()] = "configured",
    event_dataset_id: Annotated[str | None, typer.Option()] = None,
    funding_dataset_id: Annotated[str | None, typer.Option()] = None,
    catalog: Annotated[Path, typer.Option(exists=True, file_okay=False)] = Path(
        ".runtime/datasets"
    ),
    event_catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/event-datasets"
    ),
    funding_catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/funding-datasets"
    ),
    evaluation_catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/evaluations"
    ),
    starting_equity: Annotated[str, typer.Option()] = "10000",
    spread_bps: Annotated[str, typer.Option()] = "1",
    minimum_trades: Annotated[int, typer.Option(min=1)] = 30,
    minimum_profit_factor: Annotated[str, typer.Option()] = "1.05",
    minimum_average_net_return_bps_lower_bound: Annotated[
        str, typer.Option()
    ] = "0",
    maximum_drawdown_fraction: Annotated[str, typer.Option()] = "0.05",
    minimum_positive_fold_fraction: Annotated[str, typer.Option()] = "0.75",
    include_trades: Annotated[bool, typer.Option()] = False,
    register_only: Annotated[
        bool,
        typer.Option(help="只预登记本次完整实验规格，不运行回测"),
    ] = False,
) -> None:
    """预登记或运行冻结程序策略的 walk-forward；不调用 Codex。"""

    from quant_core.persistence import SqlGovernanceRepository
    from quant_core.research.candidates import resolve_research_candidate
    from quant_core.research.dataset import (
        HistoricalDatasetCatalog,
        HistoricalEventDatasetCatalog,
        HistoricalFundingDatasetCatalog,
    )
    from quant_core.research.evaluation_catalog import HistoricalEvaluationCatalog
    from quant_core.research.walk_forward import (
        WalkForwardEvaluationSpec,
        WalkForwardPlan,
        build_walk_forward_evaluation_plan,
        run_walk_forward,
        validate_walk_forward_evaluation_plan,
    )

    try:
        parsed_equity = Decimal(starting_equity)
        parsed_spread = Decimal(spread_bps)
        parsed_profit_factor = Decimal(minimum_profit_factor)
        parsed_return_lower_bound = Decimal(
            minimum_average_net_return_bps_lower_bound
        )
        parsed_drawdown = Decimal(maximum_drawdown_fraction)
        parsed_positive_folds = Decimal(minimum_positive_fold_fraction)
    except InvalidOperation as exc:
        raise typer.BadParameter("starting-equity 与 spread-bps 必须是十进制数") from exc
    if parsed_equity <= 0 or parsed_spread < 0:
        raise typer.BadParameter("starting-equity 必须为正，spread-bps 不能为负")
    if (
        parsed_profit_factor <= 0
        or not Decimal("0") < parsed_drawdown <= Decimal("1")
        or not Decimal("0") < parsed_positive_folds <= Decimal("1")
    ):
        raise typer.BadParameter("盈利因子必须为正，回撤与正收益窗口比例必须在 (0,1] 内")
    loaded_config = load_config(config)
    funding_dataset = (
        HistoricalFundingDatasetCatalog(funding_catalog).load(funding_dataset_id)
        if funding_dataset_id is not None
        else None
    )
    try:
        effective_config, research_strategy = resolve_research_candidate(
            candidate,
            loaded_config,
            funding_dataset=funding_dataset,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="candidate") from exc
    event_dataset = (
        HistoricalEventDatasetCatalog(event_catalog).load(event_dataset_id)
        if event_dataset_id is not None
        else None
    )
    dataset = HistoricalDatasetCatalog(catalog).load(dataset_id)
    walk_forward_plan = WalkForwardPlan(
        plan_id=plan_id,
        training_bars=training_bars,
        test_bars=test_bars,
        blind_bars=blind_bars,
        starting_equity=parsed_equity,
        spread_bps=parsed_spread,
        minimum_trades=minimum_trades,
        minimum_profit_factor=parsed_profit_factor,
        minimum_average_net_return_bps_lower_bound=parsed_return_lower_bound,
        maximum_drawdown_fraction=parsed_drawdown,
        minimum_positive_fold_fraction=parsed_positive_folds,
    )
    evaluation_spec = WalkForwardEvaluationSpec.freeze(
        candidate=candidate,
        dataset=dataset,
        event_dataset=event_dataset,
        funding_dataset=funding_dataset,
        config=effective_config,
        strategy=research_strategy,
        plan=walk_forward_plan,
    )
    governance = SqlGovernanceRepository(_runtime_engine(database_url))
    champion = governance.get_champion()
    if register_only:
        registered = build_walk_forward_evaluation_plan(
            spec=evaluation_spec,
            base_manifest_id=champion.manifest_id,
            registered_at=datetime.now(UTC),
        )
        governance.register_plan(registered)
        typer.echo(
            json.dumps(
                {
                    "evaluation_plan": registered.model_dump(mode="json"),
                    "walk_forward_spec": evaluation_spec.model_dump(mode="json"),
                    "walk_forward_spec_hash": content_hash(evaluation_spec),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    registered = governance.get_plan(plan_id)
    if registered is None:
        raise typer.BadParameter(
            "EvaluationPlan 尚未预登记；先用相同参数执行 --register-only",
            param_hint="plan-id",
        )
    try:
        validate_walk_forward_evaluation_plan(
            spec=evaluation_spec,
            plan=registered,
            champion_manifest_id=champion.manifest_id,
            evaluated_at=datetime.now(UTC),
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="plan-id") from exc
    result = run_walk_forward(
        dataset=dataset,
        event_dataset=event_dataset,
        funding_dataset=funding_dataset,
        config=effective_config,
        plan=walk_forward_plan,
        strategy=research_strategy,
        evaluation_spec_hash=content_hash(evaluation_spec),
    )
    result_path = HistoricalEvaluationCatalog(evaluation_catalog).store(result)
    payload = result.model_dump(mode="json")
    payload["result_path"] = str(result_path)
    if not include_trades:
        for fold in payload["folds"]:
            fold["run"].pop("trades", None)
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("blind-evaluate")
def blind_evaluate_command(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[
        str,
        typer.Option(envvar="QUANT_CORE_DATABASE_URL", help="EvaluationPlan 事实库"),
    ],
    source_evaluation_id: Annotated[str, typer.Option()],
    catalog: Annotated[Path, typer.Option(exists=True, file_okay=False)] = Path(
        ".runtime/datasets"
    ),
    event_catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/event-datasets"
    ),
    funding_catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/funding-datasets"
    ),
    evaluation_catalog: Annotated[
        Path, typer.Option(exists=True, file_okay=False)
    ] = Path(".runtime/evaluations"),
    blind_evaluation_catalog: Annotated[
        Path, typer.Option(file_okay=False)
    ] = Path(".runtime/blind-evaluations"),
    include_trades: Annotated[bool, typer.Option()] = False,
) -> None:
    """一次性揭示已通过 walk-forward 的预留尾窗；不调用 Codex。"""

    from quant_core.governance import BlindEvaluationClaim
    from quant_core.persistence import SqlGovernanceRepository
    from quant_core.research.candidates import resolve_research_candidate
    from quant_core.research.dataset import (
        HistoricalDatasetCatalog,
        HistoricalEventDatasetCatalog,
        HistoricalFundingDatasetCatalog,
    )
    from quant_core.research.evaluation_catalog import (
        BlindEvaluationCatalog,
        HistoricalEvaluationCatalog,
    )
    from quant_core.research.walk_forward import (
        WalkForwardEvaluationSpec,
        blind_evaluation_scope,
        run_blind_evaluation,
        validate_walk_forward_evaluation_plan,
    )

    governance = SqlGovernanceRepository(_runtime_engine(database_url))
    source = HistoricalEvaluationCatalog(evaluation_catalog).load(
        source_evaluation_id
    )
    if not source.completed or not source.passed:
        raise typer.BadParameter(
            "源 walk-forward 尚未通过全部门禁，禁止揭盲",
            param_hint="source-evaluation-id",
        )
    registered = governance.get_plan(source.plan.plan_id)
    if registered is None or registered.candidate_spec_snapshot is None:
        raise typer.BadParameter(
            "源 walk-forward 没有完整的预登记规格",
            param_hint="source-evaluation-id",
        )
    try:
        spec = WalkForwardEvaluationSpec.model_validate(
            registered.candidate_spec_snapshot
        )
    except ValueError as exc:
        raise typer.BadParameter(
            "预登记 walk-forward 规格无法恢复",
            param_hint="source-evaluation-id",
        ) from exc
    if (
        source.plan != spec.plan
        or source.dataset_id != spec.dataset_id
        or source.event_dataset_id != spec.event_dataset_id
        or source.funding_dataset_id != spec.funding_dataset_id
        or source.evaluation_spec_hash != content_hash(spec)
    ):
        raise typer.BadParameter(
            "源 walk-forward 与预登记规格不一致",
            param_hint="source-evaluation-id",
        )
    funding_dataset = (
        HistoricalFundingDatasetCatalog(funding_catalog).load(
            spec.funding_dataset_id
        )
        if spec.funding_dataset_id is not None
        else None
    )
    loaded_config = load_config(config)
    try:
        effective_config, research_strategy = resolve_research_candidate(
            spec.candidate,
            loaded_config,
            funding_dataset=funding_dataset,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="source-evaluation-id") from exc
    champion = governance.get_champion()
    try:
        validate_walk_forward_evaluation_plan(
            spec=spec,
            plan=registered,
            champion_manifest_id=champion.manifest_id,
            evaluated_at=datetime.now(UTC),
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="source-evaluation-id") from exc

    query_id = stable_id(
        "blind_evaluation_query",
        registered.plan_id,
        source.evaluation_id,
        source.evaluation_spec_hash,
    )
    blind_scope = blind_evaluation_scope(source)
    claim = governance.claim_blind_evaluation(
        BlindEvaluationClaim(
            query_id=query_id,
            blind_scope_id=blind_scope.scope_id,
            blind_symbol=blind_scope.symbol,
            blind_start=blind_scope.start,
            blind_end=blind_scope.end,
            plan_id=registered.plan_id,
            source_evaluation_id=source.evaluation_id,
            claimed_at=datetime.now(UTC),
        )
    )
    blind_catalog = BlindEvaluationCatalog(blind_evaluation_catalog)
    if claim.completed_at is not None:
        assert claim.result_id is not None and claim.result_hash is not None
        result = blind_catalog.load(claim.result_id)
        if content_hash(result) != claim.result_hash:
            raise RuntimeError("已完成盲测的事实库哈希与结果制品不一致")
        result_path = blind_evaluation_catalog.resolve() / f"{result.result_id}.json"
    else:
        dataset = HistoricalDatasetCatalog(catalog).load(spec.dataset_id)
        event_dataset = (
            HistoricalEventDatasetCatalog(event_catalog).load(spec.event_dataset_id)
            if spec.event_dataset_id is not None
            else None
        )
        result = run_blind_evaluation(
            source=source,
            query_id=query_id,
            dataset=dataset,
            event_dataset=event_dataset,
            funding_dataset=funding_dataset,
            config=effective_config,
            strategy=research_strategy,
        )
        result_path = blind_catalog.store(result)
        governance.complete_blind_evaluation(
            claim.model_copy(
                update={
                    "completed_at": datetime.now(UTC),
                    "result_id": result.result_id,
                    "result_hash": content_hash(result),
                }
            )
        )
    payload = result.model_dump(mode="json")
    payload["result_path"] = str(result_path)
    if not include_trades:
        payload["run"].pop("trades", None)
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("freeze-event-history")
def freeze_event_history_command(
    database_url: Annotated[
        str,
        typer.Option(envvar="QUANT_CORE_DATABASE_URL", help="点时事件事实库"),
    ],
    start: Annotated[str, typer.Option(help="按 observed_at 过滤的含时区起点（含）")],
    end: Annotated[str, typer.Option(help="按 observed_at 过滤的含时区终点（不含）")],
    catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/event-datasets"
    ),
) -> None:
    """冻结真实到达时间的标准事件；不为事后新闻猜测 observed_at。"""

    from sqlalchemy import select

    from quant_core.domain import IntelligenceEvent
    from quant_core.persistence import normalized_events
    from quant_core.research.dataset import (
        HistoricalEventDatasetCatalog,
        freeze_historical_events,
    )

    window_start = _parse_utc_option(start, name="start")
    window_end = _parse_utc_option(end, name="end")
    if window_start >= window_end:
        raise typer.BadParameter("start 必须早于 end")
    frozen_at = datetime.now(UTC)
    if window_end > frozen_at:
        raise typer.BadParameter("end 不能晚于当前冻结时间", param_hint="end")
    engine = _runtime_engine(database_url)
    with engine.connect() as connection:
        rows = tuple(
            connection.execute(
                select(normalized_events.c.payload)
                .where(
                    normalized_events.c.observed_at >= window_start,
                    normalized_events.c.observed_at < window_end,
                )
                .order_by(
                    normalized_events.c.observed_at,
                    normalized_events.c.evidence_id,
                )
            ).scalars()
        )
    dataset = freeze_historical_events(
        events=(IntelligenceEvent.model_validate(item) for item in rows),
        source="quant-core-normalized-events",
        requested_start=window_start,
        requested_end=window_end,
        collected_at=frozen_at,
    )
    target = HistoricalEventDatasetCatalog(catalog).store(dataset)
    typer.echo(
        json.dumps(
            {
                "event_dataset_id": dataset.manifest.dataset_id,
                "event_count": dataset.manifest.event_count,
                "requested_start": dataset.manifest.requested_start.isoformat(),
                "requested_end": dataset.manifest.requested_end.isoformat(),
                "events_hash": dataset.manifest.events_hash,
                "path": str(target),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("replay-event-triggers")
def replay_event_triggers_command(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[
        str,
        typer.Option(envvar="QUANT_CORE_DATABASE_URL", help="冻结 TriggerPlan 事实库"),
    ],
    event_dataset_id: Annotated[str, typer.Option()],
    replay_start: Annotated[str, typer.Option(help="带时区的回放起点（含）")],
    replay_end: Annotated[str, typer.Option(help="带时区的回放终点（不含）")],
    analysis_duration_seconds: Annotated[int, typer.Option(min=0)],
    admission_order: Annotated[
        str | None,
        typer.Option(help="同刻争用全局预算的品种顺序，逗号分隔；默认配置顺序"),
    ] = None,
    event_catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/event-datasets"
    ),
    include_batches: Annotated[bool, typer.Option()] = False,
) -> None:
    """复用生产协调规则回放全品种外部事件批次；不调用 Codex 或交易。"""

    from sqlalchemy import select

    from quant_core.persistence import (
        analysis_call_admissions,
        analysis_cycles,
        market_snapshots,
    )
    from quant_core.research.dataset import HistoricalEventDatasetCatalog
    from quant_core.research.trigger_replay import (
        ExternalTriggerReplaySpec,
        TriggerReplayInitialScopeState,
        run_external_trigger_replay,
    )

    loaded = load_config(config)
    window_start = _parse_utc_option(replay_start, name="replay-start")
    window_end = _parse_utc_option(replay_end, name="replay-end")
    engine = _runtime_engine(database_url)
    repository = SqlTriggerRepository(engine, loaded.trigger)
    parsed_admission_order = (
        tuple(
            item.strip().upper()
            for item in admission_order.split(",")
            if item.strip()
        )
        if admission_order is not None
        else None
    )
    if parsed_admission_order is not None and (
        len(parsed_admission_order) != len(set(parsed_admission_order))
        or set(parsed_admission_order) != set(loaded.market_data.symbols)
    ):
        raise typer.BadParameter(
            "admission-order 必须无重复且完整覆盖 MarketDataPolicy 品种",
            param_hint="admission-order",
        )
    try:
        plans = tuple(
            repository.plan_for_scope(
                symbol=symbol,
                pipeline_id=loaded.pipeline.version,
            )
            for symbol in loaded.market_data.symbols
        )
    except KeyError as error:
        raise typer.BadParameter(
            f"当前 Pipeline 缺少冻结 TriggerPlan: {error.args[0]}"
        ) from None
    with engine.connect() as connection:
        initial_global_admitted_times = tuple(
            connection.execute(
                select(analysis_call_admissions.c.admitted_at)
                .where(
                    analysis_call_admissions.c.admitted_at
                    > window_start - timedelta(hours=1),
                    analysis_call_admissions.c.admitted_at < window_start,
                )
                .order_by(analysis_call_admissions.c.admitted_at)
            ).scalars()
        )
        initial_scope_items = []
        for symbol in loaded.market_data.symbols:
            completed = tuple(
                connection.execute(
                    select(analysis_cycles.c.created_at)
                    .join(
                        market_snapshots,
                        market_snapshots.c.cycle_id == analysis_cycles.c.cycle_id,
                    )
                    .where(
                        analysis_cycles.c.pipeline_version == loaded.pipeline.version,
                        market_snapshots.c.symbol == symbol,
                        analysis_cycles.c.created_at > window_start - timedelta(hours=1),
                        analysis_cycles.c.created_at < window_start,
                    )
                    .order_by(analysis_cycles.c.created_at)
                ).scalars()
            )
            if completed:
                initial_scope_items.append(
                    TriggerReplayInitialScopeState(
                        symbol=symbol,
                        last_analysis_at=completed[-1],
                        call_times=completed,
                    )
                )
        initial_scopes = tuple(initial_scope_items)
    result = run_external_trigger_replay(
        event_dataset=HistoricalEventDatasetCatalog(event_catalog).load(
            event_dataset_id
        ),
        spec=ExternalTriggerReplaySpec.freeze(
            plans=plans,
            config=loaded,
            analysis_duration_seconds=analysis_duration_seconds,
            initial_global_admitted_times=initial_global_admitted_times,
            initial_scopes=initial_scopes,
            initial_state_source="CYCLE_PERSISTENCE_PROXY",
            admission_order=parsed_admission_order,
        ),
        replay_start=window_start,
        replay_end=window_end,
    )
    payload = result.model_dump(mode="json")
    payload["batch_count"] = len(result.batches)
    if not include_batches:
        payload.pop("batches", None)
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("research-catalog")
def research_catalog_command(
    evaluation_catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/evaluations"
    ),
) -> None:
    """派生历史实验的唯一有效版本、累计尝试与歧义；不改写制品。"""

    from quant_core.research.evaluation_catalog import HistoricalEvaluationCatalog

    summaries = HistoricalEvaluationCatalog(evaluation_catalog).summaries()
    typer.echo(
        json.dumps(
            {"experiments": [item.model_dump(mode="json") for item in summaries]},
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("paired-decision-tape")
def paired_decision_tape_command(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[
        str,
        typer.Option(envvar="QUANT_CORE_DATABASE_URL", help="前瞻决策带事实库"),
    ],
    pipeline_version: Annotated[str, typer.Option()],
    symbol: Annotated[str, typer.Option()],
    plan_id: Annotated[str, typer.Option(help="结果成熟前登记的评价计划 ID")],
    signal_end: Annotated[str, typer.Option(help="带时区的预登记评价终点（不含）")],
    source_blind_evaluation_id: Annotated[
        str | None,
        typer.Option(help="已通过的一次性盲测基线结果 ID"),
    ] = None,
    dataset_id: Annotated[
        str | None,
        typer.Option(help="运行评价时覆盖完整窗口的冻结行情数据集"),
    ] = None,
    horizon_minutes: Annotated[int, typer.Option(min=1)] = 60,
    maximum_age_minutes: Annotated[int, typer.Option(min=1)] = 60,
    minimum_confidence: Annotated[str, typer.Option()] = "0.60",
    minimum_non_overlapping_forecasts: Annotated[int, typer.Option(min=2)] = 30,
    candidate: Annotated[str, typer.Option()] = "configured",
    catalog: Annotated[Path, typer.Option(exists=True, file_okay=False)] = Path(
        ".runtime/datasets"
    ),
    blind_evaluation_catalog: Annotated[
        Path, typer.Option(file_okay=False)
    ] = Path(".runtime/blind-evaluations"),
    starting_equity: Annotated[str, typer.Option()] = "10000",
    spread_bps: Annotated[str, typer.Option()] = "1",
    include_trades: Annotated[bool, typer.Option()] = False,
    register_only: Annotated[
        bool,
        typer.Option(help="只登记从当前时刻开始的完整前瞻配对实验"),
    ] = False,
) -> None:
    """预登记或运行 Codex 前瞻 CONTEXT 带的 Q/Q+AI 配对回放。"""

    from quant_core.persistence import SqlGovernanceRepository
    from quant_core.research.candidates import resolve_research_candidate
    from quant_core.research.dataset import HistoricalDatasetCatalog
    from quant_core.research.decision_tape import (
        ForecastGateEvaluationSpec,
        ForecastGatePolicy,
        SqlForecastDecisionTapeReader,
        build_forecast_gate_evaluation_plan,
        run_paired_decision_tape_backtest,
        validate_forecast_gate_baseline,
        validate_forecast_gate_evaluation_plan,
    )
    from quant_core.research.evaluation_catalog import BlindEvaluationCatalog

    try:
        equity = Decimal(starting_equity)
        spread = Decimal(spread_bps)
        confidence = Decimal(minimum_confidence)
    except InvalidOperation as exc:
        raise typer.BadParameter("权益、点差和置信度必须是十进制数") from exc
    if equity <= 0 or spread < 0 or not Decimal("0") <= confidence <= Decimal("1"):
        raise typer.BadParameter("权益必须为正、点差非负、置信度位于 [0,1]")
    end = _parse_utc_option(signal_end, name="signal-end")
    try:
        loaded, strategy = resolve_research_candidate(candidate, load_config(config))
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="candidate") from exc
    canonical_symbol = symbol.upper()
    engine = _runtime_engine(database_url)
    governance = SqlGovernanceRepository(engine)
    champion = governance.get_champion()
    plan = governance.get_plan(plan_id)
    if register_only and plan is not None:
        raise typer.BadParameter("EvaluationPlan 已存在且不可覆盖", param_hint="plan-id")
    if register_only:
        registered_at = datetime.now(UTC)
        if end <= registered_at:
            raise typer.BadParameter(
                "register-only 的 signal-end 必须位于未来",
                param_hint="signal-end",
            )
    elif plan is None:
        raise typer.BadParameter("评价计划未在治理事实库预登记", param_hint="plan-id")
    else:
        registered_at = plan.registered_at
    source_id = source_blind_evaluation_id
    if not register_only and plan is not None:
        try:
            registered_spec = ForecastGateEvaluationSpec.model_validate(
                plan.candidate_spec_snapshot
            )
        except ValueError as exc:
            raise typer.BadParameter(
                "预登记 AI 门控规格无法恢复", param_hint="plan-id"
            ) from exc
        frozen_source_id = registered_spec.source_blind_evaluation_id
        if source_id is not None and source_id != frozen_source_id:
            raise typer.BadParameter(
                "调用方盲测基线与预登记规格不一致",
                param_hint="source-blind-evaluation-id",
            )
        source_id = frozen_source_id
    if source_id is None:
        raise typer.BadParameter(
            "AI 门控计划必须绑定已通过的一次性盲测基线",
            param_hint="source-blind-evaluation-id",
        )
    try:
        source_blind_evaluation = BlindEvaluationCatalog(
            blind_evaluation_catalog
        ).load(source_id)
        validate_forecast_gate_baseline(
            source=source_blind_evaluation,
            config=loaded,
            strategy=strategy,
            symbol=canonical_symbol,
        )
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(
            str(exc), param_hint="source-blind-evaluation-id"
        ) from exc
    dataset = None
    if not register_only:
        if dataset_id is None:
            raise typer.BadParameter(
                "运行配对评价必须提供冻结行情数据集",
                param_hint="dataset-id",
            )
        dataset = HistoricalDatasetCatalog(catalog).load(dataset_id)
        if dataset.manifest.symbol != canonical_symbol:
            raise typer.BadParameter("symbol 与历史数据集不一致", param_hint="symbol")
        if dataset.manifest.source != "binance-rest-historical":
            raise typer.BadParameter(
                "配对评价只接受 Binance REST 历史数据集",
                param_hint="dataset-id",
            )
    policy = ForecastGatePolicy(
        plan_id=plan_id,
        registered_at=registered_at,
        evaluation_end=end,
        horizon_minutes=horizon_minutes,
        maximum_age_minutes=maximum_age_minutes,
        minimum_confidence=confidence,
        minimum_non_overlapping_forecasts=minimum_non_overlapping_forecasts,
    )
    evaluation_spec = ForecastGateEvaluationSpec.freeze(
        strategy=strategy,
        config=loaded,
        symbol=canonical_symbol,
        pipeline_version=pipeline_version,
        starting_equity=equity,
        spread_bps=spread,
        maximum_completion_lag_seconds=loaded.shadow.analysis_deadline_seconds,
        policy=policy,
        source_blind_evaluation_id=source_blind_evaluation.result_id,
        source_blind_evaluation_hash=content_hash(source_blind_evaluation),
    )
    if register_only:
        registered = build_forecast_gate_evaluation_plan(
            spec=evaluation_spec,
            base_manifest_id=champion.manifest_id,
        )
        governance.register_plan(registered)
        typer.echo(
            json.dumps(
                {
                    "evaluation_plan": registered.model_dump(mode="json"),
                    "forecast_gate_spec": evaluation_spec.model_dump(mode="json"),
                    "forecast_gate_spec_hash": content_hash(evaluation_spec),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    assert plan is not None
    assert dataset is not None
    if datetime.now(UTC) < policy.evaluation_end:
        raise typer.BadParameter("前瞻配对评价窗口尚未结束", param_hint="signal-end")
    try:
        validate_forecast_gate_evaluation_plan(
            spec=evaluation_spec,
            plan=plan,
            champion_manifest_id=champion.manifest_id,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="plan-id") from exc
    tape = SqlForecastDecisionTapeReader(engine).read(
        pipeline_version=pipeline_version,
        symbol=canonical_symbol,
        window_start=policy.registered_at,
        window_end=end,
        maximum_completion_lag_seconds=loaded.shadow.analysis_deadline_seconds,
    )
    result = run_paired_decision_tape_backtest(
        dataset=dataset,
        config=loaded,
        tape=tape,
        policy=policy,
        strategy=strategy,
        signal_start=policy.registered_at,
        signal_end=end,
        starting_equity=equity,
        spread_bps=spread,
        evaluation_spec_hash=content_hash(evaluation_spec),
    )
    payload = result.model_dump(mode="json")
    if not include_trades:
        payload["baseline"].pop("trades", None)
        payload["gated"].pop("trades", None)
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


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


@app.command("codex-isolation-audit")
def codex_isolation_audit(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
) -> None:
    """脱敏验证所有已启用白名单账号的额度契约和无工具读取边界。"""

    loaded = load_config(config)
    accounts = tuple(item for item in loaded.codex_accounts.accounts if item.enabled)
    if not accounts:
        typer.echo(
            json.dumps(
                {
                    "ready": False,
                    "runtime_policy_version": loaded.codex_runtime.version,
                    "reason_code": "NO_ENABLED_ACCOUNTS",
                    "checks": [],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise typer.Exit(code=1)
    runtime = loaded.codex_runtime.model_copy(update={"enabled": True, "isolation_verified": True})
    audit_parent = loaded.codex_runtime.bundle_root.parent / ".isolation-audits"
    audit_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="quant-core-codex-isolation-", dir=audit_parent
    ) as directory:
        root = Path(directory)
        checks = tuple(
            audit_codex_isolation(
                account=account,
                policy=runtime,
                target=root / account.account_id,
            )
            for account in accounts
        )
    ready = all(check.ready for check in checks)
    typer.echo(
        json.dumps(
            {
                "ready": ready,
                "runtime_policy_version": runtime.version,
                "reason_code": "OK" if ready else "ISOLATION_AUDIT_FAILED",
                "checks": [check.model_dump(mode="json") for check in checks],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if not ready:
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
    release_manifest: Annotated[
        Path,
        typer.Option("--release-manifest", exists=True, dir_okay=False),
    ] = Path("config/release-manifest.yaml"),
) -> None:
    """运行持久化分析 Worker；PROPOSE 模式调用隔离的真实 Codex。"""

    loaded, manifest = _load_runtime_release(config, release_manifest)
    _require_runtime_database(database_url)
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
    release_manifest: Annotated[
        Path,
        typer.Option("--release-manifest", exists=True, dir_okay=False),
    ] = Path("config/release-manifest.yaml"),
) -> None:
    """运行 Binance 公开只读行情服务；仅显式 SHADOW 配置可以启动。"""

    loaded, _ = _load_runtime_release(config, release_manifest)
    engine = _runtime_engine(database_url)
    store = SqlMarketDataStore(engine)
    triggers = SqlTriggerRepository(engine, loaded.trigger)
    detector = MarketShockDetector(
        pipeline_id=loaded.pipeline.version,
        relative_move_threshold=loaded.trigger.volatility_jump_threshold,
        window_seconds=loaded.market_data.interval_seconds,
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

    loaded, manifest = _load_runtime_release(config, release_manifest)
    engine = _runtime_engine(database_url)
    SqlGovernanceRepository(engine).record_release(manifest)
    repository = SqlTriggerRepository(engine, loaded.trigger)
    now = datetime.now(UTC)
    previous_plans = repository.current_plans_for_symbols(loaded.market_data.symbols)
    for symbol in loaded.market_data.symbols:
        try:
            current = repository.plan_for_scope(
                symbol=symbol,
                pipeline_id=loaded.pipeline.version,
            )
        except KeyError:
            predecessors = tuple(item for item in previous_plans if item.symbol == symbol)
            if predecessors:
                plan = carry_forward_trigger_plan(
                    max(
                        predecessors,
                        key=lambda item: (item.updated_at, item.revision, item.pipeline_id),
                    ),
                    pipeline_id=loaded.pipeline.version,
                    manifest_id=manifest.manifest_id,
                    updated_at=now,
                )
            else:
                plan = build_initial_trigger_plan(
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
            repository.create_plan(plan)
        else:
            if current.manifest_id != manifest.manifest_id:
                raise ValueError(
                    f"{symbol} 当前 TriggerPlan 的 manifest 与 release 不一致；"
                    "必须升级 pipeline version 完成切换"
                )

    async def run(wakeup: PostgresOutboxListener) -> None:
        temporal = await TemporalAnalysisCoordinator.connect(loaded.temporal)
        current_plans = repository.current_plans_for_symbols(loaded.market_data.symbols)
        terminated = await terminate_superseded_trigger_coordinators(
            client=temporal.client,
            plans=current_plans,
            active_pipeline_id=loaded.pipeline.version,
        )
        if terminated:
            typer.echo(f"已终止 {len(terminated)} 个旧 pipeline TriggerCoordinator")
        activities = TriggerCoordinatorActivities(
            TriggerAnalysisRequestBuilder(
                config=loaded,
                market_store=SqlMarketDataStore(engine),
                event_store=SqlEventStore(
                    engine,
                    pipeline_id=loaded.pipeline.version,
                    trigger_expiry_seconds=loaded.trigger.trigger_expiry_seconds,
                    max_visible_events=loaded.information.read_limit,
                ),
                state=SqlShadowStateReader(
                    engine,
                    maximum_reconciliation_age_seconds=(
                        loaded.reconciliation.maximum_report_age_seconds
                    ),
                ),
                protection=SqlPortfolioProtectionStore(
                    engine,
                    policy=loaded.risk,
                    initial_equity=loaded.shadow.initial_quote_balance,
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

    loaded, manifest = _load_runtime_release(config, release_manifest)
    if loaded.deployment.stage not in {DeploymentStage.SHADOW, DeploymentStage.TESTNET}:
        raise ValueError("trigger-now 只允许 SHADOW 或 TESTNET")
    if symbol not in loaded.market_data.symbols:
        raise ValueError("symbol 不在当前行情白名单")
    repository = SqlTriggerRepository(_runtime_engine(database_url), loaded.trigger)
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
    release_manifest: Annotated[
        Path,
        typer.Option("--release-manifest", exists=True, dir_okay=False),
    ] = Path("config/release-manifest.yaml"),
) -> None:
    """发现未关闭持仓并运行可恢复的 Temporal 生命周期监控。"""

    loaded, _ = _load_runtime_release(config, release_manifest)
    _require_runtime_database(database_url)

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

    loaded, _ = _load_runtime_release(config, release_manifest)
    _require_runtime_database(database_url)

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

    loaded, _ = _load_runtime_release(config, release_manifest)
    _require_runtime_database(database_url)

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

    loaded, _ = _load_runtime_release(config, release_manifest)
    root = project_root.resolve()
    _require_runtime_database(database_url)

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

    loaded, _ = _load_runtime_release(config, release_manifest)
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
            _runtime_engine(database_url),
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

    from quant_core.dashboard import create_app

    resolved_dist = web_dist if web_dist is not None else _default_web_dist()
    if resolved_dist is None:
        typer.echo("未找到前端构建产物（web/dist）；仅提供 API。")
        typer.echo("先运行：cd web && npm install && npm run build")
    loaded, _ = _load_runtime_release(config, release_manifest)
    _require_runtime_database(database_url)
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


def _default_web_dist() -> Path | None:
    """定位开发仓库的前端产物，不依赖服务进程的当前工作目录。"""

    candidates = (
        Path.cwd() / "web" / "dist",
        Path(__file__).resolve().parents[2] / "web" / "dist",
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    return None


if __name__ == "__main__":
    app()
