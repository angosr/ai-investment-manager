from __future__ import annotations

import asyncio
import os
import signal
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import httpx
import typer
from sqlalchemy import func, select, text
from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client

from investment_manager.decision_cycle.service import assemble_trigger_service
from investment_manager.entrypoints.cli.root import app
from investment_manager.entrypoints.cli.service_commands import (
    assemble_information_service,
    assemble_market_service,
)
from investment_manager.entrypoints.dashboard import create_app
from investment_manager.entrypoints.dashboard.capital import CapitalDashboardReader
from investment_manager.entrypoints.dashboard.read_models import DashboardReader
from investment_manager.forecast.context.service import assemble_assessment_service
from investment_manager.governance.audit.acceptance import AuditProfile, PhaseAAuditor
from investment_manager.governance.evaluation.outcome_service import (
    assemble_outcome_evaluation,
)
from investment_manager.governance.models import (
    load_release_manifest,
    resolve_manifest_artifact,
    validate_manifest_against_config,
    validate_manifest_artifacts,
    validate_manifest_code_version,
    validate_runtime_release_checkout,
)
from investment_manager.governance.release.deployment import (
    ManagedProcessGroup,
    ReleaseRuntimeState,
    ReleaseRuntimeUnit,
    RuntimeLease,
    RuntimeService,
    RuntimeStateStatus,
    child_environment,
    exclusive_runtime_lock,
    load_runtime_state,
    process_exists,
    save_runtime_state,
)
from investment_manager.governance.repository import SqlGovernanceRepository
from investment_manager.information.tables import source_poll_records
from investment_manager.platform.database import (
    DATABASE_SCHEMA_VERSION,
    build_engine,
    require_current_schema,
)
from investment_manager.scheduling.repository import SqlTriggerRepository
from investment_manager.schema import compose_metadata
from investment_manager.settings import AppConfig, load_config

_SAFETY_HEALTH_KEYS = {
    "capital_account",
    "capital_execution",
    "trigger_delivery",
    "trigger_coordinator",
}
_DASHBOARD_READY_KEYS = {
    *_SAFETY_HEALTH_KEYS,
    "capital_freshness",
    "release_alignment",
}


@app.command("operate-release")
def operate_release(
    project_root: Annotated[Path, typer.Option(exists=True, file_okay=False)],
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    release_manifest: Annotated[
        Path,
        typer.Option("--release-manifest", exists=True, dir_okay=False),
    ],
    database_url: Annotated[
        str,
        typer.Option(
            envvar="INVESTMENT_MANAGER_DATABASE_URL",
            help="仅从受控环境注入数据库 URL",
        ),
    ],
    runtime_directory: Annotated[Path, typer.Option()] = Path(".runtime/managed-release"),
    command_path: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
    readiness_timeout_seconds: Annotated[int, typer.Option(min=10, max=600)] = 120,
    dashboard_host: Annotated[str, typer.Option()] = "127.0.0.1",
    dashboard_port: Annotated[int, typer.Option(min=1, max=65535)] = 8090,
) -> None:
    """预检、切换并持续监督唯一冻结 Release；ready 失败时回滚完整旧版本。"""

    root = project_root.resolve()
    config_path = config.resolve()
    manifest_path = release_manifest.resolve()
    runtime_root = runtime_directory.resolve()
    executable = (command_path or (Path(os.sys.executable).parent / "investment-manager")).resolve()
    loaded = load_config(config_path)
    manifest = load_release_manifest(manifest_path)
    unit = ReleaseRuntimeUnit(
        manifest_id=manifest.manifest_id,
        code_version=manifest.code_version,
        project_root=root,
        config_path=config_path,
        manifest_path=manifest_path,
        command_path=executable,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
    )
    state_path = runtime_root / "active-release.json"
    stop_requested = threading.Event()

    def request_stop(_signum, _frame) -> None:
        stop_requested.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    writer_lease = RuntimeLease(runtime_root / "writer.lock")
    group: ManagedProcessGroup | None = None
    active_rollback_unit: ReleaseRuntimeUnit | None = None
    with exclusive_runtime_lock(runtime_root / "cutover.lock", blocking=False):
        _preflight_release(
            unit=unit,
            config=loaded,
            manifest=manifest,
            database_url=database_url,
        )
        current = load_runtime_state(state_path)
        rollback_unit = None
        rollback_fallback = None
        if current is not None and current.status == RuntimeStateStatus.READY:
            _require_safe_cutover(current, runtime_directory=runtime_root)
            _stop_previous(current, timeout_seconds=30)
            rollback_unit = current.unit
            rollback_fallback = current.rollback_unit
        elif current is not None:
            _require_interrupted_release_stopped(current)
            rollback_unit = current.rollback_unit
        writer_lease.acquire(blocking=True)
        try:
            group = _start_until_ready(
                unit=unit,
                config=loaded,
                manifest=manifest,
                database_url=database_url,
                runtime_directory=runtime_root,
                state_path=state_path,
                timeout_seconds=readiness_timeout_seconds,
                rollback_unit=rollback_unit,
            )
            active_rollback_unit = rollback_unit
        except Exception as candidate_error:
            if group is not None:
                group.stop()
            if rollback_unit is None:
                writer_lease.release()
                raise typer.BadParameter(
                    f"候选 Release 未 ready 且没有可回滚版本：{candidate_error}"
                ) from candidate_error
            typer.echo(f"候选 Release 未 ready，回滚 {rollback_unit.manifest_id}")
            rollback_config = load_config(rollback_unit.config_path)
            rollback_manifest = load_release_manifest(rollback_unit.manifest_path)
            try:
                group = _start_until_ready(
                    unit=rollback_unit,
                    config=rollback_config,
                    manifest=rollback_manifest,
                    database_url=database_url,
                    runtime_directory=runtime_root,
                    state_path=state_path,
                    timeout_seconds=readiness_timeout_seconds,
                    rollback_unit=rollback_fallback,
                )
                active_rollback_unit = rollback_fallback
            except Exception:
                writer_lease.release()
                raise

    assert group is not None
    typer.echo(
        f"Release 已 ready：{group.unit.manifest_id} · "
        f"http://{group.unit.dashboard_host}:{group.unit.dashboard_port}"
    )
    failed = False
    try:
        while not stop_requested.wait(1.0):
            exited = group.exited()
            if exited:
                reason = ", ".join(
                    f"{service.value}={code}" for service, code in exited.items()
                )
                save_runtime_state(
                    state_path,
                    group.state(
                        status=RuntimeStateStatus.FAILED,
                        rollback_unit=active_rollback_unit,
                        failure_reason=f"SERVICE_EXITED:{reason}",
                    ),
                )
                failed = True
                raise RuntimeError(f"Release 服务异常退出：{reason}")
    finally:
        if not failed:
            save_runtime_state(
                state_path,
                group.state(
                    status=RuntimeStateStatus.STOPPING,
                    rollback_unit=active_rollback_unit,
                ),
            )
        group.stop()
        writer_lease.release()


def _preflight_release(
    *,
    unit: ReleaseRuntimeUnit,
    config: AppConfig,
    manifest,
    database_url: str,
) -> None:
    validate_manifest_against_config(manifest, config, require_configuration_hash=True)
    imported_root = validate_manifest_code_version(manifest)
    if imported_root != unit.project_root:
        raise ValueError("Release 发布入口没有从候选冻结 checkout 导入代码")
    validate_manifest_code_version(manifest, repository_root=unit.project_root)
    validate_runtime_release_checkout(unit.project_root)
    validate_manifest_artifacts(
        manifest,
        repository_root=unit.project_root,
        required_ids=("web-dist",),
    )
    audit = PhaseAAuditor(
        config,
        unit.project_root,
        profile=AuditProfile.PRIVATE_CODEX_CHALLENGER,
        runtime_manifest=unit.manifest_path,
    ).run()
    if not audit.ready:
        failures = ", ".join(
            item.check_id for item in audit.checks if item.status != "PASS"
        )
        raise ValueError(f"Release Challenger 预检失败：{failures}")

    production_engine = build_engine(database_url)
    require_current_schema(production_engine)
    try:
        with tempfile.TemporaryDirectory(prefix="investment-manager-release-") as directory:
            database_path = Path(directory) / "assembly.sqlite"
            assembly_url = f"sqlite+pysqlite:///{database_path}"
            assembly_engine = build_engine(assembly_url)
            market = None
            try:
                _initialize_assembly_database(assembly_engine)
                _seed_pre_registered_plan(
                    config=config,
                    production=SqlGovernanceRepository(production_engine),
                    assembly=SqlGovernanceRepository(assembly_engine),
                )
                market = assemble_market_service(config, assembly_engine)
                assemble_information_service(config, assembly_engine)
                assemble_trigger_service(
                    config=config,
                    manifest=manifest,
                    engine=assembly_engine,
                )
                assemble_assessment_service(
                    config,
                    assembly_url,
                    code_version=manifest.code_version,
                    manifest_id=manifest.manifest_id,
                )
                asyncio.run(
                    _assemble_temporal_services(
                        config=config,
                        database_url=assembly_url,
                        manifest=manifest,
                    )
                )
                create_app(
                    config,
                    assembly_url,
                    web_dist=resolve_manifest_artifact(
                        manifest,
                        "web-dist",
                        repository_root=unit.project_root,
                    ),
                )
            finally:
                if market is not None:
                    asyncio.run(market.aclose())
                assembly_engine.dispose()
    finally:
        production_engine.dispose()


async def _assemble_temporal_services(*, config, database_url: str, manifest) -> None:
    client = await Client.connect(config.temporal.address, namespace=config.temporal.namespace)
    assemble_outcome_evaluation(config, database_url, client, release=manifest)


def _seed_pre_registered_plan(*, config, production, assembly) -> None:
    policy = config.outcome_evaluation.world_model_ablation
    if policy is None or not policy.enabled:
        return
    existing = production.get_plan(policy.plan_id)
    if existing is None:
        raise ValueError("WorldModel 前瞻评价计划尚未预登记")
    assembly.register_plan(existing)


def _initialize_assembly_database(engine) -> None:
    compose_metadata().create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:version)"),
            {"version": DATABASE_SCHEMA_VERSION},
        )


def _require_safe_cutover(
    previous: ReleaseRuntimeState,
    *,
    runtime_directory: Path,
) -> None:
    if previous.status != RuntimeStateStatus.READY:
        raise ValueError("当前受管 Release 不是 READY，必须先恢复运行状态")
    if not process_exists(previous.supervisor_pid):
        raise ValueError("当前 Release supervisor 已消失，拒绝猜测孤儿写进程状态")
    lease = RuntimeLease(runtime_directory / "writer.lock")
    try:
        lease.acquire(blocking=False)
    except RuntimeError:
        pass
    else:
        lease.release()
        raise ValueError("当前 READY Release 没有持有单写者锁")
    url = (
        f"http://{previous.unit.dashboard_host}:"
        f"{previous.unit.dashboard_port}/api/health"
    )
    response = httpx.get(url, timeout=5)
    response.raise_for_status()
    _require_health_checks(response.json(), required=_SAFETY_HEALTH_KEYS)


def _stop_previous(previous: ReleaseRuntimeState, *, timeout_seconds: int) -> None:
    os.kill(previous.supervisor_pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout_seconds
    pids = (previous.supervisor_pid, *(item.pid for item in previous.processes))
    while time.monotonic() < deadline:
        if not any(process_exists(pid) for pid in pids):
            return
        time.sleep(0.25)
    raise RuntimeError("旧 Release 未在有界时间内完整停止")


def _require_interrupted_release_stopped(state: ReleaseRuntimeState) -> None:
    pids = (state.supervisor_pid, *(item.pid for item in state.processes))
    if any(process_exists(pid) for pid in pids):
        raise ValueError("上次 Release 切换未完成且仍有进程存活，拒绝并行恢复")
    if state.rollback_unit is None:
        raise ValueError("上次 Release 切换未完成且缺少耐久回滚版本")


def _start_until_ready(
    *,
    unit: ReleaseRuntimeUnit,
    config: AppConfig,
    manifest,
    database_url: str,
    runtime_directory: Path,
    state_path: Path,
    timeout_seconds: int,
    rollback_unit: ReleaseRuntimeUnit | None,
) -> ManagedProcessGroup:
    group = ManagedProcessGroup(unit=unit, runtime_directory=runtime_directory)
    environment = child_environment(unit, os.environ)
    try:
        group.start_core(environment=environment)
        save_runtime_state(
            state_path,
            group.state(
                status=RuntimeStateStatus.STARTING,
                rollback_unit=rollback_unit,
            ),
        )
        deadline = time.monotonic() + timeout_seconds
        last_missing: tuple[str, ...] = ()
        while time.monotonic() < deadline:
            exited = group.exited()
            if exited:
                raise RuntimeError(f"核心服务启动失败：{exited}")
            last_missing = asyncio.run(
                _core_readiness_missing(
                    group=group,
                    config=config,
                    manifest=manifest,
                    database_url=database_url,
                )
            )
            if not last_missing:
                break
            time.sleep(1)
        else:
            raise TimeoutError("Release 核心 readiness 超时：" + ", ".join(last_missing))

        group.start_dashboard(environment=environment)
        dashboard_deadline = min(deadline, time.monotonic() + 20)
        while time.monotonic() < dashboard_deadline:
            exited = group.exited()
            if exited:
                raise RuntimeError(f"Dashboard 切换失败：{exited}")
            try:
                response = httpx.get(
                    f"http://{unit.dashboard_host}:{unit.dashboard_port}/api/health",
                    timeout=3,
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("pipeline_version") != config.pipeline.version:
                    raise ValueError("Dashboard pipeline 尚未切换到候选 Release")
                _require_health_checks(payload, required=_DASHBOARD_READY_KEYS)
                ready_at = datetime.now(UTC)
                save_runtime_state(
                    state_path,
                    group.state(
                        status=RuntimeStateStatus.READY,
                        rollback_unit=rollback_unit,
                        ready_at=ready_at,
                    ),
                )
                return group
            except (httpx.HTTPError, ValueError):
                time.sleep(0.5)
        raise TimeoutError("Dashboard 未在有界时间内形成候选 Release 健康投影")
    except Exception as exc:
        save_runtime_state(
            state_path,
            group.state(
                status=RuntimeStateStatus.FAILED,
                rollback_unit=rollback_unit,
                failure_reason=type(exc).__name__,
            ),
        )
        group.stop()
        raise


async def _core_readiness_missing(
    *,
    group: ManagedProcessGroup,
    config: AppConfig,
    manifest,
    database_url: str,
) -> tuple[str, ...]:
    missing: list[str] = []
    engine = build_engine(database_url)
    try:
        repository = SqlTriggerRepository(engine, config.trigger)
        for symbol in config.analysis_symbols:
            try:
                plan = repository.plan_for_scope(
                    symbol=symbol,
                    pipeline_id=config.pipeline.version,
                )
            except KeyError:
                missing.append(f"trigger-plan:{symbol}")
                continue
            if plan.manifest_id != manifest.manifest_id:
                missing.append(f"trigger-plan-release:{symbol}")

        reader = DashboardReader(engine, config)
        spot_at = reader.latest_market_observed_at()
        if spot_at is None or spot_at < group.started_at:
            missing.append("market-spot")
        if config.market_data.perpetual_instruments:
            perpetual_at = reader.latest_perpetual_observed_at()
            if perpetual_at is None or perpetual_at < group.started_at:
                missing.append("market-perpetual")

        information_streams = tuple(
            sorted(
                {
                    stream
                    for requirement in config.information.coverage_requirements
                    for stream in requirement.source_stream_ids
                    if stream != "binance-usdm-market"
                }
            )
        )
        with engine.connect() as connection:
            latest_information = connection.scalar(
                select(func.max(source_poll_records.c.completed_at)).where(
                    source_poll_records.c.source_stream_id.in_(information_streams)
                )
            )
        if latest_information is None or latest_information < group.started_at:
            missing.append("information-poll")

        capital = CapitalDashboardReader(engine, config).overview()
        if capital.account is None or not capital.account.reconciled:
            missing.append("account-recovery")
        if capital.active_groups:
            missing.append("execution-pending")
    finally:
        engine.dispose()

    client = await Client.connect(config.temporal.address, namespace=config.temporal.namespace)
    queue_pids = (
        (
            config.temporal.assessment_task_queue,
            group.process_ids[RuntimeService.ASSESSMENT],
        ),
        (config.temporal.trigger_task_queue, group.process_ids[RuntimeService.TRIGGER]),
        (
            config.temporal.outcome_evaluation_task_queue,
            group.process_ids[RuntimeService.OUTCOME],
        ),
    )
    for task_queue, pid in queue_pids:
        for queue_type in (
            TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
            TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY,
        ):
            response = await client.workflow_service.describe_task_queue(
                DescribeTaskQueueRequest(
                    namespace=client.namespace,
                    task_queue=TaskQueue(name=task_queue),
                    task_queue_type=queue_type,
                )
            )
            if not any(poller.identity.startswith(f"{pid}@") for poller in response.pollers):
                missing.append(f"worker-poller:{task_queue}:{int(queue_type)}")
    return tuple(sorted(set(missing)))


def _require_health_checks(payload: dict, *, required: set[str]) -> None:
    checks = {
        str(item.get("key")): str(item.get("state"))
        for item in payload.get("checks", ())
        if isinstance(item, dict)
    }
    missing = required - checks.keys()
    unhealthy = {key: checks.get(key) for key in required if checks.get(key) != "ok"}
    if missing or unhealthy:
        raise ValueError(
            "Release health 未满足切换条件："
            f"missing={sorted(missing)}, unhealthy={unhealthy}"
        )
