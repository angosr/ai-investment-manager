from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from investment_manager.entrypoints.cli.research_root import app
from investment_manager.entrypoints.cli.support import (
    parse_utc_option as _parse_utc_option,
)
from investment_manager.entrypoints.cli.support import runtime_engine as _runtime_engine
from investment_manager.execution.models import Side
from investment_manager.legacy.calibration import (
    CalibrationBuildSpec,
    EdgeCalibrationBuilder,
    uncalibrated_ref,
)
from investment_manager.legacy.candidate_evaluation import SqlCandidateOutcomeStore
from investment_manager.settings import load_config


@app.command("build-edge-calibration")
def build_edge_calibration(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[
        str,
        typer.Option(envvar="INVESTMENT_MANAGER_DATABASE_URL", help="仅从受控环境注入数据库 URL"),
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


@app.command("diagnose-legacy-analysis-forecasts")
def diagnose_legacy_analysis_forecasts(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[
        str,
        typer.Option(envvar="INVESTMENT_MANAGER_DATABASE_URL", help="仅从受控环境注入数据库 URL"),
    ],
    window_start: Annotated[str, typer.Option(help="带时区的 ISO-8601 时间（含）")],
    window_end: Annotated[str, typer.Option(help="带时区的 ISO-8601 时间（不含）")],
    published_at: Annotated[str, typer.Option(help="评价事实发布时间")],
    pipeline_version: Annotated[str | None, typer.Option()] = None,
    analysis_behavior_hash: Annotated[str | None, typer.Option()] = None,
    minimum_non_overlapping_samples: Annotated[int, typer.Option(min=2)] = 30,
) -> None:
    """评价结果发生前冻结的 AI 方向预测；不把方向收益冒充可交易 PnL。"""

    from investment_manager.legacy.forecast_evaluation import (
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
