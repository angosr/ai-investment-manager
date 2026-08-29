from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter

from investment_manager.forecast.codex.bundle import (
    RunBundle,
    load_existing_bundle,
    write_run_bundle,
)
from investment_manager.forecast.codex.output import strict_output_schema
from investment_manager.forecast.codex.protocol import codex_execution_contract
from investment_manager.forecast.codex.router import (
    AccountLeaseStore,
    AnalystResult,
    CodexAccountRouter,
    RouterAuditStore,
    assemble_codex_router,
)
from investment_manager.forecast.context.contract import (
    ASSESS_INSTRUCTIONS,
    ContextAssessmentContractError,
    WorldModelStructuredOutput,
    assessment_available_feature_selectors,
    assessment_current_evidence_ids,
    assessment_previous_mechanism_ids,
    assessment_world_model_evidence_ids,
    build_assess_prompt,
    finalize_world_model,
)
from investment_manager.forecast.policy import CodexRuntimePolicy
from investment_manager.kernel.identity import canonical_json, content_hash, stable_id
from investment_manager.settings import AppConfig
from investment_manager.state.decision.packet import DecisionPacket

ASSESS_INPUT_VERSION = "world-model-input-v10"
ASSESS_DYNAMIC_OUTPUT_CONTRACT_VERSION = "world-model-output-v10"


class AssessPromptCapacityError(ValueError):
    """The frozen assessment prompt cannot fit the configured runtime boundary."""


def assess_output_schema(packet: DecisionPacket) -> dict[str, object]:
    """Constrain packet-dependent semantics before Codex sampling."""

    schema = strict_output_schema(WorldModelStructuredOutput.model_json_schema())
    definitions = schema["$defs"]
    draft = definitions["WorldModelDraft"]
    evidence_ids = assessment_world_model_evidence_ids(packet)
    causal_node = definitions["ContextCausalNode"]
    causal_node["properties"]["evidence_ids"]["items"]["enum"] = list(evidence_ids)
    mechanism = definitions["ContextMechanismDraft"]
    mechanism["properties"]["conflicting_evidence_ids"]["items"]["enum"] = list(evidence_ids)
    previous_mechanism_ids = assessment_previous_mechanism_ids(packet)
    continuity = mechanism["properties"]["continuity_ref"]
    continuity["anyOf"][0]["enum"] = list(previous_mechanism_ids)
    retirement = definitions["ContextMechanismRetirement"]
    retirement["properties"]["previous_mechanism_id"]["enum"] = list(previous_mechanism_ids)
    retirement["properties"]["evidence_ids"]["items"]["enum"] = list(
        sorted(assessment_current_evidence_ids(packet))
    )
    retired_mechanisms = draft["properties"]["retired_mechanisms"]
    retired_mechanisms["maxItems"] = len(previous_mechanism_ids)
    verification = definitions["ContextVerificationTestDraft"]
    verification["properties"]["feature_selector"]["enum"] = list(
        assessment_available_feature_selectors(packet)
    )
    return schema


def assess_behavior_hash(
    runtime: CodexRuntimePolicy,
    packet: DecisionPacket,
) -> str:
    return _assess_behavior_hash(
        runtime,
        packet_schema_version=packet.schema_version,
        packet_policy_version=packet.policy_version,
        mandate_version=packet.mandate_version,
        mandate_exposures=tuple(
            (item.economic_exposure, item.asset) for item in packet.mandate_exposures
        ),
        observation_assets=tuple((item.asset, item.market_symbol) for item in packet.asset_states),
    )


def configured_assess_behavior_hash(config: AppConfig) -> str:
    """Return the behavior identity future packets from this config will carry."""

    mandate = config.assessment.mandate
    return _assess_behavior_hash(
        config.codex_runtime,
        packet_schema_version=config.decision_state.packet_policy.schema_version,
        packet_policy_version=config.decision_state.packet_policy.version,
        mandate_version=mandate.version,
        mandate_exposures=tuple(
            (item.economic_exposure, item.asset) for item in mandate.mandate_exposures
        ),
        observation_assets=tuple(
            (asset.asset, asset.market_symbol) for asset in mandate.observation_assets
        ),
    )


def _assess_behavior_hash(
    runtime: CodexRuntimePolicy,
    *,
    packet_schema_version: str,
    packet_policy_version: str,
    mandate_version: str,
    mandate_exposures: tuple[tuple[str, str], ...],
    observation_assets: tuple[tuple[str, str], ...],
) -> str:
    return content_hash(
        {
            "input_version": ASSESS_INPUT_VERSION,
            "packet_schema_version": packet_schema_version,
            "packet_policy_version": packet_policy_version,
            "instructions": ASSESS_INSTRUCTIONS,
            "input_schema": DecisionPacket.model_json_schema(),
            "output_schema": strict_output_schema(WorldModelStructuredOutput.model_json_schema()),
            "dynamic_output_contract_version": ASSESS_DYNAMIC_OUTPUT_CONTRACT_VERSION,
            "mandate_version": mandate_version,
            "mandate_exposures": mandate_exposures,
            "observation_assets": observation_assets,
            "execution_contract": codex_execution_contract(),
            "runtime_policy_version": runtime.version,
            "expected_cli_version": runtime.expected_cli_version,
            "expected_binary_sha256": runtime.expected_binary_sha256,
            "model": runtime.model,
            "reasoning_effort": runtime.reasoning_effort,
            "maximum_schema_attempts": 1 + runtime.max_account_switches,
        }
    )


class AssessRunBundleBuilder:
    def __init__(
        self,
        runtime: CodexRuntimePolicy,
        *,
        code_version: str,
        configuration_hash: str,
    ) -> None:
        self._runtime = runtime
        self._code_version = code_version
        self._configuration_hash = configuration_hash

    def behavior_hash(self, packet: DecisionPacket) -> str:
        return assess_behavior_hash(self._runtime, packet)

    def build(self, packet: DecisionPacket, target: Path) -> RunBundle:
        prompt = build_assess_prompt(packet)
        behavior_hash = self.behavior_hash(packet)
        output_schema = assess_output_schema(packet)
        if len(prompt) > self._runtime.maximum_prompt_characters:
            raise AssessPromptCapacityError("ASSESS DecisionPacket 超过 Codex 提示容量上限")
        return write_run_bundle(
            cycle_id=packet.packet_id,
            target=target,
            prompt=prompt,
            files={
                "decision_packet.json": canonical_json(packet) + "\n",
                "analyst_prompt.md": prompt + "\n",
                "output.schema.json": json.dumps(
                    output_schema,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
            },
            manifest={
                "analysis_mode": "ASSESS",
                "assess_input_version": ASSESS_INPUT_VERSION,
                "decision_packet_hash": packet.content_hash,
                "model": self._runtime.model,
                "reasoning_effort": self._runtime.reasoning_effort,
                "runtime_policy_version": self._runtime.version,
                "code_version": self._code_version,
                "configuration_hash": self._configuration_hash,
                "analysis_behavior_hash": behavior_hash,
                "dynamic_output_contract_version": ASSESS_DYNAMIC_OUTPUT_CONTRACT_VERSION,
                "output_schema_hash": content_hash(output_schema),
            },
        )


class CodexContextAnalyst:
    """ASSESS 适配器；只产出 ContextAssessment，不拥有交易权限。"""

    def __init__(
        self,
        bundle_root: Path,
        bundle_builder: AssessRunBundleBuilder,
        router: CodexAccountRouter,
        *,
        maximum_schema_attempts: int = 1,
    ) -> None:
        if not 1 <= maximum_schema_attempts <= 3:
            raise ValueError("ContextAssessment Schema 尝试次数必须在 1..3")
        self._bundle_root = bundle_root
        self._bundle_builder = bundle_builder
        self._router = router
        self._maximum_schema_attempts = maximum_schema_attempts

    def behavior_hash(self, packet: DecisionPacket) -> str:
        return self._bundle_builder.behavior_hash(packet)

    def assess(self, packet: DecisionPacket) -> AnalystResult:
        target = self._bundle_root / stable_id(
            "assess_bundle",
            packet.packet_id,
            packet.content_hash,
            self._bundle_builder.behavior_hash(packet),
        )
        try:
            bundle = load_existing_bundle(
                cycle_id=packet.packet_id,
                target=target,
                expected_manifest={
                    "analysis_mode": "ASSESS",
                    "decision_packet_hash": packet.content_hash,
                    "analysis_behavior_hash": self._bundle_builder.behavior_hash(packet),
                },
            )
            if bundle is None:
                bundle = self._bundle_builder.build(packet, target)
        except AssessPromptCapacityError:
            return AnalystResult(False, None, "CODEX_PROMPT_CAPACITY_EXCEEDED")
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return AnalystResult(False, None, "CODEX_BUNDLE_INVALID")
        attempts = 0
        usage: dict[str, int] = {}
        last_result: AnalystResult | None = None
        for _schema_attempt in range(self._maximum_schema_attempts):
            result = self._router.run(bundle)
            last_result = result
            attempts += result.attempts
            usage = _merge_usage(usage, result.usage)
            if not result.success:
                if result.reason_code == "CODEX_SCHEMA_INVALID":
                    continue
                return AnalystResult(
                    False,
                    None,
                    result.reason_code,
                    result.account_id,
                    attempts,
                    usage,
                    result.completed_at,
                    result.run_id,
                )
            if (
                not isinstance(result.output, WorldModelStructuredOutput)
                or result.completed_at is None
                or bundle.analysis_behavior_hash is None
            ):
                continue
            try:
                assessment = finalize_world_model(
                    output=result.output,
                    packet=packet,
                    analysis_behavior_hash=bundle.analysis_behavior_hash,
                    available_at=result.completed_at,
                )
            except ContextAssessmentContractError as exc:
                return AnalystResult(
                    False,
                    None,
                    exc.reason_code,
                    result.account_id,
                    attempts,
                    usage,
                    result.completed_at,
                    result.run_id,
                )
            return AnalystResult(
                True,
                assessment,
                result.reason_code,
                result.account_id,
                attempts,
                usage,
                result.completed_at,
                result.run_id,
            )
        assert last_result is not None
        return AnalystResult(
            False,
            None,
            "CODEX_SCHEMA_INVALID",
            last_result.account_id,
            attempts,
            usage,
            last_result.completed_at,
            last_result.run_id,
        )


def _merge_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    return {key: left.get(key, 0) + right.get(key, 0) for key in left.keys() | right.keys()}


def assemble_codex_context_analyst(
    config: AppConfig,
    *,
    bundle_root: Path,
    code_version: str,
    leases: AccountLeaseStore,
    audit: RouterAuditStore,
) -> CodexContextAnalyst:
    router = assemble_codex_router(
        config,
        leases=leases,
        audit=audit,
        output_adapter=TypeAdapter(WorldModelStructuredOutput),
    )
    return CodexContextAnalyst(
        bundle_root,
        AssessRunBundleBuilder(
            config.codex_runtime,
            code_version=code_version,
            configuration_hash=content_hash(config),
        ),
        router,
        maximum_schema_attempts=1 + config.codex_runtime.max_account_switches,
    )
