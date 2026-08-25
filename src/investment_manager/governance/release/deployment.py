from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import IO

from pydantic import Field, field_validator, model_validator

from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel


class RuntimeService(StrEnum):
    MARKET = "market-stream"
    INFORMATION = "information-collector"
    OUTCOME = "outcome-evaluation-service"
    TRIGGER = "trigger-service"
    ASSESSMENT = "assessment-worker"
    DASHBOARD = "dashboard-service"


CORE_SERVICE_ORDER = (
    RuntimeService.MARKET,
    RuntimeService.INFORMATION,
    RuntimeService.OUTCOME,
    RuntimeService.TRIGGER,
    RuntimeService.ASSESSMENT,
)
SERVICE_ORDER = (*CORE_SERVICE_ORDER, RuntimeService.DASHBOARD)


class ReleaseRuntimeUnit(FrozenModel):
    manifest_id: str = Field(min_length=1)
    code_version: str = Field(min_length=1)
    project_root: Path
    config_path: Path
    manifest_path: Path
    command_path: Path
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = Field(default=8090, ge=1, le=65535)

    @model_validator(mode="after")
    def paths_must_be_explicit_and_frozen(self):
        paths = (
            self.project_root,
            self.config_path,
            self.manifest_path,
            self.command_path,
        )
        if any(not item.is_absolute() for item in paths):
            raise ValueError("Release 运行路径必须全部为绝对路径")
        if not self.project_root.is_dir():
            raise ValueError("Release checkout 不存在")
        if not self.config_path.is_file() or not self.manifest_path.is_file():
            raise ValueError("Release 配置或 Manifest 不存在")
        if not self.command_path.is_file() or not os.access(self.command_path, os.X_OK):
            raise ValueError("Release 命令入口不可执行")
        try:
            self.config_path.relative_to(self.project_root)
        except ValueError as exc:
            raise ValueError("Release 配置必须来自冻结 checkout") from exc
        if self.dashboard_host not in {"127.0.0.1", "localhost"}:
            raise ValueError("Dashboard 只能绑定本机回环地址")
        return self


class RuntimeStateStatus(StrEnum):
    STARTING = "STARTING"
    READY = "READY"
    STOPPING = "STOPPING"
    FAILED = "FAILED"


class RuntimeServiceProcess(FrozenModel):
    service: RuntimeService
    pid: int = Field(ge=1)


class ReleaseRuntimeState(FrozenModel):
    unit: ReleaseRuntimeUnit
    rollback_unit: ReleaseRuntimeUnit | None = None
    supervisor_pid: int = Field(ge=1)
    status: RuntimeStateStatus
    started_at: datetime
    ready_at: datetime | None = None
    processes: tuple[RuntimeServiceProcess, ...] = ()
    failure_reason: str | None = None

    _utc_started_at = field_validator("started_at")(require_utc)
    _utc_ready_at = field_validator("ready_at")(lambda value: require_utc(value) if value else None)

    @model_validator(mode="after")
    def service_processes_must_be_unique(self):
        services = tuple(item.service for item in self.processes)
        if len(set(services)) != len(services):
            raise ValueError("Release 服务进程身份重复")
        if self.rollback_unit is not None and (
            self.rollback_unit.manifest_id == self.unit.manifest_id
        ):
            raise ValueError("Release 回滚版本不得指向自身")
        if self.status == RuntimeStateStatus.READY:
            if self.ready_at is None or services != SERVICE_ORDER:
                raise ValueError("READY Release 必须包含完整服务集合和 ready_at")
        elif self.ready_at is not None:
            raise ValueError("非 READY Release 不得声明 ready_at")
        return self


def service_command(unit: ReleaseRuntimeUnit, service: RuntimeService) -> tuple[str, ...]:
    command = (
        str(unit.command_path),
        service.value,
        "--config",
        str(unit.config_path),
        "--release-manifest",
        str(unit.manifest_path),
    )
    if service == RuntimeService.DASHBOARD:
        return (
            *command,
            "--host",
            unit.dashboard_host,
            "--port",
            str(unit.dashboard_port),
        )
    return command


def child_environment(unit: ReleaseRuntimeUnit, source: dict[str, str]) -> dict[str, str]:
    environment = dict(source)
    frozen_source = str(unit.project_root / "src")
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        f"{frozen_source}{os.pathsep}{existing}" if existing else frozen_source
    )
    return environment


def save_runtime_state(path: Path, state: ReleaseRuntimeState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(
            state.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_runtime_state(path: Path) -> ReleaseRuntimeState | None:
    if not path.is_file():
        return None
    return ReleaseRuntimeState.model_validate_json(path.read_text(encoding="utf-8"))


@dataclass(slots=True)
class ManagedProcessGroup:
    unit: ReleaseRuntimeUnit
    runtime_directory: Path
    processes: dict[RuntimeService, subprocess.Popen] = field(default_factory=dict)
    _logs: list[IO[bytes]] = field(default_factory=list)
    _started_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def started_at(self) -> datetime:
        return self._started_at

    @property
    def process_ids(self) -> dict[RuntimeService, int]:
        return {service: process.pid for service, process in self.processes.items()}

    def start_core(self, *, environment: dict[str, str]) -> None:
        for service in CORE_SERVICE_ORDER:
            self._start(service, environment=environment)

    def start_dashboard(self, *, environment: dict[str, str]) -> None:
        self._start(RuntimeService.DASHBOARD, environment=environment)

    def exited(self) -> dict[RuntimeService, int]:
        return {
            service: int(code)
            for service, process in self.processes.items()
            if (code := process.poll()) is not None
        }

    def stop(self, *, grace_seconds: float = 20.0) -> None:
        for service in reversed(SERVICE_ORDER):
            process = self.processes.get(service)
            if process is not None and process.poll() is None:
                process.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + grace_seconds
        for service in reversed(SERVICE_ORDER):
            process = self.processes.get(service)
            if process is None or process.poll() is not None:
                continue
            remaining = max(0.0, deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for handle in self._logs:
            handle.close()
        self._logs.clear()

    def state(
        self,
        *,
        status: RuntimeStateStatus,
        rollback_unit: ReleaseRuntimeUnit | None = None,
        ready_at: datetime | None = None,
        failure_reason: str | None = None,
    ) -> ReleaseRuntimeState:
        return ReleaseRuntimeState(
            unit=self.unit,
            rollback_unit=rollback_unit,
            supervisor_pid=os.getpid(),
            status=status,
            started_at=self.started_at,
            ready_at=ready_at,
            processes=tuple(
                RuntimeServiceProcess(service=service, pid=self.processes[service].pid)
                for service in SERVICE_ORDER
                if service in self.processes
            ),
            failure_reason=failure_reason,
        )

    def _start(self, service: RuntimeService, *, environment: dict[str, str]) -> None:
        if service in self.processes:
            raise ValueError(f"Release 服务重复启动：{service.value}")
        log_directory = self.runtime_directory / "logs" / self.unit.manifest_id
        log_directory.mkdir(parents=True, exist_ok=True)
        handle = (log_directory / f"{service.value}.log").open("ab", buffering=0)
        self._logs.append(handle)
        process = subprocess.Popen(
            service_command(self.unit, service),
            cwd=self.unit.project_root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.processes[service] = process


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass(slots=True)
class RuntimeLease:
    path: Path
    _handle: IO[bytes] | None = None

    def acquire(self, *, blocking: bool = True) -> None:
        if self._handle is not None:
            raise RuntimeError("Release 运行锁已经持有")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle.fileno(), operation)
        except BlockingIOError as exc:
            handle.close()
            raise RuntimeError(f"Release 运行锁已被占用：{self.path.name}") from exc
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


@contextmanager
def exclusive_runtime_lock(path: Path, *, blocking: bool = True) -> Iterator[None]:
    """Hold one local orchestration or writer lease for the context lifetime."""

    lease = RuntimeLease(path)
    lease.acquire(blocking=blocking)
    try:
        yield
    finally:
        lease.release()
