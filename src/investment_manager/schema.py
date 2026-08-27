from __future__ import annotations

from importlib import import_module
from types import ModuleType

from sqlalchemy import MetaData, Table
from sqlalchemy.engine import Engine

from investment_manager.platform.database import metadata

_MANAGED_TABLE_OWNERS = (
    "investment_manager.platform.database",
    "investment_manager.information.tables",
    "investment_manager.market.tables",
    "investment_manager.scheduling.tables",
    "investment_manager.state.tables",
    "investment_manager.forecast.tables",
    "investment_manager.portfolio.tables",
    "investment_manager.risk.tables",
    "investment_manager.execution.tables",
    "investment_manager.governance.tables",
)

def compose_metadata() -> MetaData:
    """Compose only tables owned by the active managed runtime."""

    return _compose(_MANAGED_TABLE_OWNERS)

def _compose(owner_names: tuple[str, ...]) -> MetaData:
    owners = tuple(import_module(owner) for owner in owner_names)
    registry = MetaData()
    tables: dict[str, Table] = {}
    for owner in owners:
        for table in _owned_tables(owner):
            existing = tables.get(table.name)
            if existing is not None and existing is not table:
                raise RuntimeError(f"数据库表 {table.name} 存在多个领域所有者")
            tables[table.name] = table
    for table in sorted(tables.values(), key=lambda item: item.name):
        table.to_metadata(registry)
    return registry


def _owned_tables(owner: ModuleType) -> tuple[Table, ...]:
    return tuple(
        value
        for value in vars(owner).values()
        if isinstance(value, Table) and value.metadata is metadata
    )


def create_schema(engine: Engine) -> None:
    """Create the active managed-runtime schema for focused tests and assembly."""

    compose_metadata().create_all(engine)
