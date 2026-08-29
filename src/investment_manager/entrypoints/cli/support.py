from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import typer

from investment_manager.governance.models import (
    evaluation_plan_invalidation_id,
    load_release_manifest,
    validate_manifest_against_config,
    validate_manifest_artifacts,
    validate_manifest_code_version,
    validate_runtime_release_checkout,
)
from investment_manager.governance.repository import SqlGovernanceRepository
from investment_manager.market.models import InstrumentId
from investment_manager.platform.database import build_engine, require_current_schema
from investment_manager.settings import AppConfig, load_config


def runtime_engine(database_url: str):
    engine = build_engine(database_url)
    require_current_schema(engine)
    return engine


def require_runtime_database(
    database_url: str,
) -> None:
    engine = runtime_engine(database_url)
    engine.dispose()


def load_runtime_release(config: Path, release_manifest: Path):
    loaded = load_config(config)
    manifest = load_release_manifest(release_manifest)
    validate_manifest_against_config(
        manifest,
        loaded,
        require_configuration_hash=True,
    )
    root = validate_manifest_code_version(manifest)
    validate_runtime_release_checkout(root)
    validate_manifest_artifacts(
        manifest,
        repository_root=root,
        required_ids=("web-dist",),
    )
    return loaded, manifest


def reject_invalidated_evaluation_plan(
    governance: SqlGovernanceRepository,
    plan_id: str,
    *,
    param_hint: str = "plan-id",
) -> None:
    if governance.get_failed_experiment(evaluation_plan_invalidation_id(plan_id)):
        raise typer.BadParameter(
            "EvaluationPlan 已被不可变事实判定失效",
            param_hint=param_hint,
        )


def parse_utc_option(value: str, *, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise typer.BadParameter(f"{name} 必须是 ISO-8601 时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise typer.BadParameter(f"{name} 必须包含时区")
    return parsed.astimezone(UTC)


def parse_research_symbol(value: str) -> str:
    """Validate a public-data research symbol without expanding production scope."""

    canonical = value.upper()
    if not canonical.isalnum():
        raise typer.BadParameter("研究品种只能包含字母和数字", param_hint="symbol")
    return canonical


def observed_market_instruments(config: AppConfig) -> tuple[InstrumentId, ...]:
    """Return Market-owned identities without granting Capital authorization."""

    quote_asset = config.binance_testnet.quote_asset
    spot: list[InstrumentId] = []
    for symbol in config.market_data.symbols:
        base_asset = symbol.removesuffix(quote_asset)
        if not base_asset or f"{base_asset}{quote_asset}" != symbol:
            raise ValueError("Market Spot symbol 无法映射为配置的结算资产")
        spot.append(
            InstrumentId.binance_spot(
                symbol=symbol,
                base_asset=base_asset,
                quote_asset=quote_asset,
            )
        )
    instruments = (*spot, *config.market_data.perpetual_instruments)
    by_key = {item.key: item for item in instruments}
    if len(by_key) != len(instruments):
        raise ValueError("Market 观测产品身份不得重复")
    return tuple(by_key[key] for key in sorted(by_key))
