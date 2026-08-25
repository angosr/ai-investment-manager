from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, TypeAdapter

from investment_manager.forecast.codex.bundle import RunBundle
from investment_manager.forecast.codex.capacity import (
    AppServerCapacityProbe,
    CapacityProbe,
    CapacitySnapshot,
)
from investment_manager.forecast.codex.protocol import (
    FAILOVER_FAILURES,
    CodexExecutor,
    FailureClass,
    SubprocessCodexExecutor,
)
from investment_manager.forecast.policy import (
    CodexAccount,
    CodexAccountRegistry,
    CodexRuntimePolicy,
)
from investment_manager.kernel.identity import stable_id
from investment_manager.settings import AppConfig


class AccountState(StrEnum):
    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    LEASED = "LEASED"
    COOLDOWN = "COOLDOWN"
    AUTH_FAILED = "AUTH_FAILED"
    DISABLED = "DISABLED"


def _elapsed_time(started_at: datetime, monotonic_started: float) -> datetime:
    return started_at + timedelta(seconds=max(0.0, time.monotonic() - monotonic_started))


@dataclass(frozen=True, slots=True)
class CodexLease:
    lease_id: str
    account_id: str
    cycle_id: str
    attempt_id: str
    expires_at: datetime


class AccountLeaseStore(Protocol):
    def try_acquire(
        self, account_id: str, cycle_id: str, attempt_id: str, expires_at: datetime
    ) -> CodexLease | None: ...

    def release(self, lease_id: str) -> None: ...

    def has_active(self, account_id: str, now: datetime) -> bool: ...


@dataclass(frozen=True, slots=True)
class AttemptAudit:
    run_id: str
    cycle_id: str
    account_id: str
    attempt: int
    observed_at: datetime
    completed_at: datetime
    duration_ms: int
    runtime_policy_version: str
    status: str
    failure: FailureClass | None
    bundle_hash: str
    usage: dict[str, int]
    diagnostics: dict[str, int | str | bool] = field(default_factory=dict)
    analysis_behavior_hash: str | None = None


@dataclass(frozen=True, slots=True)
class LatestAccountAttempt:
    """Minimum persisted run state needed to restore account routing."""

    account_id: str
    status: str
    failure: FailureClass | None
    completed_at: datetime


class RouterAuditStore(Protocol):
    def latest_account_attempts(
        self, account_ids: tuple[str, ...]
    ) -> dict[str, LatestAccountAttempt]: ...

    def record_capacity(self, snapshot: CapacitySnapshot) -> None: ...

    def record_attempt(self, attempt: AttemptAudit) -> None: ...


class NullRouterAuditStore:
    def latest_account_attempts(
        self, account_ids: tuple[str, ...]
    ) -> dict[str, LatestAccountAttempt]:
        return {}

    def record_capacity(self, snapshot: CapacitySnapshot) -> None:
        return None

    def record_attempt(self, attempt: AttemptAudit) -> None:
        return None


@dataclass(slots=True)
class InMemoryAccountLeaseStore:
    _leases: dict[str, CodexLease] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def try_acquire(
        self, account_id: str, cycle_id: str, attempt_id: str, expires_at: datetime
    ) -> CodexLease | None:
        with self._lock:
            self._expire(datetime.now(tz=UTC))
            if any(item.account_id == account_id for item in self._leases.values()):
                return None
            lease = CodexLease(
                lease_id=stable_id("lease", account_id, cycle_id, attempt_id),
                account_id=account_id,
                cycle_id=cycle_id,
                attempt_id=attempt_id,
                expires_at=expires_at,
            )
            self._leases[lease.lease_id] = lease
            return lease

    def release(self, lease_id: str) -> None:
        with self._lock:
            self._leases.pop(lease_id, None)

    def has_active(self, account_id: str, now: datetime) -> bool:
        with self._lock:
            self._expire(now)
            return any(item.account_id == account_id for item in self._leases.values())

    def _expire(self, now: datetime) -> None:
        expired = [key for key, value in self._leases.items() if value.expires_at <= now]
        for key in expired:
            self._leases.pop(key, None)


@dataclass(slots=True)
class _AccountRuntime:
    state: AccountState
    snapshot: CapacitySnapshot | None = None
    cooldown_until: datetime | None = None
    last_used_at: datetime | None = None
    recent_failures: int = 0


@dataclass(frozen=True, slots=True)
class AnalystResult:
    success: bool
    output: BaseModel | None
    reason_code: str
    account_id: str | None = None
    attempts: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    completed_at: datetime | None = None
    run_id: str | None = None


class CodexAccountRouter:
    def __init__(
        self,
        registry: CodexAccountRegistry,
        policy: CodexRuntimePolicy,
        probe: CapacityProbe,
        executor: CodexExecutor,
        leases: AccountLeaseStore | None = None,
        audit: RouterAuditStore | None = None,
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._probe = probe
        self._executor = executor
        self._leases = leases or InMemoryAccountLeaseStore()
        self._audit = audit or NullRouterAuditStore()
        self._runtime = {
            item.account_id: _AccountRuntime(
                AccountState.UNKNOWN if item.enabled else AccountState.DISABLED
            )
            for item in registry.accounts
        }
        self._fallback_cursor = 0

    @property
    def account_states(self) -> dict[str, AccountState]:
        return {key: value.state for key, value in self._runtime.items()}

    def run(self, bundle: RunBundle, *, now: datetime | None = None) -> AnalystResult:
        router_started = time.monotonic()
        current = now or datetime.now(tz=UTC)
        if not self._policy.enabled:
            return AnalystResult(False, None, "CODEX_RUNTIME_DISABLED", completed_at=current)
        try:
            self._refresh_capacity(current)
        except Exception:
            return AnalystResult(
                False,
                None,
                "CODEX_AUDIT_READ_FAILED",
                completed_at=_elapsed_time(current, router_started),
            )
        attempted: set[str] = set()
        maximum_attempts = 1 + self._policy.max_account_switches
        invocation_id = stable_id(
            "codex_invocation", bundle.cycle_id, bundle.bundle_hash, current.isoformat()
        )
        for attempt_number in range(1, maximum_attempts + 1):
            account = self._select(current, attempted)
            if account is None:
                break
            attempted.add(account.account_id)
            attempt_id = stable_id("attempt", invocation_id, account.account_id, attempt_number)
            lease = self._leases.try_acquire(
                account.account_id,
                bundle.cycle_id,
                attempt_id,
                current + timedelta(seconds=self._policy.lease_ttl_seconds),
            )
            if lease is None:
                continue
            runtime = self._runtime[account.account_id]
            runtime.state = AccountState.LEASED
            runtime.last_used_at = current
            attempt_observed_at = _elapsed_time(current, router_started)
            attempt_started = time.monotonic()
            try:
                result = self._executor.execute(account, bundle)
            finally:
                self._leases.release(lease.lease_id)
            duration_ms = max(0, round((time.monotonic() - attempt_started) * 1000))
            audit = AttemptAudit(
                run_id=stable_id("codex_run", attempt_id),
                cycle_id=bundle.cycle_id,
                account_id=account.account_id,
                attempt=attempt_number,
                observed_at=attempt_observed_at,
                completed_at=_elapsed_time(current, router_started),
                duration_ms=duration_ms,
                runtime_policy_version=self._policy.version,
                status="SUCCEEDED" if result.success else "FAILED",
                failure=result.failure,
                bundle_hash=bundle.bundle_hash,
                usage=result.usage,
                diagnostics=result.diagnostics,
                analysis_behavior_hash=bundle.analysis_behavior_hash,
            )
            try:
                self._audit.record_attempt(audit)
            except Exception:
                return AnalystResult(
                    False,
                    None,
                    "CODEX_AUDIT_WRITE_FAILED",
                    account.account_id,
                    attempt_number,
                    completed_at=audit.completed_at,
                    run_id=audit.run_id,
                )
            if result.success:
                runtime.state = AccountState.HEALTHY
                runtime.recent_failures = 0
                return AnalystResult(
                    True,
                    result.output,
                    "CODEX_ANALYSIS_SUCCEEDED",
                    account.account_id,
                    attempt_number,
                    result.usage,
                    audit.completed_at,
                    audit.run_id,
                )
            failure = result.failure or FailureClass.UNAVAILABLE
            runtime.recent_failures += 1
            self._apply_failure(runtime, failure, audit.completed_at)
            if failure not in FAILOVER_FAILURES:
                return AnalystResult(
                    False,
                    None,
                    f"CODEX_{failure.value}",
                    account.account_id,
                    attempt_number,
                    completed_at=audit.completed_at,
                    run_id=audit.run_id,
                )
        return AnalystResult(
            False,
            None,
            "CODEX_ACCOUNTS_UNAVAILABLE",
            attempts=len(attempted),
            completed_at=_elapsed_time(current, router_started),
        )

    def _refresh_capacity(self, now: datetime) -> None:
        latest_attempts = self._audit.latest_account_attempts(
            tuple(
                account.account_id
                for account in self._registry.accounts
                if account.enabled
            )
        )
        for account in self._registry.accounts:
            runtime = self._runtime[account.account_id]
            if not account.enabled or runtime.state in {
                AccountState.AUTH_FAILED,
                AccountState.DISABLED,
            }:
                continue
            latest_attempt = latest_attempts.get(account.account_id)
            if (
                latest_attempt is not None
                and latest_attempt.status == "FAILED"
                and latest_attempt.failure
                in {
                    FailureClass.TIMEOUT,
                    FailureClass.PROCESS_CRASH,
                    FailureClass.ACCOUNT_UPSTREAM_TRANSIENT,
                }
            ):
                persisted_cooldown_until = latest_attempt.completed_at + timedelta(
                    seconds=self._policy.transient_failure_cooldown_seconds
                )
                if persisted_cooldown_until > now:
                    runtime.state = AccountState.COOLDOWN
                    runtime.cooldown_until = persisted_cooldown_until
                    continue
            if runtime.cooldown_until is not None and runtime.cooldown_until > now:
                continue
            if (
                runtime.snapshot is not None
                and runtime.state == AccountState.HEALTHY
                and (now - runtime.snapshot.observed_at).total_seconds()
                <= self._policy.capacity_ttl_seconds
            ):
                continue
            try:
                snapshot = self._probe.read(account)
            except (RuntimeError, ValueError):
                continue
            try:
                self._audit.record_capacity(snapshot)
            except Exception:
                continue
            runtime.snapshot = snapshot
            if snapshot.effective_headroom <= 0:
                runtime.state = AccountState.COOLDOWN
                runtime.cooldown_until = snapshot.earliest_reset or now + timedelta(minutes=1)
            else:
                runtime.state = AccountState.HEALTHY
                runtime.cooldown_until = None

    def _select(self, now: datetime, attempted: set[str]) -> CodexAccount | None:
        eligible: list[tuple[CodexAccount, _AccountRuntime]] = []
        fresh: list[tuple[CodexAccount, _AccountRuntime]] = []
        for account in self._registry.accounts:
            runtime = self._runtime[account.account_id]
            if account.account_id in attempted or not account.enabled:
                continue
            if runtime.state != AccountState.HEALTHY:
                continue
            if self._leases.has_active(account.account_id, now):
                continue
            eligible.append((account, runtime))
            if (
                runtime.snapshot is not None
                and (now - runtime.snapshot.observed_at).total_seconds()
                <= self._policy.capacity_ttl_seconds
            ):
                fresh.append((account, runtime))
        if fresh:
            ranked = sorted(
                fresh,
                key=lambda item: (
                    -(item[0].capacity_weight * item[1].snapshot.effective_headroom),
                    item[1].recent_failures,
                    item[1].last_used_at or datetime.min.replace(tzinfo=UTC),
                    item[0].account_id,
                ),
            )
            return ranked[0][0]
        if not eligible:
            return None
        ordered = sorted(eligible, key=lambda item: item[0].account_id)
        chosen = ordered[self._fallback_cursor % len(ordered)][0]
        self._fallback_cursor += 1
        return chosen

    def _apply_failure(
        self, runtime: _AccountRuntime, failure: FailureClass, now: datetime
    ) -> None:
        if failure == FailureClass.AUTH:
            runtime.state = AccountState.AUTH_FAILED
            return
        if failure == FailureClass.RATE_LIMIT:
            runtime.state = AccountState.COOLDOWN
            reset = runtime.snapshot.earliest_reset if runtime.snapshot else None
            runtime.cooldown_until = reset or now + timedelta(minutes=1)
            return
        if failure in {
            FailureClass.TIMEOUT,
            FailureClass.PROCESS_CRASH,
            FailureClass.ACCOUNT_UPSTREAM_TRANSIENT,
        }:
            runtime.state = AccountState.COOLDOWN
            runtime.cooldown_until = now + timedelta(
                seconds=self._policy.transient_failure_cooldown_seconds
            )
            return
        runtime.state = AccountState.HEALTHY


def assemble_codex_router(
    config: AppConfig,
    *,
    leases: AccountLeaseStore,
    audit: RouterAuditStore,
    output_adapter: TypeAdapter,
) -> CodexAccountRouter:
    """所有 Codex 角色共享同一白名单、额度、租约与失败切换实现。"""

    runtime = config.codex_runtime
    if not runtime.enabled or not runtime.isolation_verified:
        raise ValueError("Codex 真实运行未启用或隔离门禁未通过")
    if runtime.expected_binary_sha256 is None or runtime.binary.is_symlink():
        raise ValueError("Codex 真实运行必须绑定非符号链接的可执行制品与 SHA-256")
    if not runtime.binary.is_file() or not os.access(runtime.binary, os.X_OK):
        raise ValueError("锁定的 Codex binary 不存在或不可执行")
    enabled_accounts = [item for item in config.codex_accounts.accounts if item.enabled]
    if not enabled_accounts:
        raise ValueError("生产 Codex Router 至少需要一个已验证的白名单账号")
    for account in enabled_accounts:
        if not account.codex_home.is_dir():
            raise ValueError(f"账号目录不存在: {account.account_id}")
    return CodexAccountRouter(
        config.codex_accounts,
        runtime,
        AppServerCapacityProbe(runtime),
        SubprocessCodexExecutor(runtime, output_adapter=output_adapter),
        leases,
        audit,
    )
