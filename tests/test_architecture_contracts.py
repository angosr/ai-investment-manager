from __future__ import annotations

import ast
import tomllib
from pathlib import Path

from sqlalchemy import UniqueConstraint
from typer.main import get_command

from investment_manager.cli import app
from investment_manager.kernel.identity import content_hash
from investment_manager.schema import compose_metadata

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "src" / "investment_manager"

CLI_CONTRACT = {
    "binance-testnet-audit": "config",
    "binance-testnet-order-test": "symbol,config",
    "blind-evaluate": (
        "config,database_url,source_evaluation_id,catalog,event_catalog,funding_catalog,"
        "evaluation_catalog,blind_evaluation_catalog,include_trades"
    ),
    "build-edge-calibration": (
        "config,database_url,producer_id,producer_version,symbol,side,horizon_minutes,"
        "training_start,training_end,published_at,valid_from,valid_until,evaluation_version,"
        "source_calibration_ref,source_execution_policy_version,source_frequency_policy_version"
    ),
    "carry-blind-evaluate": (
        "database_url,source_evaluation_id,carry_catalog,spot_catalog,evaluation_catalog,"
        "blind_catalog"
    ),
    "carry-walk-forward": (
        "database_url,carry_dataset_id,plan_id,carry_catalog,spot_catalog,evaluation_catalog,"
        "register_only"
    ),
    "challenger-audit": "config,release_manifest,project_root",
    "codex-isolation-audit": "config,release_manifest,project_root,audit_catalog",
    "dashboard-service": "config,database_url,release_manifest,host,port,web_dist",
    "evaluate-ai-forecast-plan": "database_url,plan_id,published_at,evaluation_catalog",
    "evaluate-ai-forecasts": (
        "config,database_url,window_start,window_end,published_at,pipeline_version,"
        "analysis_behavior_hash,minimum_non_overlapping_samples"
    ),
    "evaluate-carry-forward-plan": (
        "database_url,plan_id,carry_dataset_id,carry_catalog,spot_catalog,funding_catalog,"
        "evaluation_catalog"
    ),
    "fetch-binance-carry-history": (
        "config,spot_dataset_id,funding_dataset_id,spot_catalog,funding_catalog,carry_catalog"
    ),
    "fetch-binance-funding-history": "config,symbol,start,end,catalog",
    "fetch-binance-history": "config,symbol,start,end,interval,catalog",
    "freeze-event-history": "database_url,start,end,catalog",
    "governance-service": "config,database_url,release_manifest,project_root",
    "information-collector": "config,database_url,release_manifest",
    "invalidate-evaluation-plan": "database_url,plan_id,reason_code,evidence_id",
    "lifecycle-service": "config,database_url,release_manifest",
    "market-stream": "config,database_url,release_manifest",
    "outcome-evaluation-service": "config,database_url,release_manifest",
    "paired-decision-tape": (
        "config,database_url,pipeline_version,symbol,plan_id,signal_end,"
        "source_blind_evaluation_id,dataset_id,horizon_minutes,maximum_age_minutes,"
        "minimum_confidence,minimum_non_overlapping_forecasts,candidate,catalog,"
        "blind_evaluation_catalog,starting_equity,spread_bps,include_trades,register_only"
    ),
    "phase-a-audit": "config,project_root",
    "reconciliation-service": "config,database_url,release_manifest",
    "register-ai-forecast-plan": (
        "config,database_url,plan_id,signal_window_start,signal_window_end,"
        "analysis_behavior_hash,minimum_non_overlapping_samples"
    ),
    "register-carry-forward-plan": (
        "database_url,plan_id,symbol,observation_start,observation_end"
    ),
    "replay-event-triggers": (
        "config,database_url,event_dataset_id,replay_start,replay_end,"
        "analysis_duration_seconds,admission_order,event_catalog,include_batches"
    ),
    "research-catalog": "evaluation_catalog",
    "reset-portfolio-protection": "config,database_url,reason,acknowledge_risk",
    "run-mock": "input_path,config",
    "screen-signals": (
        "config,dataset_id,signal_start,signal_end,candidate,event_dataset_id,catalog,"
        "event_catalog,spread_bps,minimum_non_overlapping_samples,"
        "minimum_net_return_bps_lower_bound,minimum_incremental_return_bps_lower_bound"
    ),
    "shadow-audit": "config,project_root",
    "submit-analysis": "input_path,config,deadline_minutes",
    "temporal-worker": "config,database_url,release_manifest",
    "trigger-now": "symbol,request_id,reason,config,database_url,release_manifest",
    "trigger-service": "config,database_url,release_manifest",
    "validate-config": "config",
    "walk-forward": (
        "config,database_url,dataset_id,plan_id,training_bars,test_bars,blind_bars,candidate,"
        "event_dataset_id,funding_dataset_id,catalog,event_catalog,funding_catalog,"
        "evaluation_catalog,starting_equity,spread_bps,minimum_trades,minimum_profit_factor,"
        "minimum_average_net_return_bps_lower_bound,maximum_drawdown_fraction,"
        "minimum_positive_fold_fraction,include_trades,register_only"
    ),
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

    assert len(contract) == 59
    assert content_hash(contract) == (
        "ac09f5d6b44b1941b4ad3d4a92674bda5b2804ae1abd4df74417b4745e2b7a25"
    )


def test_cli_subcommand_parameter_contract_is_frozen() -> None:
    root = get_command(app)
    observed = {
        name: ",".join(parameter.name for parameter in command.params)
        for name, command in sorted(root.commands.items())
    }

    assert observed == CLI_CONTRACT


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


def test_kernel_does_not_import_platform_or_business_modules() -> None:
    graph = _internal_import_graph()

    for module, dependencies in graph.items():
        if module.startswith("investment_manager.kernel"):
            assert not {
                dependency
                for dependency in dependencies
                if not dependency.startswith("investment_manager.kernel")
            }


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
                "investment_manager.domain"
            ):
                assert not kernel_primitives.intersection(
                    alias.name for alias in node.names
                ), path


def test_market_models_are_imported_from_their_domain_owner() -> None:
    market_models = {"FeatureSnapshot", "MarketBar", "MarketSnapshot"}

    for path in (*PACKAGE_ROOT.rglob("*.py"), *(ROOT / "tests").rglob("*.py")):
        if path == PACKAGE_ROOT / "domain.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module == (
                "investment_manager.domain"
            ):
                assert not market_models.intersection(
                    alias.name for alias in node.names
                ), path

    assert not (PACKAGE_ROOT / "market_data.py").exists()
    assert not (PACKAGE_ROOT / "market_data_sql.py").exists()
    assert not (PACKAGE_ROOT / "features.py").exists()


def test_information_facts_are_imported_from_their_domain_owner() -> None:
    moved_models = {"IntelligenceEvent", "SourceObservation", "SourceTier"}
    old_modules = {
        "investment_manager.domain",
        "investment_manager.asset_management",
    }

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


def test_old_package_and_console_entry_are_removed() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]

    assert not (ROOT / "src" / "quant_core").exists()
    assert project["name"] == "investment-manager"
    assert project["scripts"] == {
        "investment-manager": "investment_manager.cli:app"
    }
