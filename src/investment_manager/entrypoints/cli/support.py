from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import typer

from investment_manager.governance.models import (
    evaluation_plan_invalidation_id,
    load_release_manifest,
    validate_manifest_against_config,
    validate_manifest_code_version,
)
from investment_manager.governance.repository import SqlGovernanceRepository
from investment_manager.platform.database import build_engine, require_current_schema
from investment_manager.platform.fact_store import FactStoreRole, require_fact_store_role
from investment_manager.settings import AppConfig, load_config


def configured_fact_store_role(config: AppConfig) -> FactStoreRole | None:
    enabled = tuple(
        role
        for active, role in (
            (config.capital.enabled, FactStoreRole.CAPITAL),
            (config.assessment.enabled, FactStoreRole.CONTEXT),
        )
        if active
    )
    if len(enabled) > 1:
        raise RuntimeError("Capital 与 Context 不得共享一个运行事实库")
    return enabled[0] if enabled else None


def runtime_engine(
    database_url: str,
    *,
    fact_store_role: FactStoreRole | None = None,
    claim_fact_store: bool = False,
):
    engine = build_engine(database_url)
    require_current_schema(engine)
    if fact_store_role is not None:
        require_fact_store_role(
            engine,
            fact_store_role,
            claim_if_missing=claim_fact_store,
        )
    return engine


def require_runtime_database(
    database_url: str,
    *,
    config: AppConfig | None = None,
    claim_fact_store: bool = False,
) -> None:
    engine = runtime_engine(
        database_url,
        fact_store_role=(configured_fact_store_role(config) if config is not None else None),
        claim_fact_store=claim_fact_store,
    )
    engine.dispose()


def load_runtime_release(config: Path, release_manifest: Path):
    loaded = load_config(config)
    manifest = load_release_manifest(release_manifest)
    validate_manifest_against_config(
        manifest,
        loaded,
        require_configuration_hash=True,
    )
    validate_manifest_code_version(manifest)
    return loaded, manifest


def load_read_only_release_identity(config: Path, release_manifest: Path):
    """Validate an external fact producer's config identity without importing its code.

    A dashboard may read an archive produced by an older frozen commit. Requiring that
    producer's code version to equal the dashboard process would be false; config and
    manifest integrity still remain mandatory.
    """

    loaded = load_config(config, historical_read_only=True)
    manifest = load_release_manifest(release_manifest)
    validate_manifest_against_config(
        manifest,
        loaded,
        require_configuration_hash=True,
    )
    return loaded, manifest


def default_web_dist() -> Path | None:
    """Locate repository web assets independently from the process working directory."""

    candidates = (
        Path.cwd() / "web" / "dist",
        Path(__file__).resolve().parents[4] / "web" / "dist",
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    return None


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
