from __future__ import annotations

import hashlib
import json
import os
import selectors
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, TypeAdapter, ValidationError

from investment_manager.forecast.codex.bundle import RunBundle, verify_bundle
from investment_manager.forecast.codex.output import safe_validation_diagnostics
from investment_manager.forecast.policy import CodexAccount, CodexRuntimePolicy

_ANALYST_BASE_INSTRUCTIONS = (
    "只分析用户消息中完整内嵌的冻结输入。没有执行环境或工具；"
    "禁止访问文件、网络或外部状态。只输出所要求的 JSON。"
)
_ANALYST_DEVELOPER_INSTRUCTIONS = "不得猜测缺失数据，不得调用工具，不得输出中间答案。"


def codex_execution_contract() -> dict[str, object]:
    """Stable tool-less execution boundary shared by every Codex analysis role."""

    return {
        "base_instructions": _ANALYST_BASE_INSTRUCTIONS,
        "developer_instructions": _ANALYST_DEVELOPER_INSTRUCTIONS,
        "disabled_features": _DISABLED_ANALYST_FEATURES,
    }


class FailureClass(StrEnum):
    RATE_LIMIT = "RATE_LIMIT"
    AUTH = "AUTH"
    ACCOUNT_UPSTREAM_TRANSIENT = "ACCOUNT_UPSTREAM_TRANSIENT"
    TIMEOUT = "TIMEOUT"
    PROCESS_CRASH = "PROCESS_CRASH"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    BUNDLE_INVALID = "BUNDLE_INVALID"
    MCP_FAILURE = "MCP_FAILURE"
    TOOL_PERMISSION = "TOOL_PERMISSION"
    DETERMINISTIC_VALIDATION = "DETERMINISTIC_VALIDATION"
    UNAVAILABLE = "UNAVAILABLE"


FAILOVER_FAILURES = frozenset(
    {FailureClass.RATE_LIMIT, FailureClass.AUTH, FailureClass.ACCOUNT_UPSTREAM_TRANSIENT}
)


def _minimal_codex_environment(codex_home: Path, *, rust_log: str = "error") -> dict[str, str]:
    allowed = ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "CODEX_CA_CERTIFICATE")
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    environment["CODEX_HOME"] = str(codex_home)
    environment["RUST_LOG"] = rust_log
    return environment


def _write_json_rpc(process: subprocess.Popen[str], message: dict[str, Any]) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
    process.stdin.flush()


def _read_json_rpc_until(
    process: subprocess.Popen[str],
    *,
    deadline: float,
    predicate: Any,
    observed: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise RuntimeError("Codex App Server response timed out")
            line = process.stdout.readline()
            if not line:
                raise RuntimeError("Codex App Server exited before response")
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError("Codex App Server emitted invalid JSON") from exc
            if observed is not None:
                observed.append(event)
            if predicate(event):
                return event
    finally:
        selector.close()


def _stop_app_server(process: subprocess.Popen[str]) -> str:
    if process.stdin is not None:
        process.stdin.close()
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)
    return process.stderr.read() if process.stderr is not None else ""


@dataclass(frozen=True, slots=True)
class InvocationResult:
    success: bool
    output: BaseModel | None = None
    failure: FailureClass | None = None
    usage: dict[str, int] = field(default_factory=dict)
    diagnostics: dict[str, int | str | bool] = field(default_factory=dict)


class CodexExecutor(Protocol):
    def execute(self, account: CodexAccount, bundle: RunBundle) -> InvocationResult: ...


_DISABLED_ANALYST_FEATURES = (
    "apps",
    "auth_elicitation",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
    "goals",
    "hooks",
    "image_generation",
    "js_repl",
    "multi_agent",
    "multi_agent_v2",
    "plugin_sharing",
    "plugins",
    "recommended_plugins",
    "shell_tool",
    "skill_mcp_dependency_install",
    "skill_search",
    "standalone_web_search",
    "tool_call_mcp_elicitation",
    "tool_suggest",
    "unified_exec",
    "view_image",
)
_TERMINAL_READ_REQUEST_ID = "investment-manager-terminal-read"


class SubprocessCodexExecutor:
    """通过无执行环境的本地 App Server 运行一次严格 Schema 推理。"""

    def __init__(
        self,
        policy: CodexRuntimePolicy,
        *,
        output_adapter: TypeAdapter,
    ) -> None:
        self._policy = policy
        self._output_adapter = output_adapter

    def command(self) -> list[str]:
        return [
            str(self._policy.binary),
            "app-server",
            "--stdio",
            "--strict-config",
            *(value for feature in _DISABLED_ANALYST_FEATURES for value in ("--disable", feature)),
            "-c",
            'shell_environment_policy.inherit="none"',
            "-c",
            "mcp_servers={}",
        ]

    def execute(self, account: CodexAccount, bundle: RunBundle) -> InvocationResult:
        if not verify_bundle(bundle):
            return InvocationResult(False, failure=FailureClass.BUNDLE_INVALID)
        if not codex_runtime_integrity_matches(self._policy, account.codex_home):
            return InvocationResult(False, failure=FailureClass.UNAVAILABLE)
        auth_source = account.codex_home / "auth.json"
        if not auth_source.is_file():
            return InvocationResult(False, failure=FailureClass.AUTH)
        profile_parent = bundle.path.parent / ".codex-profiles"
        try:
            profile_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            return InvocationResult(False, failure=FailureClass.PROCESS_CRASH)
        process: subprocess.Popen[str] | None = None
        events: list[dict[str, Any]] = []
        stderr = ""
        recovered_completion = False
        with tempfile.TemporaryDirectory(
            prefix=f"{account.account_id}-", dir=profile_parent
        ) as isolated_directory:
            isolated_home = Path(isolated_directory)
            try:
                (isolated_home / "auth.json").symlink_to(auth_source)
                process = subprocess.Popen(
                    self.command(),
                    text=True,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=1,
                    env=_minimal_codex_environment(isolated_home, rust_log="off"),
                )
                deadline = time.monotonic() + self._policy.timeout_seconds
                thread_id, turn_id = self._start_protocol(
                    process, bundle, deadline=deadline, observed=events
                )
                if not any(event.get("method") == "turn/completed" for event in events):
                    if not _terminal_message_is_idle(events):
                        _read_json_rpc_until(
                            process,
                            deadline=deadline,
                            predicate=lambda event: (
                                event.get("method") == "turn/completed"
                                or _terminal_message_is_idle(events)
                            ),
                            observed=events,
                        )
                    if not any(event.get("method") == "turn/completed" for event in events):
                        _wait_for_turn_completion_grace(process, events=events)
                    if not any(event.get("method") == "turn/completed" for event in events):
                        recovered_completion = _recover_completed_turn(
                            process,
                            thread_id=thread_id,
                            turn_id=turn_id,
                            events=events,
                        )
            except RuntimeError as exc:
                failure = (
                    FailureClass.TIMEOUT
                    if "timed out" in str(exc)
                    else _classify_process_failure(str(exc))
                )
                return InvocationResult(
                    False,
                    failure=failure,
                    diagnostics=_app_server_diagnostics(events),
                )
            except OSError:
                return InvocationResult(False, failure=FailureClass.PROCESS_CRASH)
            finally:
                if process is not None:
                    stderr = _stop_app_server(process)
        parsed = self._parse_app_server_events(
            events,
            stderr,
            recovered_completion=recovered_completion,
        )
        return InvocationResult(
            success=parsed.success,
            output=parsed.output,
            failure=parsed.failure,
            usage=parsed.usage,
            diagnostics={
                **parsed.diagnostics,
                "codex_cli_version": self._policy.expected_cli_version,
                "codex_binary_sha256": self._policy.expected_binary_sha256 or "MISSING",
            },
        )

    def _start_protocol(
        self,
        process: subprocess.Popen[str],
        bundle: RunBundle,
        *,
        deadline: float,
        observed: list[dict[str, Any]],
    ) -> tuple[str, str]:
        _write_json_rpc(
            process,
            {
                "method": "initialize",
                "id": 0,
                "params": {
                    "clientInfo": {
                        "name": "investment_manager",
                        "title": "Investment Manager Analyst",
                        "version": self._policy.version,
                    }
                },
            },
        )
        initialized = _read_json_rpc_until(
            process,
            deadline=deadline,
            predicate=lambda event: event.get("id") == 0,
            observed=observed,
        )
        self._require_success_response(initialized)
        _write_json_rpc(process, {"method": "initialized", "params": {}})
        _write_json_rpc(
            process,
            {
                "method": "thread/start",
                "id": 1,
                "params": {
                    "model": self._policy.model,
                    "cwd": str(bundle.path),
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "config": {
                        "features": {feature: False for feature in _DISABLED_ANALYST_FEATURES},
                        "shell_environment_policy": {"inherit": "none"},
                        "mcp_servers": {},
                    },
                    "baseInstructions": _ANALYST_BASE_INSTRUCTIONS,
                    "developerInstructions": _ANALYST_DEVELOPER_INSTRUCTIONS,
                    "ephemeral": True,
                },
            },
        )
        thread_response = _read_json_rpc_until(
            process,
            deadline=deadline,
            predicate=lambda event: event.get("id") == 1,
            observed=observed,
        )
        self._require_success_response(thread_response)
        try:
            thread_id = str(thread_response["result"]["thread"]["id"])
            output_schema = json.loads(
                (bundle.path / "output.schema.json").read_text(encoding="utf-8")
            )
        except (KeyError, OSError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Codex App Server thread contract invalid") from exc
        _write_json_rpc(
            process,
            {
                "method": "turn/start",
                "id": 2,
                "params": {
                    "threadId": thread_id,
                    "input": [
                        {
                            "type": "text",
                            "text": bundle.prompt,
                            "text_elements": [],
                        }
                    ],
                    "effort": self._policy.reasoning_effort,
                    "outputSchema": output_schema,
                },
            },
        )
        turn_response = _read_json_rpc_until(
            process,
            deadline=deadline,
            predicate=lambda event: event.get("id") == 2,
            observed=observed,
        )
        self._require_success_response(turn_response)
        try:
            turn_id = str(turn_response["result"]["turn"]["id"])
        except (KeyError, TypeError) as exc:
            raise RuntimeError("Codex App Server turn contract invalid") from exc
        return thread_id, turn_id

    @staticmethod
    def _require_success_response(event: dict[str, Any]) -> None:
        if "error" in event:
            raise RuntimeError(json.dumps(event["error"], ensure_ascii=False))

    def _parse_app_server_events(
        self,
        events: list[dict[str, Any]],
        stderr: str,
        *,
        recovered_completion: bool = False,
    ) -> InvocationResult:
        diagnostics = _app_server_diagnostics(events)
        diagnostics["completion_source"] = (
            "THREAD_READ"
            if recovered_completion
            else ("TURN_NOTIFICATION" if diagnostics["turn_completed"] else "NONE")
        )
        messages: list[str] = []
        usage: dict[str, int] = {}
        notified_completion = any(
            event.get("method") == "turn/completed"
            and event.get("params", {}).get("turn", {}).get("status") == "completed"
            and event.get("params", {}).get("turn", {}).get("error") is None
            for event in events
        )
        if stderr.strip() and not (recovered_completion or notified_completion):
            return InvocationResult(
                False,
                failure=_classify_process_failure(stderr),
                diagnostics=diagnostics,
            )
        if stderr.strip():
            # JSON-RPC owns the terminal contract.  A valid completed turn may
            # still emit a shutdown warning on stderr; retain only its presence
            # as a bounded diagnostic and continue with message/schema checks.
            diagnostics["stderr_present"] = True
        completed = recovered_completion
        for event in events:
            if event.get("method") == "error" or event.get("error") is not None:
                if event.get("id") == _TERMINAL_READ_REQUEST_ID and notified_completion:
                    continue
                failure = _classify_process_failure(json.dumps(event, ensure_ascii=False))
                diagnostics["permission_failure_stage"] = "ERROR_EVENT"
                return InvocationResult(
                    False,
                    failure=(
                        failure if failure in FAILOVER_FAILURES else FailureClass.TOOL_PERMISSION
                    ),
                    diagnostics=diagnostics,
                )
            if event.get("method") == "item/completed":
                item = event.get("params", {}).get("item", {})
                item_type = str(item.get("type", ""))
                if item_type not in {
                    "userMessage",
                    "agentMessage",
                    "reasoning",
                }:
                    diagnostics["permission_failure_stage"] = "ITEM_TYPE"
                    diagnostics["rejected_item_type"] = item_type[:64] or "MISSING"
                    return InvocationResult(
                        False,
                        failure=FailureClass.TOOL_PERMISSION,
                        diagnostics=diagnostics,
                    )
                if item_type == "agentMessage":
                    text = item.get("text")
                    if not isinstance(text, str):
                        return InvocationResult(
                            False,
                            failure=FailureClass.SCHEMA_INVALID,
                            diagnostics={
                                **diagnostics,
                                "schema_failure_stage": "AGENT_MESSAGE_NOT_TEXT",
                            },
                        )
                    messages.append(text)
            if event.get("method") == "thread/tokenUsage/updated":
                last = event.get("params", {}).get("tokenUsage", {}).get("last", {})
                usage = {
                    _camel_to_snake(key): int(value)
                    for key, value in last.items()
                    if isinstance(value, int)
                }
            if event.get("method") == "turn/completed":
                turn = event.get("params", {}).get("turn", {})
                if turn.get("status") != "completed" or turn.get("error") is not None:
                    return InvocationResult(
                        False,
                        failure=FailureClass.PROCESS_CRASH,
                        diagnostics=diagnostics,
                    )
                completed = True
        if not completed:
            return InvocationResult(
                False,
                failure=FailureClass.PROCESS_CRASH,
                diagnostics=diagnostics,
            )
        if len(messages) != 1:
            return InvocationResult(
                False,
                failure=FailureClass.SCHEMA_INVALID,
                diagnostics={
                    **diagnostics,
                    "schema_failure_stage": "AGENT_MESSAGE_COUNT",
                },
            )
        try:
            output = self._output_adapter.validate_json(messages[0])
        except ValidationError as exc:
            return InvocationResult(
                False,
                failure=FailureClass.SCHEMA_INVALID,
                diagnostics={
                    **diagnostics,
                    "schema_failure_stage": "PAYLOAD_VALIDATION",
                    **safe_validation_diagnostics(
                        exc,
                        self._output_adapter.json_schema(),
                    ),
                },
            )
        except ValueError:
            return InvocationResult(
                False,
                failure=FailureClass.SCHEMA_INVALID,
                diagnostics={
                    **diagnostics,
                    "schema_failure_stage": "PAYLOAD_DECODING",
                },
            )
        return InvocationResult(
            True,
            output=output,
            usage=usage,
            diagnostics=diagnostics,
        )


def _app_server_diagnostics(events: list[dict[str, Any]]) -> dict[str, int | str | bool]:
    """Return bounded protocol metadata without persisting model or account content."""

    labels: list[str] = []
    agent_message_count = 0
    token_usage_seen = False
    thread_status_change_count = 0
    last_thread_status = "NONE"
    safety_buffer_update_count = 0
    completed_item_types: set[str] = set()
    completed_item_count = 0
    untyped_completed_item_count = 0
    error_notification_count = 0
    non_null_rpc_error_count = 0
    for event in events:
        method = event.get("method")
        if isinstance(method, str):
            labels.append(method)
            if method == "thread/tokenUsage/updated":
                token_usage_seen = True
            if method == "thread/status/changed":
                thread_status_change_count += 1
                status = event.get("params", {}).get("status", {}).get("type")
                if status in {"notLoaded", "idle", "systemError", "active"}:
                    last_thread_status = status
                else:
                    last_thread_status = "UNKNOWN"
            if method == "model/safetyBuffering/updated":
                safety_buffer_update_count += 1
            if method == "error":
                error_notification_count += 1
            if method == "item/completed" and (
                event.get("params", {}).get("item", {}).get("type") == "agentMessage"
            ):
                agent_message_count += 1
            if method == "item/completed":
                completed_item_count += 1
                item_type = event.get("params", {}).get("item", {}).get("type")
                if isinstance(item_type, str):
                    completed_item_types.add(item_type)
                else:
                    untyped_completed_item_count += 1
            continue
        response_id = event.get("id")
        if isinstance(response_id, int):
            labels.append(f"response:{response_id}")
        if event.get("error") is not None:
            non_null_rpc_error_count += 1
    return {
        "event_count": len(events),
        "last_event": labels[-1] if labels else "NONE",
        "turn_started": "turn/started" in labels,
        "turn_completed": "turn/completed" in labels,
        "agent_message_count": agent_message_count,
        "token_usage_seen": token_usage_seen,
        "thread_status_change_count": thread_status_change_count,
        "last_thread_status": last_thread_status,
        "safety_buffer_update_count": safety_buffer_update_count,
        "completed_item_types": ",".join(sorted(completed_item_types)) or "NONE",
        "completed_item_count": completed_item_count,
        "untyped_completed_item_count": untyped_completed_item_count,
        "error_notification_count": error_notification_count,
        "non_null_rpc_error_count": non_null_rpc_error_count,
    }


def _terminal_message_is_idle(events: list[dict[str, Any]]) -> bool:
    if any(
        event.get("method") in {"turn/completed", "error"} or event.get("error") is not None
        for event in events
    ):
        return False
    has_message = any(
        event.get("method") == "item/completed"
        and event.get("params", {}).get("item", {}).get("type") == "agentMessage"
        for event in events
    )
    statuses = [
        event.get("params", {}).get("status", {}).get("type")
        for event in events
        if event.get("method") == "thread/status/changed"
    ]
    return has_message and bool(statuses) and statuses[-1] == "idle"


def _recover_completed_turn(
    process: subprocess.Popen[str],
    *,
    thread_id: str,
    turn_id: str,
    events: list[dict[str, Any]],
) -> bool:
    """Read authoritative terminal state when the completion notification is absent."""

    request_id = _TERMINAL_READ_REQUEST_ID
    _write_json_rpc(
        process,
        {
            "method": "thread/read",
            "id": request_id,
            "params": {"threadId": thread_id, "includeTurns": True},
        },
    )
    response = _read_json_rpc_until(
        process,
        deadline=time.monotonic() + 3,
        predicate=lambda event: event.get("id") == request_id,
        observed=events,
    )
    if response.get("error") is not None:
        return False
    thread = response.get("result", {}).get("thread", {})
    if (
        thread.get("id") != thread_id
        or thread.get("status", {}).get("type") != "idle"
        or not isinstance(thread.get("turns"), list)
    ):
        return False
    turns = [item for item in thread["turns"] if item.get("id") == turn_id]
    if len(turns) != 1:
        return False
    turn = turns[0]
    items = turn.get("items")
    if (
        turn.get("status") != "completed"
        or turn.get("error") is not None
        or turn.get("itemsView") != "full"
        or turn.get("completedAt") is None
        or not isinstance(items, list)
        or any(
            item.get("type") not in {"userMessage", "agentMessage", "reasoning"} for item in items
        )
    ):
        return False
    observed_messages = [
        event.get("params", {}).get("item", {}).get("text")
        for event in events
        if event.get("method") == "item/completed"
        and event.get("params", {}).get("item", {}).get("type") == "agentMessage"
    ]
    recovered_messages = [item.get("text") for item in items if item.get("type") == "agentMessage"]
    return (
        len(observed_messages) == 1
        and len(recovered_messages) == 1
        and recovered_messages == observed_messages
    )


def _wait_for_turn_completion_grace(
    process: subprocess.Popen[str],
    *,
    events: list[dict[str, Any]],
) -> None:
    """Allow an imminent completion notification before issuing a recovery read."""

    try:
        _read_json_rpc_until(
            process,
            deadline=time.monotonic() + 0.25,
            predicate=lambda event: event.get("method") == "turn/completed",
            observed=events,
        )
    except RuntimeError as exc:
        if "timed out" not in str(exc):
            raise


def _camel_to_snake(value: str) -> str:
    characters: list[str] = []
    for character in value:
        if character.isupper():
            characters.extend(("_", character.lower()))
        else:
            characters.append(character)
    return "".join(characters)


def _classify_process_failure(message: str) -> FailureClass:
    lowered = message.lower()
    if any(value in lowered for value in ("rate limit", "usage limit", "quota exceeded", "429")):
        return FailureClass.RATE_LIMIT
    if any(
        value in lowered for value in ("unauthorized", "authentication", "login required", "401")
    ):
        return FailureClass.AUTH
    if any(value in lowered for value in ("upstream", "service unavailable", "502", "503")):
        return FailureClass.ACCOUNT_UPSTREAM_TRANSIENT
    if "mcp" in lowered:
        return FailureClass.MCP_FAILURE
    if "permission" in lowered or "sandbox" in lowered:
        return FailureClass.TOOL_PERMISSION
    return FailureClass.PROCESS_CRASH


def codex_runtime_integrity_matches(
    policy: CodexRuntimePolicy,
    codex_home: Path | None = None,
) -> bool:
    """Verify the exact executable artifact and version immediately before use."""

    expected_digest = policy.expected_binary_sha256
    if expected_digest is None or policy.binary.is_symlink():
        return False
    try:
        binary = policy.binary.resolve(strict=True)
        if not binary.is_file() or not os.access(binary, os.X_OK):
            return False
        digest = hashlib.sha256()
        with binary.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected_digest:
            return False
        completed = subprocess.run(
            [str(binary), "--version"],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
            env=(_minimal_codex_environment(codex_home) if codex_home is not None else None),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and completed.stdout.strip() == policy.expected_cli_version
