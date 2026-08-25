"""Codex boundary for one pre-registered Context probability forecast."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from pydantic import Field, TypeAdapter, field_validator, model_validator

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
from investment_manager.forecast.contracts import ForecastContract, ForecastDecisionSlot
from investment_manager.forecast.models import ContextAssessment
from investment_manager.forecast.policy import CodexRuntimePolicy
from investment_manager.forecast.results import ForecastMechanismEffect
from investment_manager.kernel.identity import canonical_json, content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.market.models import InstrumentId
from investment_manager.market.policy import FeaturePolicy
from investment_manager.portfolio.policy import ContextForecastPolicy
from investment_manager.settings import AppConfig
from investment_manager.state.decision.packet import (
    DecisionPacket,
    PacketAssetState,
    PacketDerivativeState,
)

CONTEXT_FORECAST_INPUT_VERSION = "context-forecast-input-v6"
CONTEXT_FORECAST_OUTPUT_VERSION = "context-forecast-output-v1"


class ContextForecastProbabilityDraft(FrozenModel):
    bucket_id: str = Field(min_length=1, max_length=64)
    probability: str = Field(pattern=r"^(0(?:\.\d+)?|1(?:\.0+)?)$")


class ContextForecastContributionDraft(FrozenModel):
    mechanism_id: str = Field(min_length=1)
    effect: ForecastMechanismEffect
    rationale: str = Field(min_length=1, max_length=600)


class ContextForecastDraft(FrozenModel):
    decision_slot_id: str = Field(min_length=1)
    outcome_probabilities: tuple[ContextForecastProbabilityDraft, ...] = Field(min_length=3)
    mechanism_contributions: tuple[ContextForecastContributionDraft, ...] = Field(min_length=1)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    invalidation_conditions: tuple[str, ...] = Field(min_length=1)


class ContextForecastStructuredOutput(FrozenModel):
    forecast: ContextForecastDraft


class ContextForecastTargetState(FrozenModel):
    """Fresh deterministic market state at one Forecast decision slot."""

    as_of: datetime
    asset_states: tuple[PacketAssetState, ...] = Field(min_length=1)
    derivative_states: tuple[PacketDerivativeState, ...] = ()
    input_refs: tuple[str, ...] = Field(min_length=1)

    _utc_as_of = field_validator("as_of")(require_utc)

    @model_validator(mode="after")
    def identity_and_time_are_canonical(self):
        if tuple(sorted(set(self.input_refs))) != self.input_refs:
            raise ValueError("Context Forecast target state input_refs 必须唯一且排序")
        if any(item.observed_at > self.as_of for item in self.asset_states):
            raise ValueError("Context Forecast asset state 不能晚于决策槽")
        if any(item.observed_at > self.as_of for item in self.derivative_states):
            raise ValueError("Context Forecast derivative state 不能晚于决策槽")
        return self

    @property
    def feature_selectors(self) -> tuple[str, ...]:
        selectors = {
            f"asset_state:{item.asset}.{field_name}"
            for item in self.asset_states
            for field_name in (
                "return_fraction",
                "realized_volatility",
                "atr",
                "spread_bps",
                "volume_ratio",
                "regime",
            )
        }
        selectors.update(
            f"derivative_state:{item.asset}.{field_name}"
            for item in self.derivative_states
            for field_name in (
                "mark_index_premium_bps",
                "executable_short_basis_bps",
                "last_funding_rate_bps",
                "open_interest_change_fraction",
                "global_long_account_fraction",
                "taker_buy_sell_ratio",
            )
            if getattr(item, field_name) is not None
        )
        return tuple(sorted(selectors))


class ContextForecastTargetStateBehavior(FrozenModel):
    """Immutable identity of the deterministic point-in-time state producer."""

    feature_policy: FeaturePolicy
    spot_instrument: InstrumentId
    derivative_evidence_instrument: InstrumentId | None = None
    interval: str = Field(pattern=r"^(1m|3m|5m|15m|30m|1h|2h|4h|1d)$")
    bar_window: int = Field(ge=8, le=1_000)
    funding_lookback_hours: int = Field(ge=8, le=720)
    maximum_quote_skew_seconds: int = Field(ge=1, le=300)


CONTEXT_FORECAST_INSTRUCTIONS = (
    "你是概率预测员。输入只包含一个已持久化世界模型、目标相关点时状态和一份预登记预测合同。",
    "你只回答合同定义的终点收益落入各 bucket 的概率；不得输出订单、仓位、杠杆、精确收益点数、止损、"
    "风险预算或交易建议。不要重新写世界认知，也不要要求补建数据。",
    "outcome_probabilities 必须逐项使用输入合同给出的 bucket_id 和顺序，"
    "概率使用 0 到 1 的十进制字符串，"
    "总和精确等于 1。不确定时扩大中间和尾部概率，不能拒绝形成预测。",
    "mechanism_contributions 只引用输入 world_model.mechanisms 的 mechanism_id，"
    "说明该机制相对合同基准分布"
    "带来上行、下行、不确定性或无实质影响；不得把市场价格结果冒充外生原因。",
    "evidence_refs 只引用输入 world_model 中已有的 evidence_id。"
    "invalidation_conditions 是可观察的未来重估线索，"
    "没有即时资本权限。中文应清晰、具体、可证伪；资产代码、数值和枚举保留原文。",
)


def context_forecast_behavior_hash(
    runtime: CodexRuntimePolicy,
    policy: ContextForecastPolicy,
    contract: ForecastContract,
    target_state_behavior: ContextForecastTargetStateBehavior,
) -> str:
    return content_hash(
        {
            "input_version": CONTEXT_FORECAST_INPUT_VERSION,
            "output_version": CONTEXT_FORECAST_OUTPUT_VERSION,
            "instructions": CONTEXT_FORECAST_INSTRUCTIONS,
            "output_schema": strict_output_schema(
                ContextForecastStructuredOutput.model_json_schema()
            ),
            "contract": contract,
            "target_state_behavior": target_state_behavior,
            "policy": policy.model_dump(
                mode="json",
                exclude={"producer_behavior_id", "enabled"},
            ),
            "execution_contract": codex_execution_contract(),
            "runtime_policy_version": runtime.version,
            "expected_cli_version": runtime.expected_cli_version,
            "expected_binary_sha256": runtime.expected_binary_sha256,
            "model": runtime.model,
            "reasoning_effort": runtime.reasoning_effort,
            "maximum_schema_attempts": 1 + runtime.max_account_switches,
        }
    )


def context_forecast_input_projection(
    *,
    slot: ForecastDecisionSlot,
    contract: ForecastContract,
    assessment: ContextAssessment,
    packet: DecisionPacket,
    target_state: ContextForecastTargetState,
) -> dict[str, object]:
    """Exclude raw news and portfolio state from the second, target-only AI call."""

    return {
        "purpose": "FORECAST_ESTIMATE",
        "decision_slot": {
            "decision_slot_id": slot.slot_id,
            "information_cutoff_at": slot.information_cutoff_at,
            "completion_deadline_at": slot.completion_deadline_at,
            "evaluation_at": slot.evaluation_at,
            "cause": slot.cause,
        },
        "forecast_contract": contract,
        "world_model": context_forecast_world_model_projection(assessment),
        "target_state": {
            "as_of": target_state.as_of,
            "asset_states": target_state.asset_states,
            "derivative_states": target_state.derivative_states,
            "coverage_gap_codes": packet.coverage_gap_codes,
        },
    }


def context_forecast_world_model_projection(
    assessment: ContextAssessment,
) -> dict[str, object]:
    """Keep causal decision content; exclude WorldModel maintenance metadata."""

    return {
        "assessment_id": assessment.assessment_id,
        "as_of": assessment.as_of,
        "available_at": assessment.available_at,
        "synthesis": assessment.synthesis,
        "synthesis_horizon_hours": assessment.synthesis_horizon_hours,
        "event_references": tuple(
            {
                "evidence_id": item.evidence_id,
                "source": item.source,
                "title": item.title,
                "event_time": item.event_time,
                "impact_state": item.impact_state,
                "rationale": item.rationale,
            }
            for item in assessment.event_references
        ),
        "mechanisms": tuple(
            {
                "mechanism_id": item.mechanism_id,
                "relationship": item.relationship,
                "claim": item.claim,
                "horizon_hours": item.horizon_hours,
                "causal_chain": item.causal_chain,
                "transmission_stage": item.transmission_stage,
                "conflicting_evidence_ids": item.conflicting_evidence_ids,
                "invalidation_conditions": item.invalidation_conditions,
            }
            for item in assessment.mechanisms
        ),
    }


def context_forecast_output_schema(
    *,
    slot: ForecastDecisionSlot,
    contract: ForecastContract,
    assessment: ContextAssessment,
) -> dict[str, object]:
    return context_forecast_output_schema_for_ids(
        decision_slot_id=slot.slot_id,
        bucket_ids=tuple(item.bucket_id for item in contract.outcome_buckets),
        mechanism_ids=tuple(item.mechanism_id for item in assessment.mechanisms),
        evidence_ids=tuple(
            sorted(
                {
                    *(item.evidence_id for item in assessment.event_references),
                    *(
                        evidence_id
                        for mechanism in assessment.mechanisms
                        for node in mechanism.causal_chain
                        for evidence_id in node.evidence_ids
                    ),
                    *(
                        evidence_id
                        for mechanism in assessment.mechanisms
                        for evidence_id in mechanism.conflicting_evidence_ids
                    ),
                }
            )
        ),
    )


def context_forecast_output_schema_for_ids(
    *,
    decision_slot_id: str,
    bucket_ids: tuple[str, ...],
    mechanism_ids: tuple[str, ...],
    evidence_ids: tuple[str, ...],
) -> dict[str, object]:
    """Build the shared schema without exposing WorldModel semantic content."""

    if not bucket_ids or not mechanism_ids or not evidence_ids:
        raise ValueError("Context Forecast 输出枚举不能为空")
    if any(len(set(values)) != len(values) for values in (bucket_ids, mechanism_ids, evidence_ids)):
        raise ValueError("Context Forecast 输出枚举不能重复")
    schema = strict_output_schema(ContextForecastStructuredOutput.model_json_schema())
    definitions = schema["$defs"]
    draft = definitions["ContextForecastDraft"]
    draft["properties"]["decision_slot_id"]["const"] = decision_slot_id
    probability = definitions["ContextForecastProbabilityDraft"]
    probability["properties"]["bucket_id"]["enum"] = list(bucket_ids)
    contribution = definitions["ContextForecastContributionDraft"]
    contribution["properties"]["mechanism_id"]["enum"] = list(mechanism_ids)
    draft["properties"]["evidence_refs"]["items"]["enum"] = list(evidence_ids)
    return schema


class ContextForecastRunBundleBuilder:
    def __init__(
        self,
        runtime: CodexRuntimePolicy,
        policy: ContextForecastPolicy,
        contract: ForecastContract,
        target_state_behavior: ContextForecastTargetStateBehavior,
        *,
        code_version: str,
        configuration_hash: str,
    ) -> None:
        self._runtime = runtime
        self._policy = policy
        self._contract = contract
        self._target_state_behavior = target_state_behavior
        self._code_version = code_version
        self._configuration_hash = configuration_hash

    @property
    def behavior_hash(self) -> str:
        return context_forecast_behavior_hash(
            self._runtime,
            self._policy,
            self._contract,
            self._target_state_behavior,
        )

    @property
    def contract_id(self) -> str:
        return self._contract.contract_id

    def build(
        self,
        *,
        slot: ForecastDecisionSlot,
        assessment: ContextAssessment,
        packet: DecisionPacket,
        target_state: ContextForecastTargetState,
        target: Path,
    ) -> RunBundle:
        projected = context_forecast_input_projection(
            slot=slot,
            contract=self._contract,
            assessment=assessment,
            packet=packet,
            target_state=target_state,
        )
        prompt = "\n".join(
            (
                *CONTEXT_FORECAST_INSTRUCTIONS,
                "context_forecast_input_json=",
                canonical_json(projected),
            )
        )
        if len(prompt) > self._runtime.maximum_prompt_characters:
            raise ValueError("FORECAST_ESTIMATE 输入超过 Codex 提示容量上限")
        output_schema = context_forecast_output_schema(
            slot=slot,
            contract=self._contract,
            assessment=assessment,
        )
        input_text = canonical_json(projected) + "\n"
        return write_run_bundle(
            cycle_id=slot.slot_id,
            target=target,
            prompt=prompt,
            files={
                "context_forecast_input.json": input_text,
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
                "analysis_mode": "FORECAST_ESTIMATE",
                "input_version": CONTEXT_FORECAST_INPUT_VERSION,
                "output_version": CONTEXT_FORECAST_OUTPUT_VERSION,
                "decision_slot_id": slot.slot_id,
                "contract_id": self._contract.contract_id,
                "world_model_id": assessment.assessment_id,
                "decision_packet_hash": packet.content_hash,
                "model": self._runtime.model,
                "reasoning_effort": self._runtime.reasoning_effort,
                "runtime_policy_version": self._runtime.version,
                "code_version": self._code_version,
                "configuration_hash": self._configuration_hash,
                "analysis_behavior_hash": self.behavior_hash,
                "output_schema_hash": content_hash(output_schema),
            },
        )


class CodexContextForecastAnalyst:
    """One-purpose Codex adapter; it returns probabilities, never a trade."""

    def __init__(
        self,
        bundle_root: Path,
        builder: ContextForecastRunBundleBuilder,
        router: CodexAccountRouter,
    ) -> None:
        self._bundle_root = bundle_root
        self._builder = builder
        self._router = router

    def estimate(
        self,
        *,
        slot: ForecastDecisionSlot,
        assessment: ContextAssessment,
        packet: DecisionPacket,
        target_state: ContextForecastTargetState,
    ) -> AnalystResult:
        target = self._bundle_root / stable_id(
            "context_forecast_bundle",
            slot.slot_id,
            assessment.assessment_id,
            self._builder.behavior_hash,
        )
        try:
            bundle = load_existing_bundle(
                cycle_id=slot.slot_id,
                target=target,
                expected_manifest={
                    "analysis_mode": "FORECAST_ESTIMATE",
                    "decision_slot_id": slot.slot_id,
                    "contract_id": self._builder.contract_id,
                    "world_model_id": assessment.assessment_id,
                    "analysis_behavior_hash": self._builder.behavior_hash,
                },
            )
            if bundle is None:
                bundle = self._builder.build(
                    slot=slot,
                    assessment=assessment,
                    packet=packet,
                    target_state=target_state,
                    target=target,
                )
        except ValueError as exc:
            reason = (
                "FORECAST_INPUT_TOO_LARGE"
                if str(exc) == "FORECAST_ESTIMATE 输入超过 Codex 提示容量上限"
                else "CODEX_BUNDLE_INVALID"
            )
            return AnalystResult(False, None, reason)
        except (OSError, KeyError, json.JSONDecodeError):
            return AnalystResult(False, None, "CODEX_BUNDLE_INVALID")
        return self._router.run(bundle)


def assemble_codex_context_forecast_analyst(
    config: AppConfig,
    *,
    policy: ContextForecastPolicy,
    contract: ForecastContract,
    target_state_behavior: ContextForecastTargetStateBehavior,
    code_version: str,
    leases: AccountLeaseStore,
    audit: RouterAuditStore,
) -> CodexContextForecastAnalyst:
    expected = context_forecast_behavior_hash(
        config.codex_runtime,
        policy,
        contract,
        target_state_behavior,
    )
    if policy.producer_behavior_id != expected:
        raise ValueError("Context Forecast producer_behavior_id 未覆盖当前完整分析行为")
    router = assemble_codex_router(
        config,
        leases=leases,
        audit=audit,
        output_adapter=TypeAdapter(ContextForecastStructuredOutput),
    )
    return CodexContextForecastAnalyst(
        config.codex_runtime.bundle_root,
        ContextForecastRunBundleBuilder(
            config.codex_runtime,
            policy,
            contract,
            target_state_behavior,
            code_version=code_version,
            configuration_hash=content_hash(config),
        ),
        router,
    )
