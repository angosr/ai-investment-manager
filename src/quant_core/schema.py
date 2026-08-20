from __future__ import annotations

from importlib import import_module

from sqlalchemy import MetaData
from sqlalchemy.engine import Engine

from quant_core.platform.database import metadata


def compose_metadata() -> MetaData:
    """Load every table owner into the one physical-database registry."""

    for owner in (
        "quant_core.market_data_sql",
        "quant_core.portfolio_protection",
        "quant_core.risk_budget",
        "quant_core.persistence",
    ):
        import_module(owner)

    return metadata


def create_schema(engine: Engine) -> None:
    """Create the complete test schema and bootstrap domain-owned singleton rows."""

    from quant_core.portfolio_protection import bootstrap_portfolio_protection
    from quant_core.risk_budget import bootstrap_risk_budget

    compose_metadata().create_all(engine)
    bootstrap_risk_budget(engine)
    bootstrap_portfolio_protection(engine)
