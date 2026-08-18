from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from quant_core.analyst import (
    AccountState,
    AnalystResult,
    AppServerCapacityProbe,
    CapacityBucket,
    CapacitySnapshot,
    CapacityWindow,
    CodexAccountRouter,
    FailureClass,
    InvocationResult,
    ProposalNormalizer,
    RunBundle,
    RunBundleBuilder,
    SubprocessCodexExecutor,
    _capacity_snapshot,
    strict_output_schema,
    verify_bundle,
)
from quant_core.config import CodexAccount, CodexAccountRegistry
from quant_core.domain import Action, AnalysisProposal, OrderType, PriceCondition, Side


def _account_registry(tmp_path: Path) -> CodexAccountRegistry:
    accounts = []
    for alias in ("codex_a", "codex_b", "codex_c"):
        home = tmp_path / alias
        home.mkdir()
        accounts.append(
            CodexAccount(
                account_id=alias,
                codex_home=home,
                enabled=True,
                capacity_weight=Decimal("1"),
                max_concurrency=1,
            )
        )
    return CodexAccountRegistry(version="test-registry-v1", accounts=tuple(accounts))


def _runtime(app_config):
    return app_config.codex_runtime.model_copy(
        update={"enabled": True, "isolation_verified": True, "timeout_seconds": 10}
    )


def _proposal(replay_input, *, action: Action = Action.OPEN) -> AnalysisProposal:
    if action == Action.NO_ACTION:
        return AnalysisProposal(
            proposal_id="proposal_no_action",
            suggested_action=action,
            symbol=replay_input.market.symbol,
            thesis="当前没有足够优势",
            confidence=Decimal("0.60"),
        )
    return AnalysisProposal(
        proposal_id="proposal_open",
        suggested_action=action,
        symbol=replay_input.market.symbol,
        side=Side.BUY,
        horizon_minutes=60,
        thesis="趋势与信息方向一致，跌破失效位即证伪",
        evidence_ids=(),
        entry_condition=PriceCondition(order_type=OrderType.MARKET),
        invalidation_price=Decimal("99"),
        valid_until=replay_input.market.as_of + timedelta(minutes=10),
        confidence=Decimal("0.63"),
    )


def _snapshot(account_id: str, now: datetime, used: str) -> CapacitySnapshot:
    return CapacitySnapshot(
        account_id=account_id,
        observed_at=now,
        buckets=(
            CapacityBucket(
                limit_id="codex",
                primary=CapacityWindow(
                    used_percent=Decimal(used),
                    window_duration_minutes=15,
                    resets_at=now + timedelta(minutes=15),
                ),
                secondary=None,
                reached_type=None,
            ),
        ),
    )


@dataclass
class FakeProbe:
    snapshots: dict[str, CapacitySnapshot]

    def read(self, account: CodexAccount) -> CapacitySnapshot:
        return self.snapshots[account.account_id]


@dataclass
class SwitchableProbe:
    snapshots: dict[str, CapacitySnapshot]
    failing: bool = False

    def read(self, account: CodexAccount) -> CapacitySnapshot:
        if self.failing:
            raise RuntimeError("probe unavailable")
        return self.snapshots[account.account_id]


@dataclass
class FakeExecutor:
    results: dict[str, list[InvocationResult]]
    calls: list[tuple[str, str, Path]] = field(default_factory=list)

    def execute(self, account: CodexAccount, bundle: RunBundle) -> InvocationResult:
        self.calls.append((account.account_id, bundle.bundle_hash, bundle.path))
        return self.results[account.account_id].pop(0)


def test_analysis_proposal_is_strict_and_cannot_smuggle_position_fields(replay_input) -> None:
    payload = _proposal(replay_input).model_dump(mode="json")
    payload["quantity"] = "10"

    with pytest.raises(ValidationError):
        AnalysisProposal.model_validate(payload)


def test_run_bundle_is_hashed_read_only_and_detects_tampering(
    app_config, replay_input, tmp_path
) -> None:
    from quant_core.features import FeatureEngine
    from quant_core.panel import PanelBuilder

    panel = PanelBuilder(app_config.panel).build(
        market=replay_input.market,
        account=replay_input.account,
        features=FeatureEngine(app_config.feature).compute(replay_input.market),
        events=replay_input.events,
    )
    target = tmp_path / "bundle"
    bundle = RunBundleBuilder(_runtime(app_config), app_config.proposal).build(panel, target)

    assert verify_bundle(bundle)
    assert {item.name for item in target.iterdir()} == {
        "panel.json",
        "panel.md",
        "policy_digest.md",
        "analyst_prompt.md",
        "output.schema.json",
        "manifest.json",
    }
    assert target.stat().st_mode & 0o222 == 0
    assert '"cycle_id":"cycle-replay-001"' in bundle.prompt
    assert "禁止调用任何工具" in bundle.prompt
    (target / "panel.md").chmod(0o644)
    (target / "panel.md").write_text("tampered", encoding="utf-8")
    assert not verify_bundle(bundle)


def test_codex_output_schema_requires_every_property_and_uses_null_for_optional() -> None:
    schema = strict_output_schema(AnalysisProposal.model_json_schema())

    assert schema["required"] == list(schema["properties"])
    price_condition = schema["$defs"]["PriceCondition"]
    assert price_condition["required"] == list(price_condition["properties"])
    assert "default" not in schema["properties"]["side"]
    assert "pattern" not in price_condition["properties"]["price"]["anyOf"][1]
    assert {item.get("type") for item in schema["properties"]["side"]["anyOf"]} >= {"null"}


def test_capacity_uses_most_constrained_window_and_bucket() -> None:
    now = datetime(2026, 8, 18, tzinfo=UTC)
    result = {
        "rateLimitsByLimitId": {
            "codex": {
                "limitId": "codex",
                "primary": {
                    "usedPercent": 25,
                    "windowDurationMins": 15,
                    "resetsAt": int((now + timedelta(minutes=15)).timestamp()),
                },
                "secondary": {
                    "usedPercent": 70,
                    "windowDurationMins": 10080,
                    "resetsAt": int((now + timedelta(days=7)).timestamp()),
                },
                "rateLimitReachedType": None,
            },
            "codex_other": {
                "limitId": "codex_other",
                "primary": {
                    "usedPercent": 85,
                    "windowDurationMins": 60,
                    "resetsAt": int((now + timedelta(hours=1)).timestamp()),
                },
                "secondary": None,
                "rateLimitReachedType": None,
            },
        }
    }

    snapshot = _capacity_snapshot("codex_a", result, now)

    assert snapshot.effective_headroom == Decimal("15")


def test_app_server_probe_uses_official_handshake_and_persists_no_identity_fields(
    app_config, tmp_path, monkeypatch
) -> None:
    registry = _account_registry(tmp_path)
    now = datetime.now(tz=UTC)
    response = {
        "id": 2,
        "result": {
            "email": "must-not-be-retained@example.com",
            "rateLimits": {
                "limitId": "codex",
                "primary": {
                    "usedPercent": 30,
                    "windowDurationMins": 15,
                    "resetsAt": int((now + timedelta(minutes=15)).timestamp()),
                },
                "secondary": None,
                "rateLimitReachedType": None,
            },
        },
    }
    captured = {}

    class FakeStream:
        def __init__(self, lines=()):
            self._lines = iter(lines)
            self.written = ""

        def write(self, value):
            self.written += value

        def flush(self):
            return None

        def readline(self):
            return next(self._lines, "")

        def close(self):
            return None

        def fileno(self):
            return 0

    class FakeProcess:
        def __init__(self, command, **kwargs):
            captured["command"] = command
            captured["env"] = kwargs["env"]
            self.stdin = FakeStream()
            self.stdout = FakeStream(
                (
                    json.dumps({"id": 0, "result": {"userAgent": "test"}}) + "\n",
                    json.dumps(response) + "\n",
                )
            )
            self.stderr = FakeStream()
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            return self.returncode

    class FakeSelector:
        def register(self, stream, events):
            return None

        def select(self, timeout=None):
            return [(object(), object())]

        def close(self):
            return None

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        process = FakeProcess(command, **kwargs)
        captured["process"] = process
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr("quant_core.analyst.selectors.DefaultSelector", FakeSelector)

    snapshot = AppServerCapacityProbe(_runtime(app_config)).read(registry.accounts[0])

    sent = [json.loads(line) for line in captured["process"].stdin.written.splitlines()]
    assert [item["method"] for item in sent] == [
        "initialize",
        "initialized",
        "account/rateLimits/read",
    ]
    assert captured["command"] == ["/usr/bin/codex", "app-server", "--stdio", "--strict-config"]
    assert snapshot.account_id == "codex_a"
    assert snapshot.effective_headroom == Decimal("70")
    assert not hasattr(snapshot, "email")


def test_router_chooses_most_headroom_without_discovering_fourth_directory(
    app_config, replay_input, tmp_path
) -> None:
    registry = _account_registry(tmp_path)
    (tmp_path / "codex_unapproved_fourth").mkdir()
    now = datetime(2026, 8, 18, tzinfo=UTC)
    proposal = _proposal(replay_input)
    executor = FakeExecutor(
        {item.account_id: [InvocationResult(True, proposal=proposal)] for item in registry.accounts}
    )
    router = CodexAccountRouter(
        registry,
        _runtime(app_config),
        FakeProbe(
            {
                "codex_a": _snapshot("codex_a", now, "40"),
                "codex_b": _snapshot("codex_b", now, "10"),
                "codex_c": _snapshot("codex_c", now, "75"),
            }
        ),
        executor,
    )
    bundle = RunBundle("cycle", tmp_path, "hash", "prompt")

    result = router.run(bundle, now=now)

    assert result.success
    assert result.account_id == "codex_b"
    assert [item[0] for item in executor.calls] == ["codex_b"]
    assert "codex_unapproved_fourth" not in router.account_states


def test_rate_limit_failover_restarts_same_immutable_bundle(
    app_config, replay_input, tmp_path
) -> None:
    registry = _account_registry(tmp_path)
    now = datetime(2026, 8, 18, tzinfo=UTC)
    executor = FakeExecutor(
        {
            "codex_a": [InvocationResult(False, failure=FailureClass.RATE_LIMIT)],
            "codex_b": [InvocationResult(True, proposal=_proposal(replay_input))],
            "codex_c": [InvocationResult(True, proposal=_proposal(replay_input))],
        }
    )
    router = CodexAccountRouter(
        registry,
        _runtime(app_config),
        FakeProbe(
            {
                "codex_a": _snapshot("codex_a", now, "1"),
                "codex_b": _snapshot("codex_b", now, "10"),
                "codex_c": _snapshot("codex_c", now, "20"),
            }
        ),
        executor,
    )
    bundle = RunBundle("cycle", tmp_path, "immutable-hash", "prompt")

    result = router.run(bundle, now=now)

    assert result.success
    assert result.account_id == "codex_b"
    assert [call[0] for call in executor.calls] == ["codex_a", "codex_b"]
    assert {call[1] for call in executor.calls} == {"immutable-hash"}
    assert {call[2] for call in executor.calls} == {tmp_path}
    assert router.account_states["codex_a"] == AccountState.COOLDOWN


def test_auth_failure_disables_account_for_current_router_and_fails_over(
    app_config, replay_input, tmp_path
) -> None:
    registry = _account_registry(tmp_path)
    now = datetime(2026, 8, 18, tzinfo=UTC)
    executor = FakeExecutor(
        {
            "codex_a": [InvocationResult(False, failure=FailureClass.AUTH)],
            "codex_b": [InvocationResult(True, proposal=_proposal(replay_input))],
            "codex_c": [InvocationResult(True, proposal=_proposal(replay_input))],
        }
    )
    router = CodexAccountRouter(
        registry,
        _runtime(app_config),
        FakeProbe(
            {
                "codex_a": _snapshot("codex_a", now, "1"),
                "codex_b": _snapshot("codex_b", now, "10"),
                "codex_c": _snapshot("codex_c", now, "20"),
            }
        ),
        executor,
    )

    result = router.run(RunBundle("cycle", tmp_path, "hash", "prompt"), now=now)

    assert result.success
    assert result.account_id == "codex_b"
    assert router.account_states["codex_a"] == AccountState.AUTH_FAILED


def test_schema_failure_never_burns_other_accounts(app_config, tmp_path) -> None:
    registry = _account_registry(tmp_path)
    now = datetime(2026, 8, 18, tzinfo=UTC)
    executor = FakeExecutor(
        {
            "codex_a": [InvocationResult(False, failure=FailureClass.SCHEMA_INVALID)],
            "codex_b": [InvocationResult(False, failure=FailureClass.SCHEMA_INVALID)],
            "codex_c": [InvocationResult(False, failure=FailureClass.SCHEMA_INVALID)],
        }
    )
    router = CodexAccountRouter(
        registry,
        _runtime(app_config),
        FakeProbe(
            {item.account_id: _snapshot(item.account_id, now, "10") for item in registry.accounts}
        ),
        executor,
    )

    result = router.run(RunBundle("cycle", tmp_path, "hash", "prompt"), now=now)

    assert not result.success
    assert result.reason_code == "CODEX_SCHEMA_INVALID"
    assert len(executor.calls) == 1


def test_timeout_never_rotates_accounts(app_config, tmp_path) -> None:
    registry = _account_registry(tmp_path)
    now = datetime(2026, 8, 18, tzinfo=UTC)
    executor = FakeExecutor(
        {
            "codex_a": [InvocationResult(False, failure=FailureClass.TIMEOUT)],
            "codex_b": [InvocationResult(False, failure=FailureClass.TIMEOUT)],
            "codex_c": [InvocationResult(False, failure=FailureClass.TIMEOUT)],
        }
    )
    router = CodexAccountRouter(
        registry,
        _runtime(app_config),
        FakeProbe(
            {item.account_id: _snapshot(item.account_id, now, "10") for item in registry.accounts}
        ),
        executor,
    )

    result = router.run(RunBundle("cycle", tmp_path, "hash", "prompt"), now=now)

    assert not result.success
    assert result.reason_code == "CODEX_TIMEOUT"
    assert len(executor.calls) == 1


def test_probe_outage_uses_only_previously_healthy_accounts_in_conservative_round_robin(
    app_config, replay_input, tmp_path
) -> None:
    registry = _account_registry(tmp_path)
    now = datetime(2026, 8, 18, tzinfo=UTC)
    snapshots = {
        item.account_id: _snapshot(item.account_id, now, "10") for item in registry.accounts
    }
    probe = SwitchableProbe(snapshots)
    executor = FakeExecutor(
        {
            item.account_id: [
                InvocationResult(True, proposal=_proposal(replay_input)),
                InvocationResult(True, proposal=_proposal(replay_input)),
            ]
            for item in registry.accounts
        }
    )
    router = CodexAccountRouter(registry, _runtime(app_config), probe, executor)

    first = router.run(RunBundle("cycle-1", tmp_path, "hash-1", "prompt"), now=now)
    probe.failing = True
    second = router.run(
        RunBundle("cycle-2", tmp_path, "hash-2", "prompt"),
        now=now + timedelta(seconds=61),
    )

    assert first.success and second.success
    assert first.account_id == "codex_a"
    assert second.account_id == "codex_a"


def test_initial_probe_outage_fails_closed_without_guessing_account_health(
    app_config, tmp_path
) -> None:
    registry = _account_registry(tmp_path)
    now = datetime(2026, 8, 18, tzinfo=UTC)
    probe = SwitchableProbe({}, failing=True)
    executor = FakeExecutor({item.account_id: [] for item in registry.accounts})
    router = CodexAccountRouter(registry, _runtime(app_config), probe, executor)

    result = router.run(RunBundle("cycle", tmp_path, "hash", "prompt"), now=now)

    assert not result.success
    assert result.reason_code == "CODEX_ACCOUNTS_UNAVAILABLE"
    assert not executor.calls


def test_subprocess_contract_uses_selected_home_and_clears_credential_overrides(
    app_config, replay_input, tmp_path, monkeypatch
) -> None:
    from quant_core.features import FeatureEngine
    from quant_core.panel import PanelBuilder

    registry = _account_registry(tmp_path)
    panel = PanelBuilder(app_config.panel).build(
        market=replay_input.market,
        account=replay_input.account,
        features=FeatureEngine(app_config.feature).compute(replay_input.market),
        events=replay_input.events,
    )
    bundle = RunBundleBuilder(_runtime(app_config), app_config.proposal).build(
        panel, tmp_path / "bundle"
    )
    proposal = _proposal(replay_input)
    stdout = "\n".join(
        (
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "message",
                        "type": "agent_message",
                        "text": proposal.model_dump_json(),
                    },
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}),
        )
    )
    captured = {}

    def fake_run(command, **kwargs):
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "codex-cli 0.147.0\n", "")
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("CODEX_API_KEY", "must-not-leak")
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "must-not-leak")
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = SubprocessCodexExecutor(_runtime(app_config)).execute(registry.accounts[1], bundle)

    assert result.success
    assert captured["env"]["CODEX_HOME"] == str(registry.accounts[1].codex_home)
    assert not {"OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"} & captured["env"].keys()
    command = captured["command"]
    assert command[:2] == ["/usr/bin/codex", "exec"]
    assert "--ephemeral" in command
    assert "--ignore-user-config" in command
    assert "--strict-config" in command
    assert "--skip-git-repo-check" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[-2:] == ["--json", "-"]


def test_proposal_normalizer_validates_evidence_and_never_sizes_position(
    app_config, replay_input
) -> None:
    from quant_core.features import FeatureEngine
    from quant_core.panel import PanelBuilder

    panel = PanelBuilder(app_config.panel).build(
        market=replay_input.market,
        account=replay_input.account,
        features=FeatureEngine(app_config.feature).compute(replay_input.market),
        events=replay_input.events,
    )
    normalizer = ProposalNormalizer(app_config.proposal)

    candidates = normalizer.normalize(_proposal(replay_input), panel)

    assert len(candidates) == 1
    assert candidates[0].producer_id == "codex-analyst"
    assert not hasattr(candidates[0], "quantity")

    bad = _proposal(replay_input).model_copy(update={"evidence_ids": ("missing",)})
    with pytest.raises(ValueError, match="evidence_id"):
        normalizer.normalize(bad, panel)


@dataclass
class AlwaysFailAnalyst:
    def analyze(self, panel) -> AnalystResult:
        return AnalystResult(False, None, "CODEX_SCHEMA_INVALID")
