from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from investment_manager.entrypoints.cli.release_commands import (
    _initialize_assembly_database,
)
from investment_manager.governance.release.deployment import (
    SERVICE_ORDER,
    ManagedProcessGroup,
    ReleaseRuntimeState,
    ReleaseRuntimeUnit,
    RuntimeService,
    RuntimeStateStatus,
    child_environment,
    exclusive_runtime_lock,
    load_runtime_state,
    save_runtime_state,
    service_command,
)
from investment_manager.platform.database import build_engine, require_current_schema


def _unit(tmp_path: Path) -> ReleaseRuntimeUnit:
    root = tmp_path / "checkout"
    config = root / "config" / "investment-manager.shadow.yaml"
    manifest = tmp_path / "release-manifest.yaml"
    command = tmp_path / "investment-manager"
    config.parent.mkdir(parents=True)
    config.write_text("version: test\n", encoding="utf-8")
    manifest.write_text("manifest_id: release-test\n", encoding="utf-8")
    command.write_text(
        "#!/bin/sh\ntrap 'exit 0' TERM INT\nsleep 30\n",
        encoding="utf-8",
    )
    command.chmod(0o755)
    return ReleaseRuntimeUnit(
        manifest_id="release-test",
        code_version="a" * 40,
        project_root=root,
        config_path=config,
        manifest_path=manifest,
        command_path=command,
    )


def test_release_unit_builds_one_frozen_command_per_service(tmp_path: Path) -> None:
    unit = _unit(tmp_path)

    commands = {service: service_command(unit, service) for service in SERVICE_ORDER}

    assert tuple(commands) == SERVICE_ORDER
    assert all(str(unit.config_path) in command for command in commands.values())
    assert all(str(unit.manifest_path) in command for command in commands.values())
    assert "--port" not in commands[RuntimeService.TRIGGER]
    assert commands[RuntimeService.DASHBOARD][-4:] == (
        "--host",
        "127.0.0.1",
        "--port",
        "8090",
    )


def test_release_children_import_candidate_before_existing_pythonpath(tmp_path: Path) -> None:
    unit = _unit(tmp_path)

    environment = child_environment(unit, {"PYTHONPATH": "/existing", "SAFE": "yes"})

    assert environment["PYTHONPATH"].split(os.pathsep) == [
        str(unit.project_root / "src"),
        "/existing",
    ]
    assert environment["SAFE"] == "yes"


def test_ready_state_requires_exact_complete_service_set(tmp_path: Path) -> None:
    unit = _unit(tmp_path)
    payload = {
        "unit": unit,
        "supervisor_pid": os.getpid(),
        "status": RuntimeStateStatus.READY,
        "started_at": datetime.now(UTC),
        "ready_at": datetime.now(UTC),
        "processes": [
            {"service": service, "pid": index + 100}
            for index, service in enumerate(SERVICE_ORDER[:-1])
        ],
    }

    with pytest.raises(ValidationError, match="完整服务集合"):
        ReleaseRuntimeState.model_validate(payload)


def test_runtime_state_round_trips_atomically(tmp_path: Path) -> None:
    unit = _unit(tmp_path)
    state = ReleaseRuntimeState(
        unit=unit,
        supervisor_pid=os.getpid(),
        status=RuntimeStateStatus.STARTING,
        started_at=datetime.now(UTC),
    )
    path = tmp_path / "runtime" / "active-release.json"

    save_runtime_state(path, state)

    assert load_runtime_state(path) == state
    assert not tuple(path.parent.glob("*.tmp"))


def test_runtime_lock_rejects_a_second_cutover(tmp_path: Path) -> None:
    path = tmp_path / "cutover.lock"

    with (
        exclusive_runtime_lock(path),
        pytest.raises(RuntimeError, match="已被占用"),
        exclusive_runtime_lock(path, blocking=False),
    ):
        pass


def test_process_group_starts_and_stops_the_exact_release_set(tmp_path: Path) -> None:
    unit = _unit(tmp_path)
    group = ManagedProcessGroup(unit=unit, runtime_directory=tmp_path / "runtime")

    group.start_core(environment=child_environment(unit, os.environ))
    group.start_dashboard(environment=child_environment(unit, os.environ))
    try:
        assert tuple(group.process_ids) == SERVICE_ORDER
        assert not group.exited()
        ready = group.state(
            status=RuntimeStateStatus.READY,
            ready_at=datetime.now(UTC),
        )
        assert tuple(item.service for item in ready.processes) == SERVICE_ORDER
    finally:
        group.stop(grace_seconds=2)

    assert set(group.exited()) == set(SERVICE_ORDER)


def test_assembly_database_has_the_same_runtime_schema_contract(tmp_path: Path) -> None:
    engine = build_engine(f"sqlite+pysqlite:///{tmp_path / 'assembly.sqlite'}")
    try:
        _initialize_assembly_database(engine)
        require_current_schema(engine)
    finally:
        engine.dispose()
