from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from investment_manager.execution.models import (
    OrderType,
    Side,
)
from investment_manager.forecast.codex import (
    AccountState,
    AnalystResult,
    AppServerCapacityProbe,
    CapacityBucket,
    CapacitySnapshot,
    CapacityWindow,
    CodexAccountRouter,
    FailureClass,
    InMemoryAccountLeaseStore,
    InvocationResult,
    IsolationProbeOutput,
    RunBundle,
    SubprocessCodexExecutor,
    _capacity_snapshot,
    _recover_completed_turn,
    _terminal_message_is_idle,
    assemble_codex_router,
    audit_codex_isolation,
    strict_output_schema,
    verify_bundle,
)
from investment_manager.forecast.models import DirectionalView
from investment_manager.forecast.policy import CodexAccount, CodexAccountRegistry
from investment_manager.legacy.analyst import (
    ANALYST_INPUT_VERSION,
    AnalystStructuredOutput,
    ProposalNormalizer,
    RunBundleBuilder,
    analysis_behavior_hash,
)
from investment_manager.legacy.models import (
    Action,
    AnalysisProposal,
    DirectionalForecast,
    PriceCondition,
)
from investment_manager.scheduling.models import TriggerDecision, TriggerReason
from investment_manager.settings import AppConfig


def _account_registry(tmp_path: Path) -> CodexAccountRegistry:
    accounts = []
    for alias in ("codex_a", "codex_b", "codex_c"):
        home = tmp_path / alias
        home.mkdir()
        (home / "auth.json").write_text("{}\n", encoding="utf-8")
        accounts.append(
            CodexAccount(
                account_id=alias,
                codex_home=home,
                enabled=True,
                capacity_weight=Decimal("1"),
            )
        )
    return CodexAccountRegistry(version="test-registry-v1", accounts=tuple(accounts))


def _runtime(app_config):
    return app_config.codex_runtime.model_copy(
        update={
            "enabled": True,
            "isolation_verified": True,
            "timeout_seconds": 10,
            "expected_binary_sha256": "0" * 64,
        }
    )


def _proposal_executor(app_config) -> SubprocessCodexExecutor:
    return SubprocessCodexExecutor(
        _runtime(app_config),
        output_adapter=TypeAdapter(AnalystStructuredOutput),
    )


def test_analysis_behavior_identity_ignores_runtime_generation_and_downstream_calibration(
    app_config, base_app_config, monkeypatch
) -> None:
    baseline = analysis_behavior_hash(base_app_config)
    redeployed = app_config.model_copy(
        update={
            "pipeline": app_config.pipeline.model_copy(
                update={"version": "another-runtime-generation"}
            ),
            "calibration": app_config.calibration.model_copy(
                update={"version": "published-calibration-v2"}
            ),
        }
    )
    changed_behavior = app_config.model_copy(
        update={
            "proposal": app_config.proposal.model_copy(
                update={"minimum_confidence": Decimal("0.91")}
            )
        }
    )

    assert app_config.calibration.artifacts
    assert analysis_behavior_hash(redeployed) == baseline
    assert analysis_behavior_hash(changed_behavior) != baseline
    monkeypatch.setattr(
        "investment_manager.legacy.analyst._ANALYST_PROMPT_INSTRUCTIONS",
        "different semantic prompt contract",
    )
    assert analysis_behavior_hash(app_config) != baseline


def _proposal(replay_input, *, action: Action = Action.OPEN) -> AnalysisProposal:
    forecasts = (
        DirectionalForecast(
            horizon_minutes=60,
            directional_view=(
                DirectionalView.UNCERTAIN
                if action == Action.NO_ACTION
                else DirectionalView.UP
            ),
            confidence=Decimal("0.60"),
        ),
        DirectionalForecast(
            horizon_minutes=240,
            directional_view=DirectionalView.UNCERTAIN,
            confidence=Decimal("0.55"),
        ),
    )
    if action == Action.NO_ACTION:
        return AnalysisProposal(
            proposal_id="proposal_no_action",
            suggested_action=action,
            symbol=replay_input.market.symbol,
            thesis="当前没有足够优势",
            confidence=Decimal("0.60"),
            forecasts=forecasts,
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
        forecasts=forecasts,
    )


def test_propose_worker_concurrency_cannot_exceed_enabled_accounts(base_app_config) -> None:
    raw = base_app_config.model_dump(mode="python")
    raw["pipeline"] = {**raw["pipeline"], "ai_mode": "PROPOSE"}
    raw["temporal"] = {**raw["temporal"], "worker_threads": 2}
    accounts = list(raw["codex_accounts"]["accounts"])
    accounts[1] = {**accounts[1], "enabled": True}
    raw["codex_accounts"] = {**raw["codex_accounts"], "accounts": accounts}

    with pytest.raises(ValueError, match="分析并发不得超过"):
        AppConfig.model_validate(raw)

    raw["temporal"] = {**raw["temporal"], "worker_threads": 1}
    assert AppConfig.model_validate(raw).temporal.worker_threads == 1


def test_account_identity_matches_directory_and_registry_is_extensible(tmp_path) -> None:
    homes = tuple(
        tmp_path / name for name in (".codex", ".codex2", ".codex_affine_pb", ".codex_dlz")
    )
    accounts = tuple(
        CodexAccount(account_id=home.name, codex_home=home, enabled=False) for home in homes
    )

    registry = CodexAccountRegistry(version="directory-registry-v1", accounts=accounts)

    assert tuple(item.account_id for item in registry.accounts) == tuple(
        home.name for home in homes
    )
    with pytest.raises(ValidationError, match="必须与 codex_home 目录名一致"):
        CodexAccount(account_id="codex_a", codex_home=homes[0], enabled=False)


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
    calls: list[str] = field(default_factory=list)

    def read(self, account: CodexAccount) -> CapacitySnapshot:
        self.calls.append(account.account_id)
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
    from investment_manager.market.features import FeatureEngine
    from investment_manager.state.panel import PanelBuilder

    duplicate_body = replay_input.events[0].model_copy(
        update={"body": replay_input.events[0].title}
    )
    panel = PanelBuilder(app_config.panel).build(
        market=replay_input.market,
        account=replay_input.account,
        features=FeatureEngine(app_config.feature).compute(replay_input.market),
        events=(duplicate_body, *replay_input.events[1:]),
    )
    target = tmp_path / "bundle"
    trigger = TriggerDecision(
        should_run=True,
        reason=TriggerReason.EVENT_BATCH,
        evidence_ids=(panel.evidence[0].evidence_id, "evidence-not-selected"),
    )
    bundle = RunBundleBuilder(
        _runtime(app_config),
        app_config.proposal,
        code_version="release-commit-v1",
        configuration_hash="a" * 64,
        analysis_behavior_hash="b" * 64,
    ).build(panel, target, trigger=trigger)

    assert verify_bundle(bundle)
    assert {item.name for item in target.iterdir()} == {
        "panel.json",
        "analyst_prompt.md",
        "output.schema.json",
        "manifest.json",
    }
    assert target.stat().st_mode & 0o222 == 0
    assert json.loads((target / "manifest.json").read_text())["code_version"] == (
        "release-commit-v1"
    )
    assert json.loads((target / "manifest.json").read_text())[
        "configuration_hash"
    ] == "a" * 64
    assert bundle.analysis_behavior_hash == "b" * 64
    assert json.loads((target / "manifest.json").read_text())[
        "analysis_behavior_hash"
    ] == "b" * 64
    assert '"cycle_id":"cycle-replay-001"' in bundle.prompt
    assert "禁止调用任何工具" in bundle.prompt
    assert "必须遵守 panel_view_json.rules_digest" in bundle.prompt
    assert "OPEN 的 side 只能为 BUY" in bundle.prompt
    assert "可交易方向只约束 suggested_action 和 side" in bundle.prompt
    assert "当前不能做空，预期价格下跌时也必须输出 DOWN" in bundle.prompt
    assert f'"analyst_input_version":"{ANALYST_INPUT_VERSION}"' in bundle.prompt
    prompt_view = bundle.prompt.split("<panel_view_json>\n", 1)[1].split("\n</panel_view_json>", 1)[
        0
    ]
    prompt_payload = json.loads(prompt_view)
    assert "bars" not in prompt_payload["market"]
    assert "cycle_id" not in prompt_payload["market"]
    assert "as_of" not in prompt_payload["market"]
    assert "cycle_id" not in prompt_payload["account"]
    assert "as_of" not in prompt_payload["account"]
    assert prompt_payload["market"]["last"] == str(panel.market.last)
    expected_features = panel.features.model_dump(mode="json")
    expected_features.pop("cycle_id")
    expected_features.pop("as_of")
    assert prompt_payload["features"] == expected_features
    expected_evidence = [item.model_dump(mode="json") for item in panel.evidence]
    for item in expected_evidence:
        if item["excerpt"] == item["title"]:
            item.pop("excerpt")
    assert prompt_payload["evidence"] == expected_evidence
    assert "excerpt" not in prompt_payload["evidence"][0]
    assert json.loads((target / "panel.json").read_text())["evidence"] == [
        item.model_dump(mode="json") for item in panel.evidence
    ]
    assert prompt_payload["trigger"] == {
        "reason": "EVENT_BATCH",
        "evidence_ids": [panel.evidence[0].evidence_id, "evidence-not-selected"],
        "missing_evidence_ids": ["evidence-not-selected"],
    }
    assert json.loads((target / "panel.json").read_text(encoding="utf-8"))["market"]["bars"]
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["analyst_input_version"] == ANALYST_INPUT_VERSION
    (target / "analyst_prompt.md").chmod(0o644)
    (target / "analyst_prompt.md").write_text("tampered", encoding="utf-8")
    assert not verify_bundle(bundle)


def test_analyst_bundle_rejects_prompt_above_explicit_limit(
    app_config, replay_input, tmp_path
) -> None:
    from investment_manager.market.features import FeatureEngine
    from investment_manager.state.panel import PanelBuilder

    panel = PanelBuilder(app_config.panel).build(
        market=replay_input.market,
        account=replay_input.account,
        features=FeatureEngine(app_config.feature).compute(replay_input.market),
        events=replay_input.events,
    )
    evidence = panel.evidence[0].model_copy(update={"excerpt": "x" * 10_000})
    oversized = panel.model_copy(update={"evidence": (evidence,)})
    runtime = _runtime(app_config).model_copy(update={"maximum_prompt_characters": 8_000})

    with pytest.raises(ValueError, match="Analyst 内嵌信息面板超过"):
        RunBundleBuilder(runtime, app_config.proposal).build(oversized, tmp_path / "oversized")


def test_codex_output_schema_requires_every_property_and_uses_null_for_optional() -> None:
    schema = strict_output_schema(AnalystStructuredOutput.model_json_schema())

    assert schema["required"] == list(schema["properties"])
    proposal_branches = schema["properties"]["proposal"]["anyOf"]
    assert len(proposal_branches) == 2
    open_ref = next(
        item["$ref"] for item in proposal_branches if "OpenProposalOutput" in item["$ref"]
    )
    no_action_ref = next(
        item["$ref"] for item in proposal_branches if "NoActionProposalOutput" in item["$ref"]
    )
    open_proposal = schema["$defs"][open_ref.rsplit("/", 1)[-1]]
    no_action_proposal = schema["$defs"][no_action_ref.rsplit("/", 1)[-1]]
    price_condition = schema["$defs"]["PriceCondition"]
    assert open_proposal["required"] == list(open_proposal["properties"])
    assert no_action_proposal["required"] == list(no_action_proposal["properties"])
    assert "side" in open_proposal["properties"]
    assert "side" not in no_action_proposal["properties"]
    for proposal_schema in (open_proposal, no_action_proposal):
        assert "forecasts" in proposal_schema["properties"]
        assert "directional_view" not in proposal_schema["properties"]
        assert "view_horizon_minutes" not in proposal_schema["properties"]
    assert price_condition["required"] == list(price_condition["properties"])
    assert "pattern" not in price_condition["properties"]["price"]["anyOf"][1]


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

        def read(self):
            return ""

        def close(self):
            return None

        def fileno(self):
            return 0

    class FakeProcess:
        def __init__(self, command, **kwargs):
            captured["command"] = command
            captured["env"] = kwargs["env"]
            captured["process"] = self
            captured["auth_target"] = (Path(kwargs["env"]["CODEX_HOME"]) / "auth.json").readlink()
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
    monkeypatch.setattr("investment_manager.forecast.codex.selectors.DefaultSelector", FakeSelector)
    monkeypatch.setattr(
        "investment_manager.forecast.codex.codex_runtime_integrity_matches",
        lambda policy, codex_home=None: True,
    )

    snapshot = AppServerCapacityProbe(_runtime(app_config)).read(registry.accounts[0])

    sent = [json.loads(line) for line in captured["process"].stdin.written.splitlines()]
    assert [item["method"] for item in sent] == [
        "initialize",
        "initialized",
        "account/rateLimits/read",
    ]
    assert captured["command"] == ["/usr/bin/codex", "app-server", "--stdio", "--strict-config"]
    assert Path(captured["env"]["CODEX_HOME"]) != registry.accounts[0].codex_home
    assert captured["auth_target"] == registry.accounts[0].codex_home / "auth.json"
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
        {item.account_id: [InvocationResult(True, output=proposal)] for item in registry.accounts}
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


def test_router_reuses_capacity_snapshot_within_ttl(app_config, replay_input, tmp_path) -> None:
    registry = _account_registry(tmp_path)
    now = datetime(2026, 8, 18, tzinfo=UTC)
    probe = FakeProbe(
        {item.account_id: _snapshot(item.account_id, now, "10") for item in registry.accounts}
    )
    executor = FakeExecutor(
        {
            item.account_id: [
                InvocationResult(True, output=_proposal(replay_input)),
                InvocationResult(True, output=_proposal(replay_input)),
            ]
            for item in registry.accounts
        }
    )
    router = CodexAccountRouter(registry, _runtime(app_config), probe, executor)

    first = router.run(RunBundle("cycle-1", tmp_path, "hash-1", "prompt"), now=now)
    second = router.run(
        RunBundle("cycle-2", tmp_path, "hash-2", "prompt"),
        now=now + timedelta(seconds=30),
    )

    assert first.success and second.success
    assert probe.calls == ["codex_a", "codex_b", "codex_c"]


def test_production_router_allows_healthy_subset_of_fixed_three_slot_registry(
    app_config, tmp_path
) -> None:
    registry = _account_registry(tmp_path)
    accounts = tuple(
        account.model_copy(update={"enabled": account.account_id != "codex_c"})
        for account in registry.accounts
    )
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    config = app_config.model_copy(
        update={
            "codex_runtime": _runtime(app_config).model_copy(update={"binary": binary}),
            "codex_accounts": registry.model_copy(update={"accounts": accounts}),
        }
    )

    router = assemble_codex_router(
        config,
        leases=InMemoryAccountLeaseStore(),
        audit=None,
        output_adapter=TypeAdapter(AnalystStructuredOutput),
    )

    assert len(router.account_states) == 3
    assert router.account_states["codex_c"] == AccountState.DISABLED


def test_rate_limit_failover_restarts_same_immutable_bundle(
    app_config, replay_input, tmp_path
) -> None:
    registry = _account_registry(tmp_path)
    now = datetime(2026, 8, 18, tzinfo=UTC)
    executor = FakeExecutor(
        {
            "codex_a": [InvocationResult(False, failure=FailureClass.RATE_LIMIT)],
            "codex_b": [InvocationResult(True, output=_proposal(replay_input))],
            "codex_c": [InvocationResult(True, output=_proposal(replay_input))],
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
            "codex_b": [InvocationResult(True, output=_proposal(replay_input))],
            "codex_c": [InvocationResult(True, output=_proposal(replay_input))],
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


def test_timeout_never_rotates_within_batch_but_quarantines_account_for_next_batch(
    app_config, replay_input, tmp_path
) -> None:
    registry = _account_registry(tmp_path)
    now = datetime(2026, 8, 18, tzinfo=UTC)
    executor = FakeExecutor(
        {
            "codex_a": [
                InvocationResult(False, failure=FailureClass.TIMEOUT),
                InvocationResult(True, output=_proposal(replay_input)),
            ],
            "codex_b": [InvocationResult(True, output=_proposal(replay_input))],
            "codex_c": [InvocationResult(True, output=_proposal(replay_input))],
        }
    )
    probe = FakeProbe(
        {
            "codex_a": _snapshot("codex_a", now, "10"),
            "codex_b": _snapshot("codex_b", now, "20"),
            "codex_c": _snapshot("codex_c", now, "30"),
        }
    )
    router = CodexAccountRouter(
        registry,
        _runtime(app_config),
        probe,
        executor,
    )

    first = router.run(RunBundle("cycle-1", tmp_path, "hash-1", "prompt"), now=now)

    assert not first.success
    assert first.reason_code == "CODEX_TIMEOUT"
    assert [item[0] for item in executor.calls] == ["codex_a"]
    assert router.account_states["codex_a"] == AccountState.COOLDOWN

    second = router.run(
        RunBundle("cycle-2", tmp_path, "hash-2", "prompt"),
        now=now + timedelta(seconds=1),
    )

    assert second.success
    assert second.account_id == "codex_b"
    assert [item[0] for item in executor.calls] == ["codex_a", "codex_b"]

    recovered_at = now + timedelta(
        seconds=_runtime(app_config).transient_failure_cooldown_seconds + 1
    )
    probe.snapshots = {
        "codex_a": _snapshot("codex_a", recovered_at, "1"),
        "codex_b": _snapshot("codex_b", recovered_at, "20"),
        "codex_c": _snapshot("codex_c", recovered_at, "30"),
    }
    third = router.run(
        RunBundle("cycle-3", tmp_path, "hash-3", "prompt"),
        now=recovered_at,
    )

    assert third.success
    assert third.account_id == "codex_a"
    assert router.account_states["codex_a"] == AccountState.HEALTHY


def test_expired_cooldown_requires_successful_capacity_reprobe(
    app_config, replay_input, tmp_path
) -> None:
    registry = _account_registry(tmp_path)
    now = datetime(2026, 8, 18, tzinfo=UTC)
    probe = SwitchableProbe(
        {
            "codex_a": _snapshot("codex_a", now, "10"),
            "codex_b": _snapshot("codex_b", now, "20"),
            "codex_c": _snapshot("codex_c", now, "30"),
        }
    )
    executor = FakeExecutor(
        {
            "codex_a": [InvocationResult(False, failure=FailureClass.TIMEOUT)],
            "codex_b": [InvocationResult(True, output=_proposal(replay_input))],
            "codex_c": [InvocationResult(True, output=_proposal(replay_input))],
        }
    )
    runtime = _runtime(app_config)
    router = CodexAccountRouter(registry, runtime, probe, executor)

    first = router.run(RunBundle("cycle-1", tmp_path, "hash-1", "prompt"), now=now)
    probe.failing = True
    second = router.run(
        RunBundle("cycle-2", tmp_path, "hash-2", "prompt"),
        now=now + timedelta(seconds=runtime.transient_failure_cooldown_seconds + 1),
    )

    assert not first.success and second.success
    assert second.account_id == "codex_b"
    assert router.account_states["codex_a"] == AccountState.COOLDOWN


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
                InvocationResult(True, output=_proposal(replay_input)),
                InvocationResult(True, output=_proposal(replay_input)),
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
    assert result.completed_at is not None and result.completed_at >= now
    assert not executor.calls


def test_subprocess_contract_uses_selected_home_and_clears_credential_overrides(
    app_config, replay_input, tmp_path, monkeypatch
) -> None:
    from investment_manager.market.features import FeatureEngine
    from investment_manager.state.panel import PanelBuilder

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
    captured = {}

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, "codex-cli 0.148.0\n", "")

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

        def read(self):
            return ""

        def close(self):
            return None

        def fileno(self):
            return 0

    class FakeProcess:
        def __init__(self, command, **kwargs):
            captured["command"] = command
            captured["env"] = kwargs["env"]
            captured["process"] = self
            captured["auth_target"] = (Path(kwargs["env"]["CODEX_HOME"]) / "auth.json").readlink()
            self.stdin = FakeStream()
            self.stdout = FakeStream(
                (
                    json.dumps({"id": 0, "result": {}}) + "\n",
                    json.dumps({"id": 1, "result": {"thread": {"id": "thread-1"}}}) + "\n",
                    json.dumps(
                        {
                            "method": "item/completed",
                            "params": {
                                "item": {
                                    "id": "message",
                                    "type": "agentMessage",
                                    "text": json.dumps(
                                        {"proposal": proposal.model_dump(mode="json")}
                                    ),
                                }
                            },
                        }
                    )
                    + "\n",
                    json.dumps(
                        {
                            "method": "thread/tokenUsage/updated",
                            "params": {"tokenUsage": {"last": {"inputTokens": 10}}},
                        }
                    )
                    + "\n",
                    json.dumps(
                        {
                            "method": "turn/completed",
                            "params": {
                                "turn": {
                                    "status": "completed",
                                    "error": None,
                                }
                            },
                        }
                    )
                    + "\n",
                    # App Server 通知允许先于对应 RPC response 到达。
                    json.dumps({"id": 2, "result": {"turn": {"id": "turn-1"}}}) + "\n",
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

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("CODEX_API_KEY", "must-not-leak")
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "must-not-leak")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", FakeProcess)
    monkeypatch.setattr("investment_manager.forecast.codex.selectors.DefaultSelector", FakeSelector)
    checks = iter((True, False))
    monkeypatch.setattr(
        "investment_manager.forecast.codex.codex_runtime_integrity_matches",
        lambda policy, codex_home=None: next(checks),
    )

    executor = _proposal_executor(app_config)
    result = executor.execute(registry.accounts[1], bundle)

    assert result.success
    assert captured["auth_target"] == registry.accounts[1].codex_home / "auth.json"
    assert not {"OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"} & captured["env"].keys()
    command = captured["command"]
    assert command[:2] == ["/usr/bin/codex", "app-server"]
    assert "--stdio" in command
    assert "--strict-config" in command
    disabled_features = {
        command[index + 1] for index, value in enumerate(command[:-1]) if value == "--disable"
    }
    assert {
        "apps",
        "browser_use",
        "computer_use",
        "image_generation",
        "multi_agent",
        "plugins",
        "shell_tool",
        "unified_exec",
        "view_image",
    } <= disabled_features
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert "mcp_servers={}" in command
    requests = [json.loads(line) for line in captured["process"].stdin.written.splitlines()]
    thread = next(item for item in requests if item.get("method") == "thread/start")
    assert thread["params"]["sandbox"] == "read-only"
    assert "environments" not in thread["params"]
    assert result.usage == {"input_tokens": 10}
    assert result.diagnostics["codex_cli_version"] == "codex-cli 0.148.0"
    assert result.diagnostics["codex_binary_sha256"] == "0" * 64
    drifted = executor.execute(registry.accounts[1], bundle)
    assert not drifted.success
    assert drifted.failure == FailureClass.UNAVAILABLE


def test_codex_runtime_integrity_rejects_binary_drift(
    app_config, tmp_path, monkeypatch
) -> None:
    from investment_manager.forecast.codex import codex_runtime_integrity_matches

    binary = tmp_path / "codex-0.148.0"
    binary.write_bytes(b"frozen-codex-binary")
    binary.chmod(0o500)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    runtime = _runtime(app_config).model_copy(
        update={"binary": binary, "expected_binary_sha256": digest}
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, "codex-cli 0.148.0\n", ""
        ),
    )

    assert codex_runtime_integrity_matches(runtime)
    binary.chmod(0o700)
    binary.write_bytes(b"silently-replaced-codex-binary")
    binary.chmod(0o500)
    assert not codex_runtime_integrity_matches(runtime)


@pytest.mark.parametrize(
    ("events", "stderr"),
    (
        (
            [
                {
                    "method": "error",
                    "params": {"message": "denied"},
                }
            ],
            "",
        ),
        (
            [
                {
                    "method": "item/completed",
                    "params": {"item": {"id": "tool", "type": "commandExecution"}},
                }
            ],
            "",
        ),
        ([], "filesystem sandbox denied"),
    ),
)
def test_subprocess_contract_rejects_errors_and_tool_activity(
    app_config, events: list[dict], stderr: str
) -> None:
    result = _proposal_executor(app_config)._parse_app_server_events(events, stderr)

    assert not result.success
    assert result.failure == FailureClass.TOOL_PERMISSION
    assert result.diagnostics["event_count"] == len(events)


def test_subprocess_diagnostics_never_persist_event_content(app_config) -> None:
    events = [
        {"id": 0, "result": {"userAgent": "sensitive-account-detail"}},
        {"method": "turn/started", "params": {"turn": {"id": "secret-turn-id"}}},
        {
            "method": "item/completed",
            "params": {"item": {"type": "agentMessage", "text": "secret-model-output"}},
        },
    ]

    result = _proposal_executor(app_config)._parse_app_server_events(events, "")

    serialized = json.dumps(result.diagnostics)
    assert result.diagnostics == {
        "event_count": 3,
        "last_event": "item/completed",
        "turn_started": True,
        "turn_completed": False,
        "agent_message_count": 1,
        "token_usage_seen": False,
        "thread_status_change_count": 0,
        "last_thread_status": "NONE",
        "safety_buffer_update_count": 0,
        "completed_item_types": "agentMessage",
        "completed_item_count": 1,
        "untyped_completed_item_count": 0,
        "error_notification_count": 0,
        "non_null_rpc_error_count": 0,
        "completion_source": "NONE",
    }
    assert "sensitive" not in serialized
    assert "secret" not in serialized


def test_subprocess_diagnostics_expose_only_bounded_protocol_state(app_config) -> None:
    events = [
        {
            "method": "thread/status/changed",
            "params": {"status": {"type": "active", "activeFlags": ["secret"]}},
        },
        {
            "method": "model/safetyBuffering/updated",
            "params": {"reasons": ["secret-reason"]},
        },
        {
            "method": "thread/status/changed",
            "params": {"status": {"type": "idle"}},
        },
    ]

    result = _proposal_executor(app_config)._parse_app_server_events(events, "")

    assert not result.success
    assert result.diagnostics["thread_status_change_count"] == 2
    assert result.diagnostics["last_thread_status"] == "idle"
    assert result.diagnostics["safety_buffer_update_count"] == 1
    assert "secret" not in json.dumps(result.diagnostics)


def test_subprocess_recovers_only_authoritative_completed_idle_turn(
    app_config, replay_input, monkeypatch
) -> None:
    message_text = json.dumps({"proposal": _proposal(replay_input).model_dump(mode="json")})
    events = [
        {
            "method": "item/completed",
            "params": {"item": {"type": "agentMessage", "text": message_text}},
        },
        {
            "method": "thread/status/changed",
            "params": {"status": {"type": "idle"}},
        },
    ]
    sent = []
    response = {
        "id": "quant-core-terminal-read",
        "error": None,
        "result": {
            "thread": {
                "id": "thread-1",
                "status": {"type": "idle"},
                "turns": [
                    {
                        "id": "turn-1",
                        "status": "completed",
                        "error": None,
                        "itemsView": "full",
                        "completedAt": 1,
                        "items": [
                            {"type": "userMessage"},
                            {"type": "agentMessage", "text": message_text},
                        ],
                    }
                ],
            }
        },
    }

    monkeypatch.setattr(
        "investment_manager.forecast.codex._write_json_rpc",
        lambda _process, value: sent.append(value),
    )

    def fake_read(*_args, **kwargs):
        kwargs["observed"].append(response)
        return response

    monkeypatch.setattr("investment_manager.forecast.codex._read_json_rpc_until", fake_read)

    assert _terminal_message_is_idle(events)
    assert _recover_completed_turn(object(), thread_id="thread-1", turn_id="turn-1", events=events)
    assert sent == [
        {
            "method": "thread/read",
            "id": "quant-core-terminal-read",
            "params": {"threadId": "thread-1", "includeTurns": True},
        }
    ]
    result = _proposal_executor(app_config)._parse_app_server_events(
        events,
        "",
        recovered_completion=True,
    )
    assert result.success
    assert result.diagnostics["completion_source"] == "THREAD_READ"


def test_subprocess_ignores_only_failed_optional_read_after_normal_completion(
    app_config, replay_input
) -> None:
    message_text = json.dumps({"proposal": _proposal(replay_input).model_dump(mode="json")})
    events = [
        {
            "method": "item/completed",
            "params": {"item": {"type": "agentMessage", "text": message_text}},
        },
        {
            "id": "quant-core-terminal-read",
            "error": {"code": -32000, "message": "optional read raced with completion"},
        },
        {
            "method": "turn/completed",
            "params": {"turn": {"status": "completed", "error": None}},
        },
    ]

    result = _proposal_executor(app_config)._parse_app_server_events(events, "")

    assert result.success
    assert result.diagnostics["non_null_rpc_error_count"] == 1
    assert result.diagnostics["completion_source"] == "TURN_NOTIFICATION"


def test_subprocess_contract_requires_exactly_one_final_message(app_config) -> None:
    message = {
        "method": "item/completed",
        "params": {"item": {"id": "message", "type": "agentMessage", "text": "{}"}},
    }
    completed = {
        "method": "turn/completed",
        "params": {"turn": {"status": "completed", "error": None}},
    }

    result = _proposal_executor(app_config)._parse_app_server_events(
        [message, message, completed], ""
    )

    assert not result.success
    assert result.failure == FailureClass.SCHEMA_INVALID
    assert result.diagnostics["agent_message_count"] == 2
    assert result.diagnostics["schema_failure_stage"] == "AGENT_MESSAGE_COUNT"


def test_subprocess_contract_classifies_payload_validation_without_persisting_content(
    app_config,
) -> None:
    events = [
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "id": "message",
                    "type": "agentMessage",
                    "text": '{"unexpected":"secret-model-output"}',
                }
            },
        },
        {
            "method": "turn/completed",
            "params": {"turn": {"status": "completed", "error": None}},
        },
    ]

    result = _proposal_executor(app_config)._parse_app_server_events(events, "")

    assert not result.success
    assert result.failure == FailureClass.SCHEMA_INVALID
    assert result.diagnostics["agent_message_count"] == 1
    assert result.diagnostics["schema_failure_stage"] == "PAYLOAD_VALIDATION"
    assert "secret" not in json.dumps(result.diagnostics)


def test_subprocess_contract_preserves_explicit_account_failure(app_config) -> None:
    events = [
        {
            "error": {
                "code": -32603,
                "message": "401 Unauthorized; login required",
            },
        }
    ]

    result = _proposal_executor(app_config)._parse_app_server_events(events, "")

    assert not result.success
    assert result.failure == FailureClass.AUTH


def test_isolation_audit_uses_production_executor_contract_without_leaking_sentinel(
    app_config, tmp_path
) -> None:
    account = _account_registry(tmp_path).accounts[0]
    now = datetime(2026, 8, 18, tzinfo=UTC)
    executor = FakeExecutor(
        {
            account.account_id: [
                InvocationResult(
                    True,
                    output=IsolationProbeOutput(
                        can_read=False,
                        value=None,
                        reason="no file tool",
                    ),
                )
            ]
        }
    )
    target = tmp_path / "audit"

    check = audit_codex_isolation(
        account=account,
        policy=_runtime(app_config),
        target=target,
        capacity_probe=FakeProbe({account.account_id: _snapshot(account.account_id, now, "30")}),
        executor=executor,
        sentinel="secret-sentinel",
    )

    prompt = (target / "bundle" / "analyst_prompt.md").read_text(encoding="utf-8")
    assert check.ready
    assert check.reason_code == "OK"
    assert check.effective_headroom == Decimal("70")
    assert str(target / "outside" / "sentinel.txt") in prompt
    assert "secret-sentinel" not in prompt


def test_isolation_audit_fails_if_model_reports_readable_sentinel(app_config, tmp_path) -> None:
    account = _account_registry(tmp_path).accounts[0]
    now = datetime(2026, 8, 18, tzinfo=UTC)

    check = audit_codex_isolation(
        account=account,
        policy=_runtime(app_config),
        target=tmp_path / "audit",
        capacity_probe=FakeProbe({account.account_id: _snapshot(account.account_id, now, "30")}),
        executor=FakeExecutor(
            {
                account.account_id: [
                    InvocationResult(
                        True,
                        output=IsolationProbeOutput(
                            can_read=True,
                            value="secret-sentinel",
                            reason="read succeeded",
                        ),
                    )
                ]
            }
        ),
        sentinel="secret-sentinel",
    )

    assert not check.ready
    assert check.reason_code == "SENTINEL_READABLE"


def test_isolation_audit_does_not_invoke_codex_when_capacity_probe_fails(
    app_config, tmp_path
) -> None:
    account = _account_registry(tmp_path).accounts[0]
    executor = FakeExecutor({account.account_id: []})

    check = audit_codex_isolation(
        account=account,
        policy=_runtime(app_config),
        target=tmp_path / "audit",
        capacity_probe=SwitchableProbe({}, failing=True),
        executor=executor,
    )

    assert not check.ready
    assert check.reason_code == "CAPACITY_PROBE_FAILED"
    assert not executor.calls


def test_proposal_normalizer_validates_evidence_and_never_sizes_position(
    app_config, replay_input
) -> None:
    from investment_manager.market.features import FeatureEngine
    from investment_manager.state.panel import PanelBuilder

    panel = PanelBuilder(app_config.panel).build(
        market=replay_input.market,
        account=replay_input.account,
        features=FeatureEngine(app_config.feature).compute(replay_input.market),
        events=replay_input.events,
    )
    normalizer = ProposalNormalizer(app_config.proposal)
    behavior_hash = analysis_behavior_hash(app_config)

    candidates = normalizer.normalize(
        _proposal(replay_input),
        panel,
        analysis_behavior_hash=behavior_hash,
    )

    assert len(candidates) == 1
    assert candidates[0].producer_id == "codex-analyst"
    assert candidates[0].calibration_ref == (
        f"uncalibrated:{app_config.proposal.version}@{behavior_hash}"
    )
    assert not hasattr(candidates[0], "quantity")

    disclosed = _proposal(replay_input).model_copy(update={"unknowns": ("缺少资金费率与持仓量",)})
    disclosed_candidates = normalizer.normalize(
        disclosed,
        panel,
        analysis_behavior_hash=behavior_hash,
    )
    assert disclosed.unknowns == ("缺少资金费率与持仓量",)
    assert disclosed_candidates[0].unknowns == ("EDGE_CALIBRATION_MISSING",)

    bad = _proposal(replay_input).model_copy(update={"evidence_ids": ("missing",)})
    with pytest.raises(ValueError, match="evidence_id"):
        normalizer.normalize(bad, panel, analysis_behavior_hash=behavior_hash)

    mismatched = _proposal(replay_input).model_copy(
        update={
            "forecasts": (
                DirectionalForecast(
                    horizon_minutes=60,
                    directional_view=DirectionalView.DOWN,
                    confidence=Decimal("0.60"),
                ),
                *_proposal(replay_input).forecasts[1:],
            )
        }
    )
    with pytest.raises(ValueError, match="方向预测不一致"):
        normalizer.normalize(mismatched, panel, analysis_behavior_hash=behavior_hash)

    unsupported_horizon = _proposal(replay_input).model_copy(
        update={
            "forecasts": (
                DirectionalForecast(
                    horizon_minutes=90,
                    directional_view=DirectionalView.UP,
                    confidence=Decimal("0.60"),
                ),
            )
        }
    )
    with pytest.raises(ValueError, match="冻结允许集合"):
        normalizer.normalize(
            unsupported_horizon,
            panel,
            analysis_behavior_hash=behavior_hash,
        )


@dataclass
class AlwaysFailAnalyst:
    def analyze(self, panel, *, trigger=None) -> AnalystResult:
        return AnalystResult(False, None, "CODEX_SCHEMA_INVALID")
