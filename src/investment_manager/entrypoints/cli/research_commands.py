from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

import typer

from investment_manager.entrypoints.cli.root import app
from investment_manager.entrypoints.cli.support import (
    parse_research_symbol as _parse_research_symbol,
)
from investment_manager.entrypoints.cli.support import (
    parse_utc_option as _parse_utc_option,
)
from investment_manager.entrypoints.cli.support import (
    reject_invalidated_evaluation_plan as _reject_invalidated_evaluation_plan,
)
from investment_manager.entrypoints.cli.support import (
    runtime_engine as _runtime_engine,
)
from investment_manager.governance.models import current_clean_code_version
from investment_manager.governance.repository import SqlGovernanceRepository
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.scheduling.repository import SqlTriggerRepository
from investment_manager.settings import load_config


@app.command("fetch-binance-history")
def fetch_binance_history_command(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    symbol: Annotated[str, typer.Option()],
    start: Annotated[str, typer.Option(help="带时区的 ISO-8601 起点（含）")],
    end: Annotated[str, typer.Option(help="带时区的 ISO-8601 终点（不含）")],
    interval: Annotated[
        str | None,
        typer.Option(help="研究 K 线周期；省略时沿用 MarketDataPolicy"),
    ] = None,
    catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(".runtime/datasets"),
) -> None:
    """抓取并内容寻址保存 Binance 官方已收盘 K 线；不生成 Markdown 报告。"""

    from investment_manager.research.dataset import (
        HistoricalDatasetCatalog,
        fetch_binance_history,
    )

    loaded = load_config(config)
    canonical_symbol = _parse_research_symbol(symbol)
    dataset = asyncio.run(
        fetch_binance_history(
            base_url=loaded.market_data.rest_base_url,
            symbol=canonical_symbol,
            interval=interval or loaded.market_data.interval,
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


@app.command("fetch-binance-usdm-history")
def fetch_binance_usdm_history_command(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    symbol: Annotated[str, typer.Option()],
    start: Annotated[str, typer.Option(help="带时区的 ISO-8601 起点（含）")],
    end: Annotated[str, typer.Option(help="带时区的 ISO-8601 终点（不含）")],
    interval: Annotated[str, typer.Option(help="USD-M 合约交易价 K 线周期")],
    catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(".runtime/datasets"),
) -> None:
    """冻结 Binance USD-M 合约交易价 K 线；不把成交价冒充 bid/ask。"""

    from investment_manager.research.dataset import (
        HistoricalDatasetCatalog,
        fetch_binance_usdm_history,
    )

    loaded = load_config(config)
    canonical_symbol = _parse_research_symbol(symbol)
    dataset = asyncio.run(
        fetch_binance_usdm_history(
            base_url="https://fapi.binance.com",
            symbol=canonical_symbol,
            interval=interval,
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
                "source": dataset.manifest.source,
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

    from investment_manager.research.dataset import (
        HistoricalFundingDatasetCatalog,
        fetch_binance_funding_history,
    )

    loaded = load_config(config)
    canonical_symbol = _parse_research_symbol(symbol)
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


@app.command("fetch-binance-carry-history")
def fetch_binance_carry_history_command(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    spot_dataset_id: Annotated[str, typer.Option()],
    funding_dataset_id: Annotated[str, typer.Option()],
    spot_catalog: Annotated[Path, typer.Option(exists=True, file_okay=False)] = Path(
        ".runtime/datasets"
    ),
    funding_catalog: Annotated[
        Path, typer.Option(exists=True, file_okay=False)
    ] = Path(".runtime/funding-datasets"),
    carry_catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/carry-datasets"
    ),
) -> None:
    """冻结 carry 所需 USD-M 价格与逐次结算标记价；不创建交易适配器。"""

    from investment_manager.research.carry import (
        HistoricalCarryDatasetCatalog,
        fetch_binance_carry_history,
    )
    from investment_manager.research.dataset import (
        HistoricalDatasetCatalog,
        HistoricalFundingDatasetCatalog,
    )

    loaded = load_config(config)
    spot_dataset = HistoricalDatasetCatalog(spot_catalog).load(spot_dataset_id)
    funding_dataset = HistoricalFundingDatasetCatalog(funding_catalog).load(
        funding_dataset_id
    )
    try:
        dataset = asyncio.run(
            fetch_binance_carry_history(
                base_url="https://fapi.binance.com",
                spot_dataset=spot_dataset,
                funding_dataset=funding_dataset,
                timeout_seconds=loaded.market_data.rest_timeout_seconds,
            )
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="spot-dataset-id") from exc
    target = HistoricalCarryDatasetCatalog(carry_catalog).store(dataset)
    typer.echo(
        json.dumps(
            {
                "carry_dataset_id": dataset.manifest.dataset_id,
                "symbol": dataset.manifest.symbol,
                "day_count": dataset.manifest.day_count,
                "settlement_count": dataset.manifest.settlement_count,
                "days_hash": dataset.manifest.days_hash,
                "settlements_hash": dataset.manifest.settlements_hash,
                "rule_snapshot_as_of": dataset.manifest.collected_at.isoformat(),
                "path": str(target),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("diagnose-dynamic-carry-history")
def diagnose_dynamic_carry_history_command(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    carry_dataset_id: Annotated[str, typer.Option()],
    start: Annotated[
        str | None,
        typer.Option(help="可选的带时区 UTC 日线开盘起点（含）"),
    ] = None,
    end: Annotated[
        str | None,
        typer.Option(help="可选的带时区 UTC 评价终点（不含）"),
    ] = None,
    carry_catalog: Annotated[Path, typer.Option(exists=True, file_okay=False)] = Path(
        ".runtime/carry-datasets"
    ),
    spot_catalog: Annotated[Path, typer.Option(exists=True, file_okay=False)] = Path(
        ".runtime/datasets"
    ),
    result_catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/dynamic-carry-replays"
    ),
) -> None:
    """以生产规则做日开盘点时诊断；结果只能用于淘汰弱候选。"""

    from datetime import timedelta

    from investment_manager.research.carry import HistoricalCarryDatasetCatalog
    from investment_manager.research.dataset import HistoricalDatasetCatalog
    from investment_manager.research.dynamic_carry import (
        DynamicCarryReplayCatalog,
        replay_policy_from_config,
        run_dynamic_carry_replay,
    )

    loaded = load_config(config)
    carry = HistoricalCarryDatasetCatalog(carry_catalog).load(carry_dataset_id)
    spot = HistoricalDatasetCatalog(spot_catalog).load(
        carry.manifest.spot_dataset_id
    )
    replay_start = (
        _parse_utc_option(start, name="start")
        if start is not None
        else carry.days[0].open_time
    )
    replay_end = (
        _parse_utc_option(end, name="end")
        if end is not None
        else carry.days[-1].close_time + timedelta(microseconds=1)
    )
    try:
        result = run_dynamic_carry_replay(
            carry_dataset=carry,
            spot_dataset=spot,
            policy=replay_policy_from_config(loaded),
            starting_equity=loaded.shadow.initial_quote_balance,
            start=replay_start,
            end=replay_end,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="carry-dataset-id") from exc
    target = DynamicCarryReplayCatalog(result_catalog).store(result)
    typer.echo(
        json.dumps(
            {
                "result_id": result.result_id,
                "evidence_scope": result.evidence_scope,
                "carry_dataset_id": result.carry_dataset_id,
                "start": result.start.isoformat(),
                "end": result.end.isoformat(),
                "metrics": result.metrics.model_dump(mode="json"),
                "limitations": result.limitations,
                "path": str(target),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("diagnose-dynamic-carry-intraday")
def diagnose_dynamic_carry_intraday_command(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    carry_dataset_id: Annotated[str, typer.Option()],
    spot_dataset_id: Annotated[str, typer.Option()],
    perpetual_dataset_id: Annotated[str, typer.Option()],
    start: Annotated[str | None, typer.Option(help="可选 UTC 起点（含）")] = None,
    end: Annotated[str | None, typer.Option(help="可选 UTC 终点（不含）")] = None,
    carry_catalog: Annotated[Path, typer.Option(exists=True, file_okay=False)] = Path(
        ".runtime/carry-datasets"
    ),
    dataset_catalog: Annotated[Path, typer.Option(exists=True, file_okay=False)] = Path(
        ".runtime/datasets"
    ),
    result_catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/dynamic-carry-replays"
    ),
) -> None:
    """以双市场盘中成交价做乐观诊断；不能代替可成交报价回测。"""

    from investment_manager.research.carry import HistoricalCarryDatasetCatalog
    from investment_manager.research.dataset import HistoricalDatasetCatalog
    from investment_manager.research.dynamic_carry import (
        DynamicCarryReplayCatalog,
        intraday_replay_policy_from_config,
        run_dynamic_carry_intraday_replay,
    )

    loaded = load_config(config)
    carry = HistoricalCarryDatasetCatalog(carry_catalog).load(carry_dataset_id)
    datasets = HistoricalDatasetCatalog(dataset_catalog)
    spot = datasets.load(spot_dataset_id)
    perpetual = datasets.load(perpetual_dataset_id)
    interval = spot.manifest.interval
    if not interval.endswith("m") or not interval[:-1].isdigit():
        raise typer.BadParameter("盘中诊断仅接受分钟 K 线", param_hint="spot-dataset-id")
    replay_start = (
        _parse_utc_option(start, name="start")
        if start is not None
        else max(
            carry.manifest.requested_start,
            spot.manifest.first_open_time,
            perpetual.manifest.first_open_time,
        )
    )
    replay_end = (
        _parse_utc_option(end, name="end")
        if end is not None
        else min(
            carry.manifest.requested_end,
            spot.manifest.requested_end,
            perpetual.manifest.requested_end,
        )
    )
    try:
        result = run_dynamic_carry_intraday_replay(
            carry_dataset=carry,
            spot_dataset=spot,
            perpetual_dataset=perpetual,
            policy=intraday_replay_policy_from_config(
                loaded,
                bar_interval_minutes=int(interval[:-1]),
            ),
            starting_equity=loaded.shadow.initial_quote_balance,
            start=replay_start,
            end=replay_end,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="spot-dataset-id") from exc
    target = DynamicCarryReplayCatalog(result_catalog).store(result)
    typer.echo(
        json.dumps(
            {
                "result_id": result.result_id,
                "evidence_scope": result.evidence_scope,
                "start": result.start.isoformat(),
                "end": result.end.isoformat(),
                "phases": [
                    {
                        "phase_offset_minutes": item.phase_offset_minutes,
                        "metrics": item.metrics.model_dump(mode="json"),
                    }
                    for item in result.phases
                ],
                "limitations": result.limitations,
                "path": str(target),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("carry-walk-forward")
def carry_walk_forward_command(
    database_url: Annotated[
        str,
        typer.Option(envvar="INVESTMENT_MANAGER_DATABASE_URL", help="EvaluationPlan 事实库"),
    ],
    carry_dataset_id: Annotated[str, typer.Option()],
    plan_id: Annotated[str, typer.Option()],
    policy_version: Annotated[
        str,
        typer.Option(help="只允许精确登记的 carry 风险规格"),
    ] = "spot-perp-monthly-50pct-v1",
    carry_catalog: Annotated[Path, typer.Option(exists=True, file_okay=False)] = Path(
        ".runtime/carry-datasets"
    ),
    spot_catalog: Annotated[Path, typer.Option(exists=True, file_okay=False)] = Path(
        ".runtime/datasets"
    ),
    evaluation_catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/carry-evaluations"
    ),
    register_only: Annotated[
        bool, typer.Option(help="只预登记固定 carry 策略、数据与门禁")
    ] = False,
) -> None:
    """预登记或评价固定现货/永续 carry；不调用模型或生产执行。"""

    from investment_manager.governance.repository import SqlGovernanceRepository
    from investment_manager.research.carry import HistoricalCarryDatasetCatalog
    from investment_manager.research.carry_evaluation import (
        CarryEvaluationCatalog,
        CarryEvaluationSpec,
        CarryWalkForwardPlan,
        build_carry_evaluation_plan,
        failed_carry_walk_forward_experiment,
        resolve_carry_policy,
        run_carry_walk_forward,
        validate_carry_evaluation_plan,
    )
    from investment_manager.research.carry_forward import current_carry_evaluator_environment
    from investment_manager.research.dataset import HistoricalDatasetCatalog

    governance = SqlGovernanceRepository(_runtime_engine(database_url))
    champion = governance.get_champion()
    registered = governance.get_plan(plan_id)
    if register_only:
        carry_dataset = HistoricalCarryDatasetCatalog(carry_catalog).load(
            carry_dataset_id
        )
        spot_dataset = HistoricalDatasetCatalog(spot_catalog).load(
            carry_dataset.manifest.spot_dataset_id
        )
        spec = CarryEvaluationSpec.freeze(
            carry_dataset=carry_dataset,
            spot_dataset=spot_dataset,
            evaluator_code_version=current_clean_code_version(),
            evaluator_environment=current_carry_evaluator_environment(),
            policy=resolve_carry_policy(policy_version),
            plan=CarryWalkForwardPlan(plan_id=plan_id),
        )
        registered = build_carry_evaluation_plan(
            spec=spec,
            base_manifest_id=champion.manifest_id,
            registered_at=datetime.now(UTC),
        )
        governance.register_plan(registered)
        typer.echo(
            json.dumps(
                {
                    "evaluation_plan": registered.model_dump(mode="json"),
                    "carry_spec": spec.model_dump(mode="json"),
                    "carry_spec_hash": content_hash(spec),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if registered is None or registered.candidate_spec_snapshot is None:
        raise typer.BadParameter(
            "carry EvaluationPlan 尚未预登记；先以相同参数执行 --register-only",
            param_hint="plan-id",
        )
    _reject_invalidated_evaluation_plan(governance, plan_id)
    try:
        spec = CarryEvaluationSpec.model_validate(
            registered.candidate_spec_snapshot
        )
        if spec.policy.version != policy_version:
            raise ValueError("carry policy version 与预登记规格不一致")
        if spec.carry_dataset_id != carry_dataset_id:
            raise ValueError("调用方 carry 数据集与预登记规格不一致")
        carry_dataset = HistoricalCarryDatasetCatalog(carry_catalog).load(
            spec.carry_dataset_id
        )
        spot_dataset = HistoricalDatasetCatalog(spot_catalog).load(
            spec.spot_dataset_id
        )
        validate_carry_evaluation_plan(
            spec=spec,
            plan=registered,
            champion_manifest_id=champion.manifest_id,
            evaluated_at=datetime.now(UTC),
            evaluator_code_version=current_clean_code_version(),
            evaluator_environment=current_carry_evaluator_environment(),
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="plan-id") from exc
    result = run_carry_walk_forward(
        carry_dataset=carry_dataset,
        spot_dataset=spot_dataset,
        policy=spec.policy,
        plan=spec.plan,
        evaluation_spec_hash=content_hash(spec),
    )
    result_path = CarryEvaluationCatalog(evaluation_catalog).store(result)
    if not result.passed:
        governance.record_failed_experiment(
            failed_carry_walk_forward_experiment(
                result,
                rejected_at=datetime.now(UTC),
            )
        )
    payload = result.model_dump(mode="json", exclude={"folds": {"__all__": {"run"}}})
    payload["result_path"] = str(result_path)
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("carry-blind-evaluate")
def carry_blind_evaluate_command(
    database_url: Annotated[
        str,
        typer.Option(envvar="INVESTMENT_MANAGER_DATABASE_URL", help="一次性盲测事实库"),
    ],
    source_evaluation_id: Annotated[str, typer.Option()],
    carry_catalog: Annotated[Path, typer.Option(exists=True, file_okay=False)] = Path(
        ".runtime/carry-datasets"
    ),
    spot_catalog: Annotated[Path, typer.Option(exists=True, file_okay=False)] = Path(
        ".runtime/datasets"
    ),
    evaluation_catalog: Annotated[Path, typer.Option(exists=True, file_okay=False)] = Path(
        ".runtime/carry-evaluations"
    ),
    blind_catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/carry-blind-evaluations"
    ),
) -> None:
    """原子消费固定 carry 策略的唯一尾窗；失败或重叠时不读取标签。"""

    from investment_manager.governance.models import BlindEvaluationClaim
    from investment_manager.governance.repository import SqlGovernanceRepository
    from investment_manager.research.carry import HistoricalCarryDatasetCatalog
    from investment_manager.research.carry_evaluation import (
        CarryBlindCatalog,
        CarryEvaluationCatalog,
        CarryEvaluationSpec,
        failed_carry_blind_experiment,
        run_carry_blind_evaluation,
        validate_carry_evaluation_plan,
    )
    from investment_manager.research.carry_forward import current_carry_evaluator_environment
    from investment_manager.research.dataset import HistoricalDatasetCatalog

    source = CarryEvaluationCatalog(evaluation_catalog).load(source_evaluation_id)
    if not source.passed or source.evaluation_spec_hash is None:
        raise typer.BadParameter(
            "源 carry walk-forward 尚未通过完整门禁",
            param_hint="source-evaluation-id",
        )
    governance = SqlGovernanceRepository(_runtime_engine(database_url))
    registered = governance.get_plan(source.plan.plan_id)
    if registered is None or registered.candidate_spec_snapshot is None:
        raise typer.BadParameter("源 carry 评价没有预登记规格")
    _reject_invalidated_evaluation_plan(
        governance,
        source.plan.plan_id,
        param_hint="source-evaluation-id",
    )
    try:
        spec = CarryEvaluationSpec.model_validate(registered.candidate_spec_snapshot)
        if (
            source.dataset_id != spec.carry_dataset_id
            or source.spot_dataset_id != spec.spot_dataset_id
            or source.funding_dataset_id != spec.funding_dataset_id
            or source.policy != spec.policy
            or source.plan != spec.plan
            or source.evaluation_spec_hash != content_hash(spec)
        ):
            raise ValueError("源 carry 结果与预登记规格不一致")
        validate_carry_evaluation_plan(
            spec=spec,
            plan=registered,
            champion_manifest_id=governance.get_champion().manifest_id,
            evaluated_at=datetime.now(UTC),
            evaluator_code_version=current_clean_code_version(),
            evaluator_environment=current_carry_evaluator_environment(),
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="source-evaluation-id") from exc
    symbol = HistoricalCarryDatasetCatalog(carry_catalog).load_manifest(
        spec.carry_dataset_id
    ).symbol
    scope_id = stable_id(
        "blind_evaluation_scope", symbol, source.blind_start, source.blind_end
    )
    query_id = stable_id(
        "carry_blind_query",
        source.plan.plan_id,
        source.evaluation_id,
        source.evaluation_spec_hash,
    )
    try:
        claim = governance.claim_blind_evaluation(
            BlindEvaluationClaim(
                query_id=query_id,
                blind_scope_id=scope_id,
                blind_symbol=symbol,
                blind_start=source.blind_start,
                blind_end=source.blind_end,
                plan_id=source.plan.plan_id,
                source_evaluation_id=source.evaluation_id,
                claimed_at=datetime.now(UTC),
            )
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="source-evaluation-id") from exc
    catalog = CarryBlindCatalog(blind_catalog)
    if claim.completed_at is not None:
        assert claim.result_id is not None and claim.result_hash is not None
        result = catalog.load(claim.result_id)
        if content_hash(result) != claim.result_hash:
            raise RuntimeError("已完成 carry 盲测的事实库哈希与结果制品不一致")
        result_path = blind_catalog.resolve() / f"{claim.result_id}.json"
    else:
        carry_dataset = HistoricalCarryDatasetCatalog(carry_catalog).load(
            spec.carry_dataset_id
        )
        spot_dataset = HistoricalDatasetCatalog(spot_catalog).load(spec.spot_dataset_id)
        result = run_carry_blind_evaluation(
            source=source,
            query_id=query_id,
            carry_dataset=carry_dataset,
            spot_dataset=spot_dataset,
        )
        result_path = catalog.store(result)
        governance.complete_blind_evaluation(
            claim.model_copy(
                update={
                    "completed_at": datetime.now(UTC),
                    "result_id": result.result_id,
                    "result_hash": content_hash(result),
                }
            )
        )
    if not result.passed:
        governance.record_failed_experiment(
            failed_carry_blind_experiment(
                result,
                rejected_at=datetime.now(UTC),
            )
        )
    payload = result.model_dump(mode="json")
    payload["result_path"] = str(result_path)
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("register-carry-forward-plan")
def register_carry_forward_plan_command(
    database_url: Annotated[
        str,
        typer.Option(envvar="INVESTMENT_MANAGER_DATABASE_URL", help="EvaluationPlan 事实库"),
    ],
    plan_id: Annotated[str, typer.Option()],
    symbol: Annotated[str, typer.Option()],
    observation_start: Annotated[str, typer.Option()],
    observation_end: Annotated[str, typer.Option()],
    policy_version: Annotated[
        str,
        typer.Option(help="只允许精确登记的 carry 风险规格"),
    ] = "spot-perp-monthly-50pct-v1",
) -> None:
    """在未来数据产生前冻结 carry forward 窗口、策略与全部门禁。"""

    from investment_manager.research.carry_evaluation import resolve_carry_policy
    from investment_manager.research.carry_forward import (
        CarryForwardEvaluationSpec,
        build_carry_forward_evaluation_plan,
        current_carry_evaluator_environment,
    )

    governance = SqlGovernanceRepository(_runtime_engine(database_url))
    base_manifest_id = governance.get_champion().manifest_id
    registered_at = datetime.now(UTC)
    try:
        spec = CarryForwardEvaluationSpec(
            plan_id=plan_id,
            base_manifest_id=base_manifest_id,
            evaluator_code_version=current_clean_code_version(),
            evaluator_environment=current_carry_evaluator_environment(),
            symbol=_parse_research_symbol(symbol),
            observation_start=_parse_utc_option(
                observation_start, name="observation-start"
            ),
            observation_end=_parse_utc_option(
                observation_end, name="observation-end"
            ),
            policy=resolve_carry_policy(policy_version),
        )
        plan = build_carry_forward_evaluation_plan(
            spec=spec,
            base_manifest_id=base_manifest_id,
            registered_at=registered_at,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    governance.register_plan(plan)
    typer.echo(
        json.dumps(
            {
                "evaluation_plan": plan.model_dump(mode="json"),
                "carry_forward_spec": spec.model_dump(mode="json"),
                "carry_forward_spec_hash": content_hash(spec),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("evaluate-carry-forward-plan")
def evaluate_carry_forward_plan_command(
    database_url: Annotated[
        str,
        typer.Option(envvar="INVESTMENT_MANAGER_DATABASE_URL", help="EvaluationPlan 事实库"),
    ],
    plan_id: Annotated[str, typer.Option()],
    carry_dataset_id: Annotated[str, typer.Option()],
    carry_catalog: Annotated[Path, typer.Option(exists=True, file_okay=False)] = Path(
        ".runtime/carry-datasets"
    ),
    spot_catalog: Annotated[Path, typer.Option(exists=True, file_okay=False)] = Path(
        ".runtime/datasets"
    ),
    funding_catalog: Annotated[
        Path, typer.Option(exists=True, file_okay=False)
    ] = Path(".runtime/funding-datasets"),
    evaluation_catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/carry-forward-evaluations"
    ),
) -> None:
    """窗口成熟后按预登记合同评价精确同窗口 carry 数据。"""

    from investment_manager.research.carry import HistoricalCarryDatasetCatalog
    from investment_manager.research.carry_forward import (
        CarryForwardCatalog,
        CarryForwardEvaluationSpec,
        current_carry_evaluator_environment,
        failed_carry_forward_experiment,
        run_carry_forward_evaluation,
        validate_carry_forward_evaluation_plan,
    )
    from investment_manager.research.dataset import (
        HistoricalDatasetCatalog,
        HistoricalFundingDatasetCatalog,
    )

    governance = SqlGovernanceRepository(_runtime_engine(database_url))
    registered = governance.get_plan(plan_id)
    if registered is None or registered.candidate_spec_snapshot is None:
        raise typer.BadParameter(
            "carry forward EvaluationPlan 不存在", param_hint="plan-id"
        )
    _reject_invalidated_evaluation_plan(governance, plan_id)
    evaluated_at = datetime.now(UTC)
    try:
        spec = CarryForwardEvaluationSpec.model_validate(
            registered.candidate_spec_snapshot
        )
        # 必须先证明窗口成熟，再允许调用方指定的数据 ID 触及标签。
        validate_carry_forward_evaluation_plan(
            spec=spec,
            plan=registered,
            evaluated_at=evaluated_at,
            evaluator_code_version=current_clean_code_version(),
            evaluator_environment=current_carry_evaluator_environment(),
        )
        carry_dataset = HistoricalCarryDatasetCatalog(carry_catalog).load(
            carry_dataset_id
        )
        spot_dataset = HistoricalDatasetCatalog(spot_catalog).load(
            carry_dataset.manifest.spot_dataset_id
        )
        funding_dataset = HistoricalFundingDatasetCatalog(funding_catalog).load(
            carry_dataset.manifest.funding_dataset_id
        )
        result = run_carry_forward_evaluation(
            spec=spec,
            carry_dataset=carry_dataset,
            spot_dataset=spot_dataset,
            funding_dataset=funding_dataset,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="plan-id") from exc
    result_path = CarryForwardCatalog(evaluation_catalog).store(result)
    if not result.passed:
        governance.record_failed_experiment(
            failed_carry_forward_experiment(result, rejected_at=evaluated_at)
        )
    payload = result.model_dump(
        mode="json",
        exclude={"months": {"__all__": {"run"}}},
    )
    payload["result_path"] = str(result_path)
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("screen-signals")
def screen_signals_command(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    dataset_id: Annotated[str, typer.Option()],
    signal_start: Annotated[str, typer.Option(help="开发窗口起点，必须包含时区")],
    signal_end: Annotated[
        str,
        typer.Option(help="开发标签终点，任何样本不得跨越该边界"),
    ],
    candidate: Annotated[str, typer.Option()] = "configured",
    event_dataset_id: Annotated[str | None, typer.Option()] = None,
    catalog: Annotated[Path, typer.Option(exists=True, file_okay=False)] = Path(
        ".runtime/datasets"
    ),
    event_catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/event-datasets"
    ),
    spread_bps: Annotated[str, typer.Option()] = "1",
    minimum_non_overlapping_samples: Annotated[int, typer.Option(min=2)] = 30,
    minimum_net_return_bps_lower_bound: Annotated[str, typer.Option()] = "0",
    minimum_incremental_return_bps_lower_bound: Annotated[str, typer.Option()] = "0",
) -> None:
    """用轻量原始信号机会筛选淘汰弱假设；不能授予交易资格。"""

    from investment_manager.research.candidates import resolve_research_candidate
    from investment_manager.research.dataset import (
        HistoricalDatasetCatalog,
        HistoricalEventDatasetCatalog,
    )
    from investment_manager.research.screening import run_raw_signal_screen

    try:
        parsed_spread = Decimal(spread_bps)
        parsed_net_gate = Decimal(minimum_net_return_bps_lower_bound)
        parsed_incremental_gate = Decimal(
            minimum_incremental_return_bps_lower_bound
        )
    except InvalidOperation as exc:
        raise typer.BadParameter("快速筛选成本和门槛必须是十进制数") from exc
    if parsed_spread < 0:
        raise typer.BadParameter("spread-bps 不能为负", param_hint="spread-bps")
    loaded_config = load_config(config)
    try:
        effective_config, research_strategy = resolve_research_candidate(
            candidate,
            loaded_config,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="candidate") from exc
    event_dataset = (
        HistoricalEventDatasetCatalog(event_catalog).load(event_dataset_id)
        if event_dataset_id is not None
        else None
    )
    parsed_start = _parse_utc_option(signal_start, name="signal-start")
    parsed_end = _parse_utc_option(signal_end, name="signal-end")
    dataset = HistoricalDatasetCatalog(catalog).load_window(
        dataset_id,
        start=parsed_start,
        end=parsed_end,
        warmup_bars=effective_config.market_data.bar_window - 1,
    )
    result = run_raw_signal_screen(
        dataset=dataset,
        event_dataset=event_dataset,
        config=effective_config,
        strategy=research_strategy,
        signal_start=parsed_start,
        signal_end=parsed_end,
        spread_bps=parsed_spread,
        minimum_non_overlapping_samples=minimum_non_overlapping_samples,
        minimum_net_return_bps_lower_bound=parsed_net_gate,
        minimum_incremental_return_bps_lower_bound=parsed_incremental_gate,
    )
    typer.echo(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))


@app.command("walk-forward")
def walk_forward_command(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[
        str,
        typer.Option(envvar="INVESTMENT_MANAGER_DATABASE_URL", help="EvaluationPlan 事实库"),
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

    from investment_manager.governance.repository import SqlGovernanceRepository
    from investment_manager.research.candidates import resolve_research_candidate
    from investment_manager.research.dataset import (
        HistoricalDatasetCatalog,
        HistoricalEventDatasetCatalog,
        HistoricalFundingDatasetCatalog,
    )
    from investment_manager.research.evaluation_catalog import HistoricalEvaluationCatalog
    from investment_manager.research.walk_forward import (
        WalkForwardEvaluationSpec,
        WalkForwardPlan,
        build_walk_forward_evaluation_plan,
        failed_walk_forward_experiment,
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
    governance = SqlGovernanceRepository(_runtime_engine(database_url))
    if not register_only:
        _reject_invalidated_evaluation_plan(governance, plan_id)
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
    if not result.passed:
        governance.record_failed_experiment(
            failed_walk_forward_experiment(
                result,
                rejected_at=datetime.now(UTC),
            )
        )
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
        typer.Option(envvar="INVESTMENT_MANAGER_DATABASE_URL", help="EvaluationPlan 事实库"),
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

    from investment_manager.governance.models import BlindEvaluationClaim
    from investment_manager.governance.repository import SqlGovernanceRepository
    from investment_manager.research.candidates import resolve_research_candidate
    from investment_manager.research.dataset import (
        HistoricalDatasetCatalog,
        HistoricalEventDatasetCatalog,
        HistoricalFundingDatasetCatalog,
    )
    from investment_manager.research.evaluation_catalog import (
        BlindEvaluationCatalog,
        HistoricalEvaluationCatalog,
    )
    from investment_manager.research.walk_forward import (
        WalkForwardEvaluationSpec,
        blind_evaluation_scope,
        failed_blind_experiment,
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
    _reject_invalidated_evaluation_plan(
        governance,
        source.plan.plan_id,
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
    if not result.passed:
        governance.record_failed_experiment(
            failed_blind_experiment(
                result,
                rejected_at=datetime.now(UTC),
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
        typer.Option(envvar="INVESTMENT_MANAGER_DATABASE_URL", help="点时事件事实库"),
    ],
    start: Annotated[str, typer.Option(help="按 observed_at 过滤的含时区起点（含）")],
    end: Annotated[str, typer.Option(help="按 observed_at 过滤的含时区终点（不含）")],
    catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/event-datasets"
    ),
) -> None:
    """冻结真实到达时间的标准事件；不为事后新闻猜测 observed_at。"""

    from sqlalchemy import select

    from investment_manager.information.models import IntelligenceEvent
    from investment_manager.information.tables import normalized_events
    from investment_manager.research.dataset import (
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
        source="investment-manager-normalized-events",
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
        typer.Option(envvar="INVESTMENT_MANAGER_DATABASE_URL", help="冻结 TriggerPlan 事实库"),
    ],
    event_dataset_id: Annotated[str, typer.Option()],
    replay_start: Annotated[str, typer.Option(help="带时区的回放起点（含）")],
    replay_end: Annotated[str, typer.Option(help="带时区的回放终点（不含）")],
    analysis_duration_seconds: Annotated[int, typer.Option(min=0)],
    admission_order: Annotated[
        str | None,
        typer.Option(help="同刻争用全局防重复间隔的品种顺序，逗号分隔；默认配置顺序"),
    ] = None,
    event_catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/event-datasets"
    ),
    include_batches: Annotated[bool, typer.Option()] = False,
) -> None:
    """复用生产协调规则回放全品种外部事件批次；不调用 Codex 或交易。"""

    from sqlalchemy import select

    from investment_manager.legacy.repository import analysis_cycles, market_snapshots
    from investment_manager.research.dataset import HistoricalEventDatasetCatalog
    from investment_manager.research.trigger_replay import (
        ExternalTriggerReplaySpec,
        TriggerReplayInitialScopeState,
        run_external_trigger_replay,
    )
    from investment_manager.scheduling.tables import analysis_call_admissions

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
        initial_global_last_admitted_at = connection.execute(
            select(analysis_call_admissions.c.admitted_at)
            .where(analysis_call_admissions.c.admitted_at < window_start)
            .order_by(analysis_call_admissions.c.admitted_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        initial_scope_items = []
        for symbol in loaded.market_data.symbols:
            completed = connection.execute(
                select(analysis_cycles.c.created_at)
                .join(
                    market_snapshots,
                    market_snapshots.c.cycle_id == analysis_cycles.c.cycle_id,
                )
                .where(
                    analysis_cycles.c.pipeline_version == loaded.pipeline.version,
                    market_snapshots.c.symbol == symbol,
                    analysis_cycles.c.created_at < window_start,
                )
                .order_by(analysis_cycles.c.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if completed is not None:
                initial_scope_items.append(
                    TriggerReplayInitialScopeState(
                        symbol=symbol,
                        last_analysis_at=completed,
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
            initial_global_last_admitted_at=initial_global_last_admitted_at,
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
    blind_evaluation_catalog: Annotated[
        Path, typer.Option(file_okay=False)
    ] = Path(".runtime/blind-evaluations"),
) -> None:
    """派生历史实验的最终证据状态、累计尝试与歧义；不改写制品。"""

    from investment_manager.research.evaluation_catalog import (
        BlindEvaluationCatalog,
        HistoricalEvaluationCatalog,
    )

    summaries = HistoricalEvaluationCatalog(evaluation_catalog).summaries(
        blind_catalog=BlindEvaluationCatalog(blind_evaluation_catalog)
    )
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
        typer.Option(envvar="INVESTMENT_MANAGER_DATABASE_URL", help="前瞻决策带事实库"),
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

    from investment_manager.governance.repository import SqlGovernanceRepository
    from investment_manager.research.candidates import resolve_research_candidate
    from investment_manager.research.dataset import HistoricalDatasetCatalog
    from investment_manager.research.decision_tape import (
        ForecastGateEvaluationSpec,
        ForecastGatePolicy,
        SqlForecastDecisionTapeReader,
        build_forecast_gate_evaluation_plan,
        run_paired_decision_tape_backtest,
        validate_forecast_gate_baseline,
        validate_forecast_gate_evaluation_plan,
    )
    from investment_manager.research.evaluation_catalog import BlindEvaluationCatalog

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
        _reject_invalidated_evaluation_plan(governance, plan_id)
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
