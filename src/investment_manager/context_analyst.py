from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter

from investment_manager.analyst import (
    AccountLeaseStore,
    AnalystResult,
    CodexAccountRouter,
    RouterAuditStore,
    RunBundle,
    assemble_codex_router,
    codex_execution_contract,
    load_existing_bundle,
    strict_output_schema,
    write_run_bundle,
)
from investment_manager.config import AppConfig, CodexRuntimePolicy
from investment_manager.decision_packet import (
    ASSESS_INSTRUCTIONS,
    AssessStructuredOutput,
    DecisionPacket,
    build_assess_prompt,
    finalize_context_assessment,
)
from investment_manager.kernel.identity import canonical_json, content_hash, stable_id

ASSESS_INPUT_VERSION = "assess-input-v1"


def assess_behavior_hash(
    runtime: CodexRuntimePolicy,
    packet: DecisionPacket,
) -> str:
    return content_hash(
        {
            "input_version": ASSESS_INPUT_VERSION,
            "packet_schema_version": packet.schema_version,
            "packet_policy_version": packet.policy_version,
            "instructions": ASSESS_INSTRUCTIONS,
            "input_schema": DecisionPacket.model_json_schema(),
            "output_schema": strict_output_schema(
                AssessStructuredOutput.model_json_schema()
            ),
            "execution_contract": codex_execution_contract(),
            "model": runtime.model,
            "reasoning_effort": runtime.reasoning_effort,
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
        if len(prompt) > self._runtime.maximum_prompt_characters:
            raise ValueError("ASSESS DecisionPacket 超过 Codex 提示容量上限")
        return write_run_bundle(
            cycle_id=packet.packet_id,
            target=target,
            prompt=prompt,
            files={
                "decision_packet.json": canonical_json(packet) + "\n",
                "analyst_prompt.md": prompt + "\n",
                "output.schema.json": json.dumps(
                    strict_output_schema(AssessStructuredOutput.model_json_schema()),
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
            },
        )


class CodexContextAnalyst:
    """ASSESS 适配器；只产出 ContextAssessment，不拥有交易权限。"""

    def __init__(
        self,
        bundle_root: Path,
        bundle_builder: AssessRunBundleBuilder,
        router: CodexAccountRouter,
    ) -> None:
        self._bundle_root = bundle_root
        self._bundle_builder = bundle_builder
        self._router = router

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
                    "analysis_behavior_hash": self._bundle_builder.behavior_hash(
                        packet
                    ),
                },
            )
            if bundle is None:
                bundle = self._bundle_builder.build(packet, target)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return AnalystResult(False, None, "CODEX_BUNDLE_INVALID")
        result = self._router.run(bundle)
        if not result.success:
            return result
        if (
            not isinstance(result.output, AssessStructuredOutput)
            or result.completed_at is None
            or bundle.analysis_behavior_hash is None
        ):
            return AnalystResult(
                False,
                None,
                "CODEX_SCHEMA_INVALID",
                result.account_id,
                result.attempts,
                result.usage,
                result.completed_at,
                result.run_id,
            )
        try:
            assessment = finalize_context_assessment(
                output=result.output,
                packet=packet,
                analysis_behavior_hash=bundle.analysis_behavior_hash,
                available_at=result.completed_at,
            )
        except ValueError:
            return AnalystResult(
                False,
                None,
                "CODEX_SCHEMA_INVALID",
                result.account_id,
                result.attempts,
                result.usage,
                result.completed_at,
                result.run_id,
            )
        return AnalystResult(
            True,
            assessment,
            result.reason_code,
            result.account_id,
            result.attempts,
            result.usage,
            result.completed_at,
            result.run_id,
        )

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
        output_adapter=TypeAdapter(AssessStructuredOutput),
    )
    return CodexContextAnalyst(
        bundle_root,
        AssessRunBundleBuilder(
            config.codex_runtime,
            code_version=code_version,
            configuration_hash=content_hash(config),
        ),
        router,
    )
