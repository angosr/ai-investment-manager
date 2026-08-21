from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer

from investment_manager.entrypoints.cli.root import app
from investment_manager.entrypoints.cli.support import (
    parse_utc_option as _parse_utc_option,
)
from investment_manager.entrypoints.cli.support import runtime_engine as _runtime_engine
from investment_manager.execution.models import Side
from investment_manager.execution.venue.binance import (
    BinanceApiError,
    BinanceCredentials,
    BinanceTestnetClient,
    SymbolRules,
)
from investment_manager.forecast.codex.isolation import audit_codex_isolation
from investment_manager.governance.audit.acceptance import AuditProfile, PhaseAAuditor
from investment_manager.governance.models import (
    build_evaluation_plan_invalidation,
    load_release_manifest,
    validate_manifest_against_config,
    validate_manifest_code_version,
)
from investment_manager.governance.policy import DeploymentStage
from investment_manager.governance.repository import SqlGovernanceRepository
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.legacy.calibration import (
    CalibrationBuildSpec,
    EdgeCalibrationBuilder,
    uncalibrated_ref,
)
from investment_manager.legacy.candidate_evaluation import SqlCandidateOutcomeStore
from investment_manager.legacy.cycle import AnalysisCycle, CycleInput
from investment_manager.risk.protection import SqlPortfolioProtectionStore
from investment_manager.settings import load_config


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

@app.command("diagnose-legacy-analysis-forecasts")
def diagnose_legacy_analysis_forecasts(
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


@app.command("invalidate-evaluation-plan")
def invalidate_evaluation_plan(
    database_url: Annotated[
        str,
        typer.Option(envvar="QUANT_CORE_DATABASE_URL", help="EvaluationPlan 事实库"),
    ],
    plan_id: Annotated[str, typer.Option()],
    reason_code: Annotated[str, typer.Option()],
    evidence_id: Annotated[list[str], typer.Option()],
) -> None:
    """以不可变负面事实使受污染或有缺陷的评价计划永久失效。"""

    governance = SqlGovernanceRepository(_runtime_engine(database_url))
    if governance.get_plan(plan_id) is None:
        raise typer.BadParameter("EvaluationPlan 不存在", param_hint="plan-id")
    canonical_reason = reason_code.strip().upper()
    if not canonical_reason or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
        for character in canonical_reason
    ):
        raise typer.BadParameter("reason-code 必须是大写字母、数字或下划线")
    evidence = tuple(dict.fromkeys(item.strip() for item in evidence_id if item.strip()))
    invalidation = build_evaluation_plan_invalidation(
        plan_id=plan_id,
        invalidated_at=datetime.now(UTC),
        reason_codes=(canonical_reason,),
        evidence_ids=evidence,
    )
    governance.record_failed_experiment(invalidation)
    typer.echo(invalidation.model_dump_json(indent=2))


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


@app.command("challenger-audit")
def challenger_audit(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    release_manifest: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    project_root: Annotated[Path, typer.Option(exists=True, file_okay=False)] = Path("."),
) -> None:
    """验收真实 Codex、Mock 交易的私有 Shadow Challenger。"""

    loaded = load_config(config)
    report = PhaseAAuditor(
        loaded,
        project_root.resolve(),
        profile=AuditProfile.PRIVATE_CODEX_CHALLENGER,
        runtime_manifest=release_manifest.resolve(),
    ).run()
    payload = report.model_dump(mode="json")
    payload["profile"] = AuditProfile.PRIVATE_CODEX_CHALLENGER
    payload["challenger_ready"] = report.ready
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    if not report.ready:
        raise typer.Exit(code=1)


@app.command("codex-isolation-audit")
def codex_isolation_audit(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    release_manifest: Annotated[
        Path,
        typer.Option("--release-manifest", exists=True, dir_okay=False),
    ],
    project_root: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False),
    ] = Path("."),
    audit_catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/isolation-audit-artifacts"
    ),
) -> None:
    """验收已启用账号，并保存绑定精确 Release 的脱敏内容寻址制品。"""

    loaded = load_config(config)
    manifest = load_release_manifest(release_manifest)
    try:
        validate_manifest_against_config(
            manifest,
            loaded,
            require_configuration_hash=True,
        )
        validate_manifest_code_version(
            manifest,
            repository_root=project_root.resolve(),
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="release-manifest") from exc
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
    runtime = loaded.codex_runtime.model_copy(
        update={"enabled": True, "isolation_verified": True}
    )
    audit_parent = loaded.codex_runtime.bundle_root.parent / ".isolation-audits"
    audit_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="investment-manager-codex-isolation-", dir=audit_parent
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
    from investment_manager.governance.audit.isolation import (
        CodexIsolationAuditCatalog,
        build_codex_isolation_audit_artifact,
    )

    artifact = build_codex_isolation_audit_artifact(
        config=loaded,
        manifest=manifest,
        checks=checks,
        audited_at=datetime.now(UTC),
    )
    artifact_path = CodexIsolationAuditCatalog(audit_catalog).store(artifact)
    ready = artifact.ready
    typer.echo(
        json.dumps(
            {
                "ready": ready,
                "runtime_policy_version": runtime.version,
                "reason_code": "OK" if ready else "ISOLATION_AUDIT_FAILED",
                "checks": [check.model_dump(mode="json") for check in checks],
                "artifact_id": artifact.artifact_id,
                "artifact_hash": content_hash(artifact),
                "artifact_path": str(artifact_path),
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
