from __future__ import annotations

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select

from quant_core.market_data_sql import market_metadata
from quant_core.persistence import metadata, portfolio_risk_budgets


def test_alembic_initial_migration_matches_metadata_and_seeds_risk_budget(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'migration.db'}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)

    command.upgrade(config, "head")
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    tables = set(inspect(engine).get_table_names())
    assert set(metadata.tables).issubset(tables)
    assert set(market_metadata.tables).issubset(tables)
    assert "alembic_version" in tables
    assert "analysis_workflow_runs" not in tables
    with engine.connect() as connection:
        budget = connection.execute(select(portfolio_risk_budgets)).mappings().one()
    assert budget["portfolio_id"] == "primary"
    assert budget["reserved_amount"] == 0

    command.check(config)
