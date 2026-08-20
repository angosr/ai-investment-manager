from __future__ import annotations

from importlib import import_module

from sqlalchemy import MetaData

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
