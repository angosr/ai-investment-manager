from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, insert, inspect, select, text

from investment_manager.forecast.tables import forecast_contracts, forecast_decision_slots
from investment_manager.legacy.tables import analysis_cycles
from investment_manager.market.tables import market_quotes
from investment_manager.platform.database import require_current_schema
from investment_manager.risk.budget import portfolio_risk_budgets, risk_reservations
from investment_manager.risk.protection import portfolio_protection_states
from investment_manager.schema import compose_metadata, compose_offline_metadata


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


def test_managed_schema_excludes_retired_tables_but_offline_schema_can_read_them() -> None:
    managed = compose_metadata()
    offline = compose_offline_metadata()

    assert set(managed.tables) < set(offline.tables)
    assert market_quotes.name in managed.tables
    assert {
        analysis_cycles.name,
        portfolio_protection_states.name,
        portfolio_risk_budgets.name,
        risk_reservations.name,
    } <= set(offline.tables)
    assert {
        analysis_cycles.name,
        portfolio_protection_states.name,
        portfolio_risk_budgets.name,
        risk_reservations.name,
    }.isdisjoint(managed.tables)


def test_unified_store_migration_archives_retired_role_facts(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'unified-store.db'}"
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "d7a9c2e4f601")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO fact_store_identity "
                "(singleton_key, store_id, role, claimed_at) "
                "VALUES ('PRIMARY', 'old-store', 'CONTEXT', '2026-08-23 12:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO fact_cohort_quarantines "
                "(quarantine_id, store_id, manifest_id, pipeline_id, "
                "analysis_behavior_hash, reason_code, quarantined_at, payload) "
                "VALUES ('old-quarantine', 'old-store', 'old-manifest', 'old-pipeline', "
                "NULL, 'WRONG_FACT_STORE', '2026-08-23 12:01:00', '{}')"
            )
        )

    command.upgrade(config, "head")

    tables = set(inspect(engine).get_table_names())
    assert "fact_store_identity" not in tables
    assert "fact_cohort_quarantines" not in tables
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT role FROM historical_fact_store_identities")
        ).scalar_one() == "CONTEXT"
        assert connection.execute(
            text("SELECT quarantine_id FROM historical_fact_cohort_quarantines")
        ).scalar_one() == "old-quarantine"


def test_world_model_migration_keeps_only_canonical_v2_in_active_ledger(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'world-model-v2.db'}"
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "e2f6a8c4d901")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        for suffix in ("old", "current", "transitional"):
            connection.execute(
                text(
                    "INSERT INTO decision_packets "
                    "(packet_id, analysis_scope, as_of, policy_version, content_hash, payload) "
                    "VALUES (:packet_id, 'scope', '2026-08-23 12:00:00', 'policy', "
                    ":content_hash, '{}')"
                ),
                {"packet_id": f"packet-{suffix}", "content_hash": suffix[0] * 64},
            )
        connection.execute(
            text(
                "INSERT INTO context_assessments "
                "(assessment_id, packet_id, analysis_scope, available_at, "
                "analysis_behavior_hash, payload) VALUES "
                "('assessment-old', 'packet-old', 'scope', '2026-08-23 12:01:00', "
                ":old_hash, :old_payload), "
                "('assessment-current', 'packet-current', 'scope', "
                "'2026-08-23 12:02:00', :current_hash, :current_payload), "
                "('assessment-transitional', 'packet-transitional', 'scope', "
                "'2026-08-23 12:03:00', :transitional_hash, :transitional_payload)"
            ),
            {
                "old_hash": "a" * 64,
                "current_hash": "b" * 64,
                "transitional_hash": "c" * 64,
                "old_payload": '{"schema_version":"world-model-assessment-v1"}',
                "current_payload": '{"schema_version":"world-model-assessment-v2"}',
                "transitional_payload": (
                    '{"schema_version":"world-model-assessment-v2",'
                    '"views":[],"market_mechanism":"retired"}'
                ),
            },
        )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        active = connection.execute(
            text("SELECT assessment_id, payload FROM context_assessments")
        ).mappings().all()
        archived = set(
            connection.execute(
                text("SELECT assessment_id FROM historical_context_assessments")
            ).scalars()
        )

    assert {row["assessment_id"] for row in active} == {
        "assessment-current",
        "assessment-transitional",
    }
    transitional = next(
        row["payload"]
        for row in active
        if row["assessment_id"] == "assessment-transitional"
    )
    assert "views" not in transitional
    assert "market_mechanism" not in transitional
    assert archived == {"assessment-old", "assessment-transitional"}


def test_forecast_cause_migration_removes_only_retired_empty_fields(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'forecast-cause.db'}"
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "r6a9d3e2f842")
    engine = create_engine(database_url)
    contract_payload = {
        "contract_id": "contract",
        "contract_version": "version",
        "outcome_family_id": "family",
        "target": {
            "target_id": "target",
            "legs": [],
            "quantity_mode": "INDEPENDENT_NOTIONAL",
        },
        "horizon_minutes": 240,
    }
    retired_payload = {
        "slot_id": "retired-empty-fields",
        "contract_id": "contract",
        "slot_as_of": "2026-08-25T12:00:00Z",
        "information_cutoff_at": "2026-08-25T12:00:00Z",
        "completion_deadline_at": "2026-08-25T12:25:00Z",
        "evaluation_at": "2026-08-25T16:00:00Z",
        "cause": {
            "origin": "CADENCE",
            "policy_version": "fixed-cadence-v1",
            "trigger_refs": [],
            "additional_origins": [],
            "cadence_anchor_at": None,
        },
    }
    canonical_payload = {
        **retired_payload,
        "slot_id": "already-canonical",
        "cause": {
            "origin": "CADENCE",
            "policy_version": "fixed-cadence-v1",
            "trigger_refs": [],
        },
    }
    slot_as_of = datetime(2026, 8, 25, 12, tzinfo=UTC)
    with engine.begin() as connection:
        connection.execute(
            insert(forecast_contracts).values(
                contract_id="contract",
                contract_version="version",
                outcome_family_id="family",
                target_id="target",
                horizon_minutes=240,
                payload=contract_payload,
            )
        )
        for payload in (retired_payload, canonical_payload):
            connection.execute(
                insert(forecast_decision_slots).values(
                    slot_id=payload["slot_id"],
                    contract_id="contract",
                    slot_as_of=slot_as_of,
                    information_cutoff_at=slot_as_of,
                    completion_deadline_at=slot_as_of + timedelta(minutes=25),
                    evaluation_at=slot_as_of + timedelta(hours=4),
                    payload=payload,
                )
            )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        payloads = dict(
            connection.execute(
                select(forecast_decision_slots.c.slot_id, forecast_decision_slots.c.payload)
            ).all()
        )
    assert payloads["retired-empty-fields"]["cause"] == canonical_payload["cause"]
    assert payloads["already-canonical"] == canonical_payload
