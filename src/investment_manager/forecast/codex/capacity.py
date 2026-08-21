from __future__ import annotations

import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from investment_manager.forecast.codex.protocol import (
    _minimal_codex_environment,
    _read_json_rpc_until,
    _stop_app_server,
    _write_json_rpc,
    codex_runtime_integrity_matches,
)
from investment_manager.forecast.policy import CodexAccount, CodexRuntimePolicy


@dataclass(frozen=True, slots=True)
class CapacityWindow:
    used_percent: Decimal
    window_duration_minutes: int
    resets_at: datetime

    @property
    def headroom(self) -> Decimal:
        return max(Decimal("0"), Decimal("100") - self.used_percent)


@dataclass(frozen=True, slots=True)
class CapacityBucket:
    limit_id: str
    primary: CapacityWindow | None
    secondary: CapacityWindow | None
    reached_type: str | None


@dataclass(frozen=True, slots=True)
class CapacitySnapshot:
    account_id: str
    observed_at: datetime
    buckets: tuple[CapacityBucket, ...]

    @property
    def effective_headroom(self) -> Decimal:
        windows = [
            window.headroom
            for bucket in self.buckets
            for window in (bucket.primary, bucket.secondary)
            if window is not None
        ]
        if any(bucket.reached_type for bucket in self.buckets):
            return Decimal("0")
        return min(windows) if windows else Decimal("0")

    @property
    def earliest_reset(self) -> datetime | None:
        resets = [
            window.resets_at
            for bucket in self.buckets
            for window in (bucket.primary, bucket.secondary)
            if window is not None
        ]
        return min(resets) if resets else None


class CapacityProbe(Protocol):
    def read(self, account: CodexAccount) -> CapacitySnapshot: ...


class AppServerCapacityProbe:
    """只通过官方 App Server 协议读取额度，不接触 auth.json。"""

    def __init__(self, policy: CodexRuntimePolicy) -> None:
        self._policy = policy

    def read(self, account: CodexAccount) -> CapacitySnapshot:
        if not codex_runtime_integrity_matches(self._policy, account.codex_home):
            raise RuntimeError("Codex App Server runtime artifact mismatch")
        initialize = {
            "method": "initialize",
            "id": 0,
            "params": {
                "clientInfo": {
                    "name": "investment_manager",
                    "title": "Investment Manager Capacity Probe",
                    "version": "0.1.0",
                }
            },
        }
        auth_source = account.codex_home / "auth.json"
        if not auth_source.is_file():
            raise RuntimeError("Codex App Server capacity probe unavailable")
        profile_parent = account.codex_home.parent / ".investment-manager-capacity-profiles"
        try:
            profile_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeError("Codex App Server capacity probe unavailable") from exc
        with tempfile.TemporaryDirectory(
            prefix=f"{account.account_id}-", dir=profile_parent
        ) as isolated_directory:
            isolated_home = Path(isolated_directory)
            process: subprocess.Popen[str] | None = None
            try:
                (isolated_home / "auth.json").symlink_to(auth_source)
                process = subprocess.Popen(
                    [str(self._policy.binary), "app-server", "--stdio", "--strict-config"],
                    text=True,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=1,
                    env=_minimal_codex_environment(isolated_home),
                )
                _write_json_rpc(process, initialize)
                initialized = self._read_response(process, response_id=0)
                if "error" in initialized:
                    raise RuntimeError("Codex App Server initialize failed")
                _write_json_rpc(process, {"method": "initialized", "params": {}})
                _write_json_rpc(
                    process,
                    {"method": "account/rateLimits/read", "id": 2, "params": None},
                )
                response = self._read_response(process, response_id=2)
            except (OSError, RuntimeError) as exc:
                raise RuntimeError("Codex App Server capacity probe unavailable") from exc
            finally:
                if process is not None:
                    _stop_app_server(process)
        if "error" in response:
            raise RuntimeError("Codex App Server capacity contract failed")
        return _capacity_snapshot(account.account_id, response["result"], datetime.now(tz=UTC))

    def _read_response(self, process: subprocess.Popen[str], *, response_id: int) -> dict[str, Any]:
        return _read_json_rpc_until(
            process,
            deadline=time.monotonic() + self._policy.capacity_probe_timeout_seconds,
            predicate=lambda event: event.get("id") == response_id,
        )


def _capacity_snapshot(
    account_id: str, result: dict[str, Any], observed_at: datetime
) -> CapacitySnapshot:
    raw_buckets = result.get("rateLimitsByLimitId")
    if raw_buckets:
        values = [raw_buckets[key] for key in sorted(raw_buckets)]
    elif result.get("rateLimits"):
        values = [result["rateLimits"]]
    else:
        raise ValueError("额度响应不包含 rateLimits")

    def window(raw: dict[str, Any] | None) -> CapacityWindow | None:
        if raw is None:
            return None
        return CapacityWindow(
            used_percent=Decimal(str(raw["usedPercent"])),
            window_duration_minutes=int(raw["windowDurationMins"]),
            resets_at=datetime.fromtimestamp(int(raw["resetsAt"]), tz=UTC),
        )

    buckets = tuple(
        CapacityBucket(
            limit_id=str(item["limitId"]),
            primary=window(item.get("primary")),
            secondary=window(item.get("secondary")),
            reached_type=item.get("rateLimitReachedType"),
        )
        for item in values
    )
    return CapacitySnapshot(account_id=account_id, observed_at=observed_at, buckets=buckets)
