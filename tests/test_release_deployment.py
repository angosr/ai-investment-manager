from __future__ import annotations

import asyncio
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
import typer
from pydantic import ValidationError

from investment_manager.entrypoints.cli import release_commands
from investment_manager.entrypoints.cli.release_commands import (
    _initialize_assembly_database,
    _recover_ready_failure,
    _require_trigger_coordinators_idle,
    _required_release_artifacts,
    _start_candidate_or_rollback,
    _wait_for_service_exit,
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
from investment_manager.scheduling.workflows import coordinator_workflow_id
from investment_manager.settings import load_config


def _unit(tmp_path: Path, manifest_id: str = "release-test") -> ReleaseRuntimeUnit:
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
        manifest_id=manifest_id,
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


def test_release_requires_the_bound_reference_selection_artifact() -> None:
    config = load_config("config/investment-manager.shadow.yaml")
    assert _required_release_artifacts(config) == ("web-dist",)

    payload = config.model_dump(mode="python")
    payload["capital"]["mandate"]["status"] = "APPROVED"
    payload["capital"]["investable_universe"]["instruments"][0][
        "reference_candidate"
    ] = True
    payload["capital"]["reference_policy"] = {
        "version": "reference-v1",
        "mandate_version": config.capital.mandate.version,
        "universe_version": config.capital.investable_universe.version,
        "selection_artifact_id": "reference-selection-v1",
        "allocations": (
            {
                "implementation_key": "BINANCE:SPOT:BTCUSDT",
                "target_exposure_fraction": "0.10",
            },
            {
                "implementation_key": "CASH:USDT",
                "target_exposure_fraction": "0.90",
            },
        ),
        "rebalance_band_fraction": "0.05",
    }
    configured = type(config).model_validate(payload)

    assert _required_release_artifacts(configured) == (
        "reference-selection-v1",
        "web-dist",
    )


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


def test_release_cutover_requires_every_trigger_coordinator_to_be_idle(
    monkeypatch,
) -> None:
    config = load_config("config/investment-manager.shadow.yaml")
    active_workflow_id = coordinator_workflow_id(
        config.analysis_symbols[0],
        config.pipeline.version,
    )

    class Handle:
        def __init__(self, workflow_id: str) -> None:
            self.workflow_id = workflow_id

        async def query(self, _name: str):
            return {
                "active_batch_id": (
                    "batch-active" if self.workflow_id == active_workflow_id else None
                )
            }

    class ClientStub:
        def get_workflow_handle(self, workflow_id: str):
            return Handle(workflow_id)

    async def connect(*_args, **_kwargs):
        return ClientStub()

    monkeypatch.setattr(release_commands.Client, "connect", connect)
    monkeypatch.setattr(
        release_commands,
        "_current_release_trigger_plans",
        lambda _database_url, *, manifest_id: tuple(
            type("Plan", (), {"symbol": symbol, "pipeline_id": config.pipeline.version})()
            for symbol in config.analysis_symbols
        ),
    )

    with pytest.raises(ValueError, match="活动 TriggerBatch"):
        asyncio.run(
            _require_trigger_coordinators_idle(
                config=config,
                database_url="unused",
                manifest_id="release-current",
            )
        )


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


def test_candidate_readiness_failure_rolls_back_the_complete_previous_unit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    candidate = _unit(tmp_path / "candidate", "release-candidate")
    rollback = _unit(tmp_path / "rollback", "release-rollback")
    fallback = _unit(tmp_path / "fallback", "release-fallback")
    calls: list[tuple[str, str | None]] = []
    rollback_group = object()

    def start(**kwargs):
        calls.append(
            (
                kwargs["unit"].manifest_id,
                (
                    kwargs["rollback_unit"].manifest_id
                    if kwargs["rollback_unit"] is not None
                    else None
                ),
            )
        )
        if len(calls) == 1:
            raise TimeoutError("candidate warming timeout")
        return rollback_group

    monkeypatch.setattr(release_commands, "_start_until_ready", start)
    monkeypatch.setattr(release_commands, "load_config", lambda _path: object())
    monkeypatch.setattr(release_commands, "load_release_manifest", lambda _path: object())

    group, next_rollback = _start_candidate_or_rollback(
        unit=candidate,
        config=object(),
        manifest=object(),
        database_url="postgresql://unused",
        runtime_directory=tmp_path,
        state_path=tmp_path / "state.json",
        timeout_seconds=30,
        rollback_unit=rollback,
        rollback_fallback=fallback,
    )

    assert group is rollback_group
    assert next_rollback == fallback
    assert calls == [
        ("release-candidate", "release-rollback"),
        ("release-rollback", "release-fallback"),
    ]


def test_candidate_failure_without_a_previous_release_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    candidate = _unit(tmp_path / "candidate", "release-candidate")
    monkeypatch.setattr(
        release_commands,
        "_start_until_ready",
        lambda **_kwargs: (_ for _ in ()).throw(TimeoutError("warming timeout")),
    )

    with pytest.raises(typer.BadParameter, match="没有可回滚版本"):
        _start_candidate_or_rollback(
            unit=candidate,
            config=object(),
            manifest=object(),
            database_url="postgresql://unused",
            runtime_directory=tmp_path,
            state_path=tmp_path / "state.json",
            timeout_seconds=30,
            rollback_unit=None,
            rollback_fallback=None,
        )


class _ReadyGroupStub:
    def __init__(self, unit: ReleaseRuntimeUnit) -> None:
        self.unit = unit
        self.stopped = False

    def state(self, *, status, rollback_unit=None, ready_at=None, failure_reason=None):
        return ReleaseRuntimeState(
            unit=self.unit,
            rollback_unit=rollback_unit,
            supervisor_pid=os.getpid(),
            status=status,
            started_at=datetime.now(UTC),
            ready_at=ready_at,
            failure_reason=failure_reason,
        )

    def stop(self) -> None:
        self.stopped = True


@pytest.mark.parametrize("service", SERVICE_ORDER)
def test_ready_service_exit_stops_group_and_recovers_previous_unit_once(
    tmp_path: Path,
    monkeypatch,
    service: RuntimeService,
) -> None:
    current = _unit(tmp_path / "current", "release-current")
    rollback = _unit(tmp_path / "rollback", "release-rollback")
    fallback = _unit(tmp_path / "fallback", "release-fallback")
    group = _ReadyGroupStub(current)
    recovered = object()
    calls = []

    def start(**kwargs):
        calls.append(kwargs)
        return recovered

    monkeypatch.setattr(release_commands, "_start_until_ready", start)
    monkeypatch.setattr(release_commands, "load_config", lambda _path: object())
    monkeypatch.setattr(release_commands, "load_release_manifest", lambda _path: object())
    state_path = tmp_path / "active-release.json"

    result, next_rollback = _recover_ready_failure(
        group=group,
        exited={service: 23},
        rollback_unit=rollback,
        rollback_fallback=fallback,
        database_url="postgresql://unused",
        runtime_directory=tmp_path,
        state_path=state_path,
        timeout_seconds=30,
    )

    assert result is recovered
    assert next_rollback == fallback
    assert group.stopped
    failed_state = load_runtime_state(state_path)
    assert failed_state is not None
    assert failed_state.status == RuntimeStateStatus.FAILED
    assert failed_state.failure_reason == f"SERVICE_EXITED:{service.value}=23"
    assert calls[0]["unit"] == rollback
    assert calls[0]["rollback_unit"] == fallback


def test_ready_service_exit_without_rollback_fails_closed_after_stopping_group(
    tmp_path: Path,
) -> None:
    current = _unit(tmp_path / "current", "release-current")
    group = _ReadyGroupStub(current)

    with pytest.raises(RuntimeError, match="没有可恢复版本"):
        _recover_ready_failure(
            group=group,
            exited={RuntimeService.TRIGGER: 9},
            rollback_unit=None,
            rollback_fallback=None,
            database_url="postgresql://unused",
            runtime_directory=tmp_path,
            state_path=tmp_path / "active-release.json",
            timeout_seconds=30,
        )

    assert group.stopped


def test_planned_stop_never_polls_children_or_triggers_recovery() -> None:
    stop_requested = threading.Event()
    stop_requested.set()

    class Group:
        def exited(self):
            raise AssertionError("计划停止不得检查子进程异常")

    assert _wait_for_service_exit(Group(), stop_requested=stop_requested) is None
