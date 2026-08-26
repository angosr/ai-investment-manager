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
    preregister_world_model_ablation_plan,
)
from investment_manager.governance.evaluation.reference_selection import (
    load_reference_selection_artifact,
    validate_reference_policy_selection,
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
from investment_manager.market.models import SpotVenue
from investment_manager.market.tables import cross_venue_spot_quotes
from investment_manager.platform.database import (
    DATABASE_SCHEMA_VERSION,
    build_engine,
    require_current_schema,
)
from investment_manager.scheduling.repository import SqlTriggerRepository
from investment_manager.scheduling.workflows import coordinator_workflow_id
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


@app.command("preregister-world-model-ablation")
def preregister_world_model_ablation(
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
) -> None:
    """在候选首次前向时点前登记其不可变 WorldModel 配对计划。"""

    root = project_root.resolve()
    loaded = load_config(config.resolve())
    manifest = load_release_manifest(release_manifest.resolve())
    validate_manifest_against_config(
        manifest,
        loaded,
        require_configuration_hash=True,
    )
    imported_root = validate_manifest_code_version(manifest)
    if imported_root != root:
        raise ValueError("预登记入口没有从候选冻结 checkout 导入代码")
    validate_manifest_code_version(manifest, repository_root=root)
    validate_runtime_release_checkout(root)
    validate_manifest_artifacts(
        manifest,
        repository_root=root,
        required_ids=_required_release_artifacts(loaded),
    )
    engine = build_engine(database_url)
    try:
        require_current_schema(engine)
        plan = preregister_world_model_ablation_plan(
            config=loaded,
            engine=engine,
            release=manifest,
            registered_at=datetime.now(UTC),
        )
    finally:
        engine.dispose()
    typer.echo(plan.model_dump_json(indent=2))


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
            group, active_rollback_unit = _start_candidate_or_rollback(
                unit=unit,
                config=loaded,
                manifest=manifest,
                database_url=database_url,
                runtime_directory=runtime_root,
                state_path=state_path,
                timeout_seconds=readiness_timeout_seconds,
                rollback_unit=rollback_unit,
                rollback_fallback=rollback_fallback,
            )
        except Exception:
            writer_lease.release()
            raise

    assert group is not None
    typer.echo(
        f"Release 已 ready：{group.unit.manifest_id} · "
        f"http://{group.unit.dashboard_host}:{group.unit.dashboard_port}"
    )
    failed = False
    recovery_attempted = False
    try:
        while True:
            exited = _wait_for_service_exit(group, stop_requested=stop_requested)
            if exited is None:
                break
            if recovery_attempted:
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
                raise RuntimeError(f"恢复后的 Release 服务再次异常退出：{reason}")
            try:
                group, active_rollback_unit = _recover_ready_failure(
                    group=group,
                    exited=exited,
                    rollback_unit=active_rollback_unit,
                    rollback_fallback=rollback_fallback,
                    database_url=database_url,
                    runtime_directory=runtime_root,
                    state_path=state_path,
                    timeout_seconds=readiness_timeout_seconds,
                )
            except Exception:
                failed = True
                raise
            recovery_attempted = True
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


def _wait_for_service_exit(
    group: ManagedProcessGroup,
    *,
    stop_requested: threading.Event,
) -> dict[RuntimeService, int] | None:
    while not stop_requested.wait(1.0):
        exited = group.exited()
        if exited:
            return exited
    return None


def _recover_ready_failure(
    *,
    group: ManagedProcessGroup,
    exited: dict[RuntimeService, int],
    rollback_unit: ReleaseRuntimeUnit | None,
    rollback_fallback: ReleaseRuntimeUnit | None,
    database_url: str,
    runtime_directory: Path,
    state_path: Path,
    timeout_seconds: int,
) -> tuple[ManagedProcessGroup, ReleaseRuntimeUnit | None]:
    reason = ", ".join(
        f"{service.value}={code}" for service, code in exited.items()
    )
    save_runtime_state(
        state_path,
        group.state(
            status=RuntimeStateStatus.FAILED,
            rollback_unit=rollback_unit,
            failure_reason=f"SERVICE_EXITED:{reason}",
        ),
    )
    group.stop()
    if rollback_unit is None:
        raise RuntimeError(f"Release 服务异常退出且没有可恢复版本：{reason}")
    next_rollback = (
        rollback_fallback
        if rollback_fallback is not None
        and rollback_fallback.manifest_id != rollback_unit.manifest_id
        else None
    )
    typer.echo(
        f"Release 服务异常退出：{reason}；恢复 {rollback_unit.manifest_id}"
    )
    rollback_config = load_config(rollback_unit.config_path)
    rollback_manifest = load_release_manifest(rollback_unit.manifest_path)
    recovered = _start_until_ready(
        unit=rollback_unit,
        config=rollback_config,
        manifest=rollback_manifest,
        database_url=database_url,
        runtime_directory=runtime_directory,
        state_path=state_path,
        timeout_seconds=timeout_seconds,
        rollback_unit=next_rollback,
    )
    return recovered, next_rollback


def _start_candidate_or_rollback(
    *,
    unit: ReleaseRuntimeUnit,
    config: AppConfig,
    manifest,
    database_url: str,
    runtime_directory: Path,
    state_path: Path,
    timeout_seconds: int,
    rollback_unit: ReleaseRuntimeUnit | None,
    rollback_fallback: ReleaseRuntimeUnit | None,
) -> tuple[ManagedProcessGroup, ReleaseRuntimeUnit | None]:
    try:
        return (
            _start_until_ready(
                unit=unit,
                config=config,
                manifest=manifest,
                database_url=database_url,
                runtime_directory=runtime_directory,
                state_path=state_path,
                timeout_seconds=timeout_seconds,
                rollback_unit=rollback_unit,
            ),
            rollback_unit,
        )
    except Exception as candidate_error:
        if rollback_unit is None:
            raise typer.BadParameter(
                f"候选 Release 未 ready 且没有可回滚版本：{candidate_error}"
            ) from candidate_error
        typer.echo(f"候选 Release 未 ready，回滚 {rollback_unit.manifest_id}")
        rollback_config = load_config(rollback_unit.config_path)
        rollback_manifest = load_release_manifest(rollback_unit.manifest_path)
        return (
            _start_until_ready(
                unit=rollback_unit,
                config=rollback_config,
                manifest=rollback_manifest,
                database_url=database_url,
                runtime_directory=runtime_directory,
                state_path=state_path,
                timeout_seconds=timeout_seconds,
                rollback_unit=rollback_fallback,
            ),
            rollback_fallback,
        )


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
        required_ids=_required_release_artifacts(config),
    )
    if reference := config.capital.reference_policy:
        selection_path = resolve_manifest_artifact(
            manifest,
            reference.selection_artifact_id,
            repository_root=unit.project_root,
        )
        selection = load_reference_selection_artifact(
            selection_path,
            expected_artifact_id=reference.selection_artifact_id,
        )
        validate_reference_policy_selection(selection, reference)
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
                assemble_outcome_evaluation(
                    config,
                    assembly_url,
                    release=manifest,
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


def _required_release_artifacts(config: AppConfig) -> tuple[str, ...]:
    reference = config.capital.reference_policy
    required = {"web-dist"}
    if reference is not None:
        required.add(reference.selection_artifact_id)
    return tuple(sorted(required))


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
    asyncio.run(_require_trigger_coordinators_idle(previous.unit.config_path))


async def _require_trigger_coordinators_idle(config_path: Path) -> None:
    """Do not strand a version-bound child workflow during a release switch."""

    config = load_config(config_path)
    client = await Client.connect(
        config.temporal.address,
        namespace=config.temporal.namespace,
    )

    async def status(symbol: str) -> tuple[str, dict]:
        workflow_id = coordinator_workflow_id(symbol, config.pipeline.version)
        payload = await asyncio.wait_for(
            client.get_workflow_handle(workflow_id).query("status"),
            timeout=3,
        )
        if not isinstance(payload, dict):
            raise ValueError(f"TriggerCoordinator {symbol} 状态格式无效")
        return symbol, payload

    try:
        statuses = await asyncio.gather(*(status(symbol) for symbol in config.analysis_symbols))
    except Exception as exc:
        raise ValueError("切流前无法确认 TriggerCoordinator 空闲") from exc
    active = tuple(
        sorted(symbol for symbol, payload in statuses if payload.get("active_batch_id") is not None)
    )
    if active:
        raise ValueError("切流前仍有活动 TriggerBatch：" + ", ".join(active))


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
        if config.market_data.cross_venue_spot is not None:
            with engine.connect() as connection:
                for venue in SpotVenue:
                    latest_cross_venue = connection.scalar(
                        select(func.max(cross_venue_spot_quotes.c.observed_at)).where(
                            cross_venue_spot_quotes.c.venue == venue.value
                        )
                    )
                    if latest_cross_venue is None or latest_cross_venue < group.started_at:
                        missing.append(f"market-cross-venue:{venue.value}")

        market_coverage_streams = {
            "binance-usdm-market",
            "coinbase-spot-market",
            "kraken-spot-market",
        }
        information_streams = tuple(
            sorted(
                {
                    source.stream_id
                    for requirement in config.information.coverage_requirements
                    for source in requirement.sources
                    if source.stream_id not in market_coverage_streams
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
