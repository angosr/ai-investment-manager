from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text

from investment_manager.legacy.repository import analysis_cycles
from investment_manager.market.tables import market_quotes
from investment_manager.platform.database import require_current_schema
from investment_manager.risk.budget import portfolio_risk_budgets, risk_reservations
from investment_manager.risk.protection import portfolio_protection_states
from investment_manager.schema import compose_metadata


def test_alembic_initial_migration_matches_metadata_and_seeds_risk_budget(
    tmp_path, monkeypatch
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'migration.db'}"
    wrong_database = tmp_path / "inherited-runtime.db"
    monkeypatch.setenv(
        "INVESTMENT_MANAGER_DATABASE_URL",
        f"sqlite+pysqlite:///{wrong_database}",
    )
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["database_url"] = database_url

    command.upgrade(config, "head")
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    tables = set(inspect(engine).get_table_names())
    assert set(compose_metadata().tables) <= tables
    assert "alembic_version" in tables
    assert "analysis_workflow_runs" not in tables
    with engine.connect() as connection:
        budget = connection.execute(select(portfolio_risk_budgets)).mappings().one()
    assert budget["portfolio_id"] == "primary"
    assert budget["reserved_amount"] == 0

    command.check(config)
    assert not wrong_database.exists()

    require_current_schema(engine)
    with engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num = 'stale-version'"))
    with pytest.raises(RuntimeError, match="数据库 Schema 版本不匹配"):
        require_current_schema(engine)


def test_all_physical_database_tables_share_one_metadata_registry() -> None:
    registry = compose_metadata()

    assert analysis_cycles.metadata is registry
    assert market_quotes.metadata is registry
    assert portfolio_protection_states.metadata is registry
    assert portfolio_risk_budgets.metadata is registry
    assert risk_reservations.metadata is registry
