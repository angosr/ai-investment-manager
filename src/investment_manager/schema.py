from __future__ import annotations

from importlib import import_module

from sqlalchemy import MetaData
from sqlalchemy.engine import Engine

from investment_manager.platform.database import metadata


def compose_metadata() -> MetaData:
    """Load every table owner into the one physical-database registry."""

    for owner in (
        "investment_manager.market.repository",
        "investment_manager.portfolio_protection",
        "investment_manager.risk_budget",
        "investment_manager.persistence",
    ):
        import_module(owner)

    return metadata


def create_schema(engine: Engine) -> None:
    """Create the complete test schema and bootstrap domain-owned singleton rows."""

    from investment_manager.portfolio_protection import bootstrap_portfolio_protection
    from investment_manager.risk_budget import bootstrap_risk_budget

    compose_metadata().create_all(engine)
    bootstrap_risk_budget(engine)
    bootstrap_portfolio_protection(engine)
