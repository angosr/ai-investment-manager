from __future__ import annotations

import ast
import os
import subprocess
import sys
import tomllib
from pathlib import Path

from sqlalchemy import UniqueConstraint
from typer.main import get_command

from investment_manager.entrypoints.cli import app
from investment_manager.entrypoints.research_cli import app as research_app
from investment_manager.kernel.identity import content_hash
from investment_manager.schema import compose_metadata

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "src" / "investment_manager"

CLI_CONTRACT = {
    "assessment-worker": "config,database_url,release_manifest",
    "binance-testnet-audit": "config",
    "binance-testnet-order-test": "symbol,config",
    "challenger-audit": "config,release_manifest,project_root",
    "codex-isolation-audit": "config,release_manifest,project_root,audit_catalog",
    "dashboard-service": (
        "config,database_url,release_manifest,host,port,web_dist"
    ),
    "fetch-binance-carry-history": (
        "config,spot_dataset_id,funding_dataset_id,spot_catalog,funding_catalog,carry_catalog"
    ),
    "fetch-economic-series": "config,series,catalog",
    "fetch-binance-funding-history": "config,symbol,start,end,catalog",
    "fetch-binance-history": "config,symbol,start,end,interval,catalog",
    "fetch-binance-usdm-history": "config,symbol,start,end,interval,catalog",
    "freeze-executable-quotes": (
        "config,database_url,instrument_key,start,end,sampling_interval_seconds,catalog"
    ),
    "freeze-event-history": "database_url,start,end,catalog",
    "information-collector": "config,database_url,release_manifest",
    "invalidate-evaluation-plan": "database_url,plan_id,reason_code,evidence_id",
    "market-stream": "config,database_url,release_manifest",
    "outcome-evaluation-service": "config,database_url,release_manifest",
    "operate-release": (
        "project_root,config,release_manifest,database_url,runtime_directory,command_path,"
        "readiness_timeout_seconds,dashboard_host,dashboard_port"
    ),
    "record-reference-rejection": (
        "config,plan,information_cutoff,project_root,economic_catalog,product_catalog,"
        "funding_catalog,quote_catalog,result_catalog"
    ),
    "phase-a-audit": "config,project_root",
    "set-trigger-heartbeat": (
        "symbol,heartbeat_minutes,config,database_url,release_manifest"
    ),
    "shadow-audit": "config,release_manifest,project_root",
    "submit-context-assessment": "input_path,config,deadline_minutes",
    "trigger-now": "symbol,request_id,reason,config,database_url,evidence_id,release_manifest",
    "trigger-service": "config,database_url,release_manifest",
    "validate-config": "config",
}

RESEARCH_COMMAND_NAMES = {
    "fetch-binance-carry-history",
    "fetch-binance-funding-history",
    "fetch-binance-history",
    "fetch-binance-usdm-history",
    "fetch-economic-series",
    "freeze-event-history",
    "freeze-executable-quotes",
    "record-reference-rejection",
}


def _schema_contract() -> tuple[dict[str, object], ...]:
    contract = []
    for table_name, table in sorted(compose_metadata().tables.items()):
        constraints = tuple(
            sorted(
                (
                    (
                        constraint.__class__.__name__,
                        constraint.name,
                        tuple(column.name for column in constraint.columns),
                    )
                    for constraint in table.constraints
                    if isinstance(constraint, UniqueConstraint)
                ),
                key=lambda item: (item[0], item[1] or "", item[2]),
            )
        )
        contract.append(
            {
                "table": table_name,
                "columns": tuple(
                    (
                        column.name,
                        str(column.type),
                        column.nullable,
                        column.primary_key,
                    )
                    for column in table.columns
                ),
                "foreign_keys": tuple(
                    sorted(
                        (foreign_key.parent.name, foreign_key.target_fullname)
                        for foreign_key in table.foreign_keys
                    )
                ),
                "indexes": tuple(
                    sorted(
                        (
                            index.name,
                            index.unique,
                            tuple(column.name for column in index.columns),
                        )
                        for index in table.indexes
                    )
                ),
                "unique_constraints": constraints,
            }
        )
    return tuple(contract)


def _internal_import_graph() -> dict[str, set[str]]:
    paths: dict[str, Path] = {}
    for path in PACKAGE_ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT / "src").with_suffix("")
        parts = list(relative.parts)
        if parts[-1] == "__init__":
            parts.pop()
        paths[".".join(parts)] = path

    def resolve(name: str) -> str | None:
        parts = name.split(".")
        while parts:
            candidate = ".".join(parts)
            if candidate in paths:
                return candidate
            parts.pop()
        return None

    graph: dict[str, set[str]] = {}
    for module, path in paths.items():
        dependencies: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text())):
            names: tuple[str, ...] = ()
            if isinstance(node, ast.ImportFrom) and node.module:
                names = tuple(
                    candidate if candidate in paths else node.module
                    for alias in node.names
                    for candidate in (f"{node.module}.{alias.name}",)
                )
            elif isinstance(node, ast.Import):
                names = tuple(alias.name for alias in node.names)
            for name in names:
                if name.startswith("investment_manager."):
                    dependency = resolve(name)
                    if dependency is not None and dependency != module:
                        dependencies.add(dependency)
        graph[module] = dependencies
    return graph


def test_schema_shape_is_frozen_during_structure_migration() -> None:
    contract = _schema_contract()

    assert len(contract) == 73
    assert content_hash(contract) == (
        "d055699991be6370a393ed4499be0f56fb5c6b4846cd73bdbb774ade23f15d2f"
    )


def test_managed_schema_runtime_does_not_import_or_register_retired_chain() -> None:
    probe = """
import json
import sys

import investment_manager.entrypoints.cli
from investment_manager.schema import compose_metadata

tables = set(compose_metadata().tables)
forbidden_modules = sorted(
    name for name in sys.modules if name.startswith("investment_manager.legacy")
)
retired_tables = sorted(
    tables.intersection(
        {
            "account_snapshots",
            "analysis_cycles",
            "execution_requests",
            "orders",
            "portfolio_protection_states",
            "reconciliation_reports",
            "risk_reservations",
            "trade_intents",
        }
    )
)
if forbidden_modules or retired_tables:
    raise SystemExit(
        json.dumps(
            {"forbidden_modules": forbidden_modules, "retired_tables": retired_tables}
        )
    )
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_cli_subcommand_parameter_contract_is_frozen() -> None:
    production = get_command(app)
    research = get_command(research_app)
    observed_production = {
        name: ",".join(parameter.name for parameter in command.params)
        for name, command in sorted(production.commands.items())
    }
    observed_research = {
        name: ",".join(parameter.name for parameter in command.params)
        for name, command in sorted(research.commands.items())
    }

    assert observed_production == {
        name: contract
        for name, contract in CLI_CONTRACT.items()
        if name not in RESEARCH_COMMAND_NAMES
    }
    assert observed_research == {
        name: contract
        for name, contract in CLI_CONTRACT.items()
        if name in RESEARCH_COMMAND_NAMES
    }


def test_cli_commands_are_owned_by_change_reason() -> None:
    expected = {
        "entrypoints/cli/commands.py": {
            "invalidate-evaluation-plan",
            "validate-config",
            "phase-a-audit",
            "shadow-audit",
            "challenger-audit",
            "codex-isolation-audit",
            "binance-testnet-audit",
            "binance-testnet-order-test",
        },
        "entrypoints/cli/service_commands.py": {
            "assessment-worker",
            "submit-context-assessment",
            "market-stream",
            "trigger-service",
            "trigger-now",
            "set-trigger-heartbeat",
            "outcome-evaluation-service",
            "information-collector",
            "dashboard-service",
        },
        "entrypoints/cli/release_commands.py": {
            "operate-release",
        },
        "entrypoints/cli/research_commands.py": {
            "record-reference-rejection",
            "freeze-executable-quotes",
            "fetch-economic-series",
            "fetch-binance-history",
            "fetch-binance-usdm-history",
            "fetch-binance-funding-history",
            "fetch-binance-carry-history",
            "freeze-event-history",
        },
    }

    for relative, names in expected.items():
        tree = ast.parse((PACKAGE_ROOT / relative).read_text())
        actual = {
            decorator.args[0].value
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            for decorator in node.decorator_list
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "command"
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
            )
        }
        assert actual == names


def test_current_internal_module_graph_has_no_cycles() -> None:
    graph = _internal_import_graph()
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(module: str, trail: tuple[str, ...]) -> None:
        if module in visiting:
            raise AssertionError("内部模块循环依赖: " + " → ".join((*trail, module)))
        if module in visited:
            return
        visiting.add(module)
        for dependency in sorted(graph.get(module, ())):
            if dependency in graph:
                visit(dependency, (*trail, module))
        visiting.remove(module)
        visited.add(module)

    for module in sorted(graph):
        visit(module, ())


def test_managed_entrypoints_cannot_import_offline_or_legacy_modules() -> None:
    graph = _internal_import_graph()
    entrypoints = (
        "investment_manager.entrypoints.cli",
        "investment_manager.entrypoints.dashboard.app",
    )
    forbidden_prefixes = (
        "investment_manager.legacy",
        "investment_manager.research",
    )

    for entrypoint in entrypoints:
        reachable: set[str] = set()
        pending = [entrypoint]
        while pending:
            module = pending.pop()
            if module in reachable:
                continue
            reachable.add(module)
            pending.extend(graph.get(module, ()) - reachable)
        forbidden = {
            module
            for module in reachable
            if module.startswith(forbidden_prefixes)
        }
        assert forbidden == set(), f"{entrypoint} reaches offline modules: {forbidden}"


def test_dense_domains_group_independent_capabilities_without_reexports() -> None:
    root_modules = {
        "market": {
            "features.py",
            "models.py",
            "policy.py",
            "repository.py",
            "runtime.py",
            "tables.py",
        },
        "execution": {
            "models.py",
            "policy.py",
            "tables.py",
        },
        "forecast": {
            "contract_repository.py",
            "contracts.py",
            "models.py",
            "policy.py",
            "repository.py",
            "results.py",
            "settlement.py",
            "tables.py",
        },
        "governance": {"models.py", "policy.py", "repository.py", "tables.py"},
    }
    capabilities = {
        "execution": {"group", "planning", "venue"},
        "forecast": {"codex", "context", "product"},
        "governance": {"audit", "evaluation", "release"},
        "information": {"official"},
        "market": {"perpetual"},
        "state": {"decision"},
    }

    for domain, expected in root_modules.items():
        observed = {
            path.name
            for path in (PACKAGE_ROOT / domain).glob("*.py")
            if path.name != "__init__.py"
        }
        assert observed == expected

    for domain, names in capabilities.items():
        for name in names:
            package = PACKAGE_ROOT / domain / name
            assert package.is_dir()
            init_tree = ast.parse((package / "__init__.py").read_text())
            assert not any(
                isinstance(node, (ast.Import, ast.ImportFrom))
                for node in init_tree.body
            ), package

    codex_package = PACKAGE_ROOT / "forecast" / "codex"
    assert {
        path.name for path in codex_package.glob("*.py")
    } == {
        "__init__.py",
        "bundle.py",
        "capacity.py",
        "isolation.py",
        "output.py",
        "protocol.py",
        "repository.py",
        "router.py",
    }
    protocol_tree = ast.parse((codex_package / "protocol.py").read_text())
    assert "strict_output_schema" not in {
        node.name for node in protocol_tree.body if isinstance(node, ast.FunctionDef)
    }
    assert not (codex_package / "runtime.py").exists()

    assert not (PACKAGE_ROOT / "legacy").exists()
    assert not (PACKAGE_ROOT / "execution" / "legacy_exchange.py").exists()


def test_decision_cycle_is_the_minimal_one_way_cross_domain_layer() -> None:
    package = PACKAGE_ROOT / "decision_cycle"
    assert {
        path.name for path in package.glob("*.py")
    } == {"__init__.py", "capital.py", "portfolio.py", "service.py", "trigger.py"}

    init_tree = ast.parse((package / "__init__.py").read_text())
    assert not any(
        isinstance(node, (ast.Import, ast.ImportFrom)) for node in init_tree.body
    )

    graph = _internal_import_graph()
    for module, dependencies in graph.items():
        if module.startswith("investment_manager.decision_cycle"):
            assert not {
                dependency
                for dependency in dependencies
                if dependency.startswith("investment_manager.legacy")
            }, module

    business_domains = {
        "execution",
        "forecast",
        "governance",
        "information",
        "market",
        "portfolio",
        "risk",
        "scheduling",
        "state",
    }
    for path in PACKAGE_ROOT.rglob("*.py"):
        relative = path.relative_to(PACKAGE_ROOT)
        tree = ast.parse(path.read_text())
        if relative.parts[0] in business_domains:
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert not node.module.startswith(
                        "investment_manager.decision_cycle"
                    ), path
                if isinstance(node, ast.Import):
                    assert not any(
                        alias.name.startswith("investment_manager.decision_cycle")
                        for alias in node.names
                    ), path
        if relative.parts[0] != "decision_cycle":
            continue
        for node in tree.body:
            assert not (
                isinstance(node, ast.ClassDef)
                and node.name.endswith(("Policy", "Repository"))
            ), path
            assert not (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "Table"
            ), path

    assert not (PACKAGE_ROOT / "portfolio" / "pipeline.py").exists()
    assert not (PACKAGE_ROOT / "legacy" / "trigger_adapter.py").exists()
    assert not (PACKAGE_ROOT / "legacy" / "trigger_runtime.py").exists()


def test_platform_does_not_import_business_modules() -> None:
    graph = _internal_import_graph()

    for module, dependencies in graph.items():
        if module.startswith("investment_manager.platform"):
            assert not {
                dependency
                for dependency in dependencies
                if not dependency.startswith(
                    ("investment_manager.kernel", "investment_manager.platform")
                )
            }

    for filename in (
        "sql_locking.py",
        "sql_time.py",
        "temporal_compat.py",
        "temporal_worker.py",
    ):
        assert not (PACKAGE_ROOT / filename).exists()


def test_research_is_called_by_entrypoints_without_importing_them() -> None:
    graph = _internal_import_graph()

    for module, dependencies in graph.items():
        if module.startswith("investment_manager.research"):
            assert not {
                dependency
                for dependency in dependencies
                if dependency.startswith("investment_manager.entrypoints")
            }, module

    assert not (PACKAGE_ROOT / "research" / "cli.py").exists()
    assert (PACKAGE_ROOT / "entrypoints" / "cli" / "research_commands.py").exists()


def test_kernel_does_not_import_platform_or_business_modules() -> None:
    graph = _internal_import_graph()

    for module, dependencies in graph.items():
        if module.startswith("investment_manager.kernel"):
            assert not {
                dependency
                for dependency in dependencies
                if not dependency.startswith("investment_manager.kernel")
            }


def test_domain_policies_have_one_owner_and_settings_only_composes() -> None:
    owners = {
        "StrictConfig": "kernel/configuration.py",
        "FeaturePolicy": "market/policy.py",
        "MarketDataPolicy": "market/policy.py",
        "DecisionPacketPolicy": "state/policy.py",
        "DecisionStatePolicy": "state/policy.py",
        "PipelinePolicy": "forecast/policy.py",
        "CodexAccount": "forecast/policy.py",
        "CodexAccountRegistry": "forecast/policy.py",
        "CodexRuntimePolicy": "forecast/policy.py",
        "ContextAssessmentPolicy": "forecast/policy.py",
        "ShadowSimulationPolicy": "execution/policy.py",
        "BinanceTestnetPolicy": "execution/policy.py",
        "OutcomeEvaluationPolicy": "governance/policy.py",
        "GovernancePolicy": "governance/policy.py",
        "DeploymentStage": "governance/policy.py",
        "DeploymentPolicy": "governance/policy.py",
        "TriggerPolicy": "scheduling/policy.py",
        "TemporalPolicy": "scheduling/policy.py",
        "InformationPolicy": "information/policy.py",
    }
    definitions: dict[str, list[str]] = {name: [] for name in owners}

    for path in PACKAGE_ROOT.rglob("*.py"):
        relative = path.relative_to(PACKAGE_ROOT).as_posix()
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name in definitions:
                definitions[node.name].append(relative)
        if path.name == "settings.py":
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == (
                "investment_manager.settings"
            ):
                    assert {alias.name for alias in node.names}.issubset(
                        {"AppConfig", "load_config", "load_config_mapping"}
                    ), path

    assert definitions == {name: [owner] for name, owner in owners.items()}
    assert not (PACKAGE_ROOT / "config.py").exists()
    settings_classes = {
        node.name
        for node in ast.parse((PACKAGE_ROOT / "settings.py").read_text()).body
        if isinstance(node, ast.ClassDef)
    }
    assert settings_classes == {"AppConfig"}


def test_evidence_and_scheduling_domains_do_not_depend_on_forecast() -> None:
    graph = _internal_import_graph()

    for module, dependencies in graph.items():
        if module.startswith(
            (
                "investment_manager.information",
                "investment_manager.market",
                "investment_manager.scheduling",
                "investment_manager.state",
            )
        ):
            assert not {
                dependency
                for dependency in dependencies
                if dependency.startswith("investment_manager.forecast")
            }, module


def test_shared_models_are_owned_and_retired_modules_are_absent() -> None:
    owners = {
        "ForecastResultKind": "forecast/results.py",
    }
    definitions: dict[str, list[str]] = {name: [] for name in owners}

    for path in PACKAGE_ROOT.rglob("*.py"):
        relative = path.relative_to(PACKAGE_ROOT).as_posix()
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name in definitions:
                definitions[node.name].append(relative)
        if relative.split("/", 1)[0] not in {
            "forecast",
            "information",
            "market",
            "portfolio",
            "scheduling",
            "state",
        }:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("investment_manager.legacy"), path

    assert definitions == {name: [owner] for name, owner in owners.items()}

    for path in (PACKAGE_ROOT / "risk").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("investment_manager.legacy"), path
            if isinstance(node, ast.Import):
                assert not any(
                    alias.name.startswith("investment_manager.legacy")
                    for alias in node.names
                ), path

    assert not (PACKAGE_ROOT / "legacy").exists()
    assert not (PACKAGE_ROOT / "risk" / "legacy.py").exists()
    for filename in (
        "analyst.py",
        "calibration.py",
        "candidate_evaluation.py",
        "cycle.py",
        "decision.py",
        "domain.py",
        "forecast_evaluation.py",
        "metrics.py",
        "panel.py",
        "shadow.py",
        "strategy.py",
        "temporal_runtime.py",
        "temporal_workflows.py",
        "workflow.py",
    ):
        assert not (PACKAGE_ROOT / filename).exists()


def test_shared_kernel_primitives_are_not_imported_from_domain() -> None:
    kernel_primitives = {
        "FrozenModel",
        "Money",
        "PositiveDecimal",
        "UnitInterval",
        "floor_to_step",
        "optional_utc",
        "require_utc",
    }

    for path in (*PACKAGE_ROOT.rglob("*.py"), *(ROOT / "tests").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module == (
                "investment_manager.legacy.models"
            ):
                assert not kernel_primitives.intersection(
                    alias.name for alias in node.names
                ), path


def test_market_models_are_imported_from_their_domain_owner() -> None:
    market_models = {
        "FeatureSnapshot",
        "InstrumentId",
        "InstrumentProduct",
        "MarketBar",
        "MarketSnapshot",
    }

    for path in (*PACKAGE_ROOT.rglob("*.py"), *(ROOT / "tests").rglob("*.py")):
        if path == PACKAGE_ROOT / "domain.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module == (
                "investment_manager.legacy.models"
            ):
                assert not market_models.intersection(
                    alias.name for alias in node.names
                ), path

    assert not (PACKAGE_ROOT / "market_data.py").exists()
    assert not (PACKAGE_ROOT / "market_data_sql.py").exists()
    assert not (PACKAGE_ROOT / "features.py").exists()


def test_market_tables_have_one_domain_owner_and_no_repository_reexports() -> None:
    owned_tables = {
        "funding_settlements",
        "market_bars",
        "market_quotes",
        "market_trades",
        "perpetual_market_states",
        "perpetual_quotes",
        "tradfi_trading_schedules",
    }
    owners: dict[str, list[Path]] = {name: [] for name in owned_tables}

    for path in PACKAGE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if (
                    isinstance(target, ast.Name)
                    and target.id in owned_tables
                    and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and node.value.func.id == "Table"
                ):
                    owners[target.id].append(path)
        if path == PACKAGE_ROOT / "market" / "repository.py":
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "investment_manager.market.repository"
            ):
                assert not owned_tables.intersection(
                    alias.name for alias in node.names
                ), path

    expected = PACKAGE_ROOT / "market" / "tables.py"
    assert owners == {name: [expected] for name in owned_tables}


def test_information_facts_are_imported_from_their_domain_owner() -> None:
    moved_models = {"IntelligenceEvent", "SourceObservation", "SourceTier"}
    old_modules = {"investment_manager.portfolio.models"}

    for path in (*PACKAGE_ROOT.rglob("*.py"), *(ROOT / "tests").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module in old_modules:
                assert not moved_models.intersection(
                    alias.name for alias in node.names
                ), path

    for filename in (
        "ingestion.py",
        "official_information.py",
        "official_information_sql.py",
        "source_payload.py",
        "source_payload_sql.py",
    ):
        assert not (PACKAGE_ROOT / filename).exists()


def test_information_tables_have_one_domain_owner() -> None:
    owned_tables = {
        "market_calendar_event_revisions",
        "normalized_events",
        "raw_source_payloads",
        "source_observations",
    }
    owners: dict[str, list[Path]] = {name: [] for name in owned_tables}

    for path in PACKAGE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if (
                isinstance(target, ast.Name)
                and target.id in owned_tables
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "Table"
            ):
                owners[target.id].append(path)

    expected = PACKAGE_ROOT / "information" / "tables.py"
    assert owners == {name: [expected] for name in owned_tables}


def test_state_models_and_tables_have_one_domain_owner() -> None:
    moved_models = {
        "CanonicalFactRevision",
        "DeltaCategory",
        "FactRevisionStatus",
        "MaterialDelta",
        "Materiality",
        "StateSnapshot",
    }
    owned_tables = {
        "canonical_fact_revision_sources",
        "canonical_fact_revisions",
        "decision_packets",
        "material_deltas",
        "state_evidence_snapshots",
        "state_snapshots",
    }
    owners: dict[str, list[Path]] = {name: [] for name in owned_tables}

    for path in (*PACKAGE_ROOT.rglob("*.py"), *(ROOT / "tests").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "investment_manager.portfolio.models"
            ):
                assert not moved_models.intersection(
                    alias.name for alias in node.names
                ), path
        if not path.is_relative_to(PACKAGE_ROOT):
            continue
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if (
                isinstance(target, ast.Name)
                and target.id in owned_tables
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "Table"
            ):
                owners[target.id].append(path)

    expected = PACKAGE_ROOT / "state" / "tables.py"
    assert owners == {name: [expected] for name in owned_tables}
    for filename in (
        "decision_packet.py",
        "decision_packet_sql.py",
        "fact_pipeline.py",
        "fact_state_sql.py",
        "official_fact_pipeline.py",
        "state_evidence_sql.py",
        "state_projection.py",
    ):
        assert not (PACKAGE_ROOT / filename).exists()


def test_scheduling_tables_and_entry_modules_have_one_owner() -> None:
    owned_tables = {
        "analysis_call_admissions",
        "analysis_scheduled_wakeups",
        "analysis_trigger_batches",
        "analysis_trigger_events",
        "analysis_trigger_plans",
        "trigger_outbox",
    }
    owners: dict[str, list[Path]] = {name: [] for name in owned_tables}

    for path in PACKAGE_ROOT.rglob("*.py"):
        for node in ast.parse(path.read_text()).body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if (
                isinstance(target, ast.Name)
                and target.id in owned_tables
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "Table"
            ):
                owners[target.id].append(path)

    expected = PACKAGE_ROOT / "scheduling" / "tables.py"
    assert owners == {name: [expected] for name in owned_tables}
    for filename in (
        "trigger.py",
        "trigger_runtime.py",
        "trigger_sql.py",
        "trigger_workflows.py",
    ):
        assert not (PACKAGE_ROOT / filename).exists()


def test_new_forecast_chain_has_one_domain_owner() -> None:
    moved_models = {
        "BaseForecast",
        "CalibratedForecast",
        "ContextAssessment",
        "ExposureDirection",
        "ForecastLeg",
        "ForecastTarget",
    }
    owned_tables = {
        "codex_account_capacity",
        "codex_account_leases",
        "codex_runs",
        "context_assessments",
        "context_forecast_posterior_assignments",
        "context_forecast_stability_assignments",
        "context_forecast_stability_results",
        "forecast_outcomes",
        "forecasts",
        "historical_assessment_view_outcomes",
        "product_payoff_outcomes",
        "product_payoff_projections",
    }
    owners: dict[str, list[Path]] = {name: [] for name in owned_tables}

    for path in (*PACKAGE_ROOT.rglob("*.py"), *(ROOT / "tests").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "investment_manager.portfolio.models"
            ):
                assert not moved_models.intersection(
                    alias.name for alias in node.names
                ), path
        if not path.is_relative_to(PACKAGE_ROOT):
            continue
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if (
                isinstance(target, ast.Name)
                and target.id in owned_tables
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "Table"
            ):
                owners[target.id].append(path)

    expected = PACKAGE_ROOT / "forecast" / "tables.py"
    assert owners == {name: [expected] for name in owned_tables}
    codex_repository_classes = {
        node.name
        for node in ast.parse(
            (
                PACKAGE_ROOT / "forecast" / "codex" / "repository.py"
            ).read_text()
        ).body
        if isinstance(node, ast.ClassDef)
    }
    assert {"SqlAccountLeaseStore", "SqlCodexAuditStore"}.issubset(
        codex_repository_classes
    )
    for filename in (
        "assess_execution.py",
        "assessment_calibration.py",
        "assessment_forecast.py",
        "assessment_outcome.py",
        "context_analyst.py",
        "context_assessment_sql.py",
    ):
        assert not (PACKAGE_ROOT / filename).exists()


def test_portfolio_target_chain_has_one_domain_owner() -> None:
    for filename in (
        "asset_management.py",
        "portfolio_decision.py",
        "portfolio_pipeline.py",
    ):
        assert not (PACKAGE_ROOT / filename).exists()

    portfolio_models = ast.parse(
        (PACKAGE_ROOT / "portfolio" / "models.py").read_text()
    )
    portfolio_classes = {
        node.name for node in portfolio_models.body if isinstance(node, ast.ClassDef)
    }
    assert {"PortfolioAccountSnapshot", "SleevePosition", "SleeveTarget"}.issubset(
        portfolio_classes
    )
    assert "AssetTarget" not in portfolio_classes

    risk_portfolio = ast.parse((PACKAGE_ROOT / "risk" / "portfolio.py").read_text())
    planner = ast.parse(
        (PACKAGE_ROOT / "execution" / "planning" / "planner.py").read_text()
    )
    decision_pipeline = ast.parse(
        (PACKAGE_ROOT / "decision_cycle" / "portfolio.py").read_text()
    )
    for tree in (risk_portfolio, planner, decision_pipeline):
        imported_account_owners = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and any(alias.name == "PortfolioAccountSnapshot" for alias in node.names)
        }
        assert imported_account_owners == {"investment_manager.portfolio.models"}
    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("investment_manager.execution")
        for node in ast.walk(risk_portfolio)
    )

    world_service = ast.parse(
        (PACKAGE_ROOT / "state" / "decision" / "service.py").read_text()
    )
    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("investment_manager.execution")
        for node in ast.walk(world_service)
    )

    owned_tables = {
        "portfolio_account_snapshots",
        "portfolio_target_forecasts",
        "portfolio_targets",
    }
    owners = {name: [] for name in owned_tables}
    for path in PACKAGE_ROOT.rglob("*.py"):
        for node in ast.parse(path.read_text()).body:
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in owned_tables
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "Table"
            ):
                owners[node.targets[0].id].append(path)
    expected = PACKAGE_ROOT / "portfolio" / "tables.py"
    assert owners == {name: [expected] for name in owned_tables}


def test_risk_models_and_modules_have_one_domain_owner() -> None:
    for filename in (
        "portfolio_protection.py",
        "portfolio_risk.py",
        "risk.py",
        "risk_budget.py",
    ):
        assert not (PACKAGE_ROOT / filename).exists()

    owners = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        for node in ast.parse(path.read_text()).body:
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "portfolio_risk_decisions"
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "Table"
            ):
                owners.append(path)
    assert owners == [PACKAGE_ROOT / "risk" / "tables.py"]


def test_active_execution_tables_have_one_owner() -> None:
    active_tables = {
        "execution_groups",
        "mock_product_orders",
        "product_order_observations",
        "trade_plans",
    }
    owned_tables = active_tables
    owners: dict[str, list[Path]] = {name: [] for name in owned_tables}

    for path in PACKAGE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if (
                isinstance(target, ast.Name)
                and target.id in owned_tables
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "Table"
            ):
                owners[target.id].append(path)

    active_owner = PACKAGE_ROOT / "execution" / "tables.py"
    assert owners == {name: [active_owner] for name in active_tables}
    for filename in (
        "binance_testnet.py",
        "execution.py",
        "execution_contract.py",
        "exit_policy.py",
        "ledger.py",
        "lifecycle.py",
        "lifecycle_runtime.py",
        "lifecycle_workflows.py",
        "mock_exchange_sql.py",
        "reconciliation.py",
        "reconciliation_runtime.py",
        "reconciliation_sql.py",
        "reconciliation_workflows.py",
        "trade_planner.py",
    ):
        assert not (PACKAGE_ROOT / filename).exists()


def test_governance_tables_and_entry_modules_have_one_owner() -> None:
    active_tables = {
        "blind_evaluation_claims",
        "evaluation_plans",
        "failed_experiment_records",
        "release_manifests",
        "world_model_ablation_assignments",
        "world_model_ablation_results",
    }
    owned_tables = active_tables
    owners: dict[str, list[Path]] = {name: [] for name in owned_tables}

    for path in PACKAGE_ROOT.rglob("*.py"):
        for node in ast.parse(path.read_text()).body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if (
                isinstance(target, ast.Name)
                and target.id in owned_tables
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "Table"
            ):
                owners[target.id].append(path)

    active_owner = PACKAGE_ROOT / "governance" / "tables.py"
    assert owners == {name: [active_owner] for name in active_tables}
    assert not (PACKAGE_ROOT / "persistence.py").exists()
    governance_classes = {
        node.name
        for node in ast.parse(
            (PACKAGE_ROOT / "governance" / "repository.py").read_text()
        ).body
        if isinstance(node, ast.ClassDef)
    }
    assert governance_classes == {"SqlGovernanceRepository"}
    for filename in (
        "acceptance.py",
        "deployment.py",
        "evaluation.py",
        "governance.py",
        "governance_agent.py",
        "governance_context.py",
        "governance_runtime.py",
        "governance_workflows.py",
        "isolation_audit.py",
        "outcome_evaluation_runtime.py",
        "outcome_evaluation_sql.py",
        "outcome_evaluation_workflows.py",
        "release_runtime.py",
        "release_workflows.py",
        "version_evaluation_runtime.py",
        "version_evaluation_workflows.py",
    ):
        assert not (PACKAGE_ROOT / filename).exists()


def test_old_package_and_console_entry_are_removed() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]

    assert not (ROOT / "src" / "quant_core").exists()
    assert project["name"] == "investment-manager"
    assert project["scripts"] == {
        "investment-manager": "investment_manager.entrypoints.cli:app",
        "investment-manager-research": (
            "investment_manager.entrypoints.research_cli:app"
        ),
    }
    assert not (PACKAGE_ROOT / "cli.py").exists()
    assert not (PACKAGE_ROOT / "dashboard").exists()


def test_current_runtime_identity_uses_product_name() -> None:
    runtime_roots = (
        PACKAGE_ROOT,
        ROOT / "config",
        ROOT / "migrations",
    )
    runtime_files = [
        path
        for root in runtime_roots
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    ]
    runtime_files.extend((ROOT / ".env.example", ROOT / "docker-compose.yml"))

    forbidden = ("quant_core", "quant-core", "QUANT_CORE")
    violations = {
        str(path.relative_to(ROOT)): token
        for path in runtime_files
        for token in forbidden
        if token in str(path.relative_to(ROOT))
        or token in path.read_text(encoding="utf-8")
    }

    assert violations == {}


def test_capital_decision_language_is_venue_neutral() -> None:
    paths = (
        PACKAGE_ROOT / "forecast" / "contracts.py",
        PACKAGE_ROOT / "forecast" / "context" / "contract.py",
        PACKAGE_ROOT / "forecast" / "context" / "estimate.py",
        PACKAGE_ROOT / "forecast" / "context" / "producer.py",
        PACKAGE_ROOT / "portfolio" / "decision.py",
        PACKAGE_ROOT / "portfolio" / "models.py",
        PACKAGE_ROOT / "portfolio" / "policy.py",
        PACKAGE_ROOT / "state" / "decision" / "packet.py",
        PACKAGE_ROOT / "entrypoints" / "dashboard" / "capital.py",
        ROOT / "web" / "src" / "components" / "CapitalActions.tsx",
        ROOT / "web" / "src" / "components" / "CapitalEquityHero.tsx",
    )
    forbidden = (
        "mock_authorization",
        "mock_candidate",
        "MockCandidate",
        "MOCK",
        "MOCK_HYPOTHESIS",
        "shadow",
        "testnet",
        "simulation",
        "模拟盘",
        "模拟交易",
        "模拟订单",
        "模拟执行",
        "模拟账户",
        "仿真",
    )

    violations = {
        str(path.relative_to(ROOT)): token
        for path in paths
        for token in forbidden
        if token in path.read_text(encoding="utf-8")
    }

    assert violations == {}


def test_capital_application_does_not_select_deployment_adapter() -> None:
    source = (PACKAGE_ROOT / "decision_cycle" / "capital.py").read_text(
        encoding="utf-8"
    )
    module = ast.parse(source)
    service = next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "CapitalCycleService"
    )
    initializer = next(
        node
        for node in service.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    service_source = ast.get_source_segment(source, service)
    assert service_source is not None

    assert "DeploymentStage" not in source
    assert "SqlMockProductVenue" not in source
    assert "config.shadow" not in source
    assert "config.pipeline" not in source
    assert "config" not in {
        item.arg for item in (*initializer.args.args, *initializer.args.kwonlyargs)
    }
    assert "capital_policy" in {
        item.arg for item in (*initializer.args.args, *initializer.args.kwonlyargs)
    }
    assert "capital_behavior_id" in {
        item.arg for item in (*initializer.args.args, *initializer.args.kwonlyargs)
    }
    assert "pipeline_version" not in service_source
    assert all(
        token not in service_source
        for token in ("deployment", "shadow", "testnet", "simulation", "模拟", "仿真")
    )


def test_dashboard_has_one_current_capital_and_world_model_read_path() -> None:
    dashboard_root = PACKAGE_ROOT / "entrypoints" / "dashboard"
    backend = "\n".join(
        path.read_text(encoding="utf-8") for path in dashboard_root.glob("*.py")
    )

    assert "investment_manager.legacy" not in backend
    for route in ("/api/cycles", "/api/assessment/cycles", "/api/equity"):
        assert route not in backend
    for retired in (
        "CycleRow.tsx",
        "CycleRail.tsx",
        "CycleRail.module.css",
        "EquityHero.tsx",
    ):
        assert not (ROOT / "web" / "src" / "components" / retired).exists()

    def methods(path: Path, class_name: str) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owner = next(
            item
            for item in tree.body
            if isinstance(item, ast.ClassDef) and item.name == class_name
        )
        return {
            item.name
            for item in owner.body
            if isinstance(item, ast.FunctionDef)
        }

    evaluation_methods = {
        "capital_choice_evidence",
        "trading_cost_evidence",
    }
    assert methods(dashboard_root / "capital.py", "CapitalDashboardReader").isdisjoint(
        evaluation_methods
    )
    assert evaluation_methods.issubset(
        methods(dashboard_root / "evaluation.py", "EvaluationDashboardReader")
    )
    assert not (dashboard_root / "producer_capital.py").exists()


def test_package_root_contains_only_composition_entries() -> None:
    assert {
        path.name
        for path in PACKAGE_ROOT.iterdir()
        if path.is_file()
    } == {"__init__.py", "schema.py", "settings.py"}


def test_top_level_packages_are_explicit_architectural_boundaries() -> None:
    expected = {
        "decision_cycle",
        "entrypoints",
        "execution",
        "forecast",
        "governance",
        "information",
        "kernel",
        "market",
        "platform",
        "portfolio",
        "research",
        "risk",
        "scheduling",
        "state",
    }
    observed = {
        path.name
        for path in PACKAGE_ROOT.iterdir()
        if path.is_dir() and not path.name.startswith("__")
    }

    assert observed == expected
