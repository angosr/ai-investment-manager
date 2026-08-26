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
from investment_manager.forecast.context.analyst import configured_assess_behavior_hash
from investment_manager.forecast.contracts import ForecastContract, ForecastDecisionSlot
from investment_manager.forecast.models import ContextAssessment
from investment_manager.forecast.policy import CodexRuntimePolicy
from investment_manager.forecast.results import ForecastMechanismEffect
from investment_manager.kernel.identity import canonical_json, content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.market.models import InstrumentId, InstrumentProduct, SpotVenue
from investment_manager.market.policy import FeaturePolicy
from investment_manager.portfolio.policy import ContextForecastPolicy
from investment_manager.settings import AppConfig
from investment_manager.state.decision.packet import (
    DecisionPacket,
    PacketAssetState,
    PacketDerivativeState,
)

CONTEXT_FORECAST_INPUT_VERSION = "context-forecast-input-v9"
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
    forecasts: tuple[ContextForecastDraft, ...] = Field(min_length=1)


class ContextForecastTargetState(FrozenModel):
    """Fresh deterministic market state at one Forecast decision slot."""

    as_of: datetime
    asset_states: tuple[PacketAssetState, ...] = ()
    derivative_states: tuple[PacketDerivativeState, ...] = ()
    input_refs: tuple[str, ...] = Field(min_length=1)

    _utc_as_of = field_validator("as_of")(require_utc)

    @model_validator(mode="after")
    def identity_and_time_are_canonical(self):
        if not self.asset_states and not self.derivative_states:
            raise ValueError("Context Forecast target state 不能同时缺少现货和衍生品状态")
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
                "spot_mid_range_bps",
                "reference_spot_mid_deviation_bps",
                "widest_spot_spread_bps",
                "open_interest_change_fraction",
                "global_long_account_fraction",
                "taker_buy_sell_ratio",
            )
            if getattr(item, field_name) is not None
        )
        return tuple(sorted(selectors))


class ContextForecastAnalysisTarget(FrozenModel):
    """One contract/slot/state member of a shared portfolio Forecast call."""

    slot: ForecastDecisionSlot
    contract: ForecastContract
    target_state: ContextForecastTargetState

    @model_validator(mode="after")
    def slot_must_belong_to_contract(self):
        if self.slot.contract_id != self.contract.contract_id:
            raise ValueError("Context Forecast target 的 Slot/Contract 不一致")
        return self


class ContextForecastTargetStateBehavior(FrozenModel):
    """Immutable identity of the deterministic point-in-time state producer."""

    feature_policy: FeaturePolicy
    reference_instrument: InstrumentId
    derivative_evidence_instrument: InstrumentId | None = None
    interval: str = Field(pattern=r"^(1m|3m|5m|15m|30m|1h|2h|4h|1d)$")
    bar_window: int = Field(ge=8, le=1_000)
    funding_lookback_hours: int = Field(ge=8, le=720)
    maximum_quote_skew_seconds: int = Field(ge=1, le=300)
    cross_venue_spot_version: str | None = Field(default=None, min_length=1)
    cross_venue_spot_venues: tuple[SpotVenue, ...] = ()
    maximum_cross_venue_spot_age_seconds: int = Field(default=30, ge=1, le=900)

    @model_validator(mode="after")
    def cross_venue_contract_must_be_complete(self):
        enabled = self.cross_venue_spot_version is not None
        if enabled != bool(self.cross_venue_spot_venues):
            raise ValueError("Forecast 跨场所现货行为必须完整启用或关闭")
        if self.cross_venue_spot_venues and tuple(
            sorted(set(self.cross_venue_spot_venues), key=lambda item: item.value)
        ) != self.cross_venue_spot_venues:
            raise ValueError("Forecast 跨场所现货 venues 必须唯一排序")
        if (
            self.reference_instrument.product != InstrumentProduct.SPOT
            and self.cross_venue_spot_venues
        ):
            raise ValueError("非 Spot 规范参考不得伪造跨场所现货证据")
        return self


CONTEXT_FORECAST_INSTRUCTIONS = (
    "你是组合概率预测员。输入只包含一个已持久化世界模型、多个目标相关点时状态和逐目标预登记预测合同。",
    "你必须为每个可见 decision_slot_id 恰好输出一份 Forecast，并只回答对应合同定义的"
    "终点收益落入各 bucket 的概率；"
    "不得把一个资产的结论复制给另一个资产，不得输出订单、仓位、杠杆、精确收益点数、止损、"
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
    contracts: tuple[ForecastContract, ...],
    target_state_behaviors: tuple[ContextForecastTargetStateBehavior, ...],
    world_model_behavior_id: str,
) -> str:
    return content_hash(
        {
            "input_version": CONTEXT_FORECAST_INPUT_VERSION,
            "output_version": CONTEXT_FORECAST_OUTPUT_VERSION,
            "instructions": CONTEXT_FORECAST_INSTRUCTIONS,
            "output_schema": strict_output_schema(
                ContextForecastStructuredOutput.model_json_schema()
            ),
            "contracts": contracts,
            "target_state_behaviors": target_state_behaviors,
            "world_model_behavior_id": world_model_behavior_id,
            "policy": policy.model_dump(
                mode="json",
                # Product payoff mapping is a deterministic downstream Forecast
                # concern.  Changing legal execution products must not relabel
                # an otherwise identical AI probability behavior or its samples.
                exclude={
                    "producer_behavior_id": True,
                    "enabled": True,
                    "targets": {"__all__": {"product_payoffs"}},
                },
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
    targets: tuple[ContextForecastAnalysisTarget, ...],
    assessment: ContextAssessment,
    packet: DecisionPacket,
) -> dict[str, object]:
    """Exclude raw news and portfolio state from the second, target-only AI call."""

    return {
        "purpose": "FORECAST_ESTIMATE",
        "forecast_targets": tuple(
            {
                "decision_slot": {
                    "decision_slot_id": item.slot.slot_id,
                    "information_cutoff_at": item.slot.information_cutoff_at,
                    "completion_deadline_at": item.slot.completion_deadline_at,
                    "evaluation_at": item.slot.evaluation_at,
                    "cause": (
                        None
                        if item.slot.cause is None
                        else item.slot.cause.identity_payload()
                    ),
                },
                "forecast_contract": item.contract,
                "target_state": {
                    "as_of": item.target_state.as_of,
                    "asset_states": item.target_state.asset_states,
                    "derivative_states": item.target_state.derivative_states,
                    "coverage_gap_codes": packet.coverage_gap_codes,
                },
            }
            for item in targets
        ),
        "world_model": context_forecast_world_model_projection(assessment),
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
    targets: tuple[ContextForecastAnalysisTarget, ...],
    assessment: ContextAssessment,
) -> dict[str, object]:
    return context_forecast_output_schema_for_ids(
        decision_slot_ids=tuple(item.slot.slot_id for item in targets),
        bucket_ids=tuple(
            sorted(
                {
                    bucket.bucket_id
                    for item in targets
                    for bucket in item.contract.outcome_buckets
                }
            )
        ),
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
    decision_slot_ids: tuple[str, ...],
    bucket_ids: tuple[str, ...],
    mechanism_ids: tuple[str, ...],
    evidence_ids: tuple[str, ...],
) -> dict[str, object]:
    """Build the shared schema without exposing WorldModel semantic content."""

    if not decision_slot_ids or not bucket_ids or not mechanism_ids or not evidence_ids:
        raise ValueError("Context Forecast 输出枚举不能为空")
    if any(
        len(set(values)) != len(values)
        for values in (decision_slot_ids, bucket_ids, mechanism_ids, evidence_ids)
    ):
        raise ValueError("Context Forecast 输出枚举不能重复")
    schema = strict_output_schema(ContextForecastStructuredOutput.model_json_schema())
    definitions = schema["$defs"]
    draft = definitions["ContextForecastDraft"]
    draft["properties"]["decision_slot_id"]["enum"] = list(decision_slot_ids)
    forecasts = schema["properties"]["forecasts"]
    forecasts["minItems"] = len(decision_slot_ids)
    forecasts["maxItems"] = len(decision_slot_ids)
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
        contracts: tuple[ForecastContract, ...],
        target_state_behaviors: tuple[ContextForecastTargetStateBehavior, ...],
        world_model_behavior_id: str,
        *,
        code_version: str,
        configuration_hash: str,
    ) -> None:
        self._runtime = runtime
        self._policy = policy
        self._contracts = contracts
        self._target_state_behaviors = target_state_behaviors
        self._world_model_behavior_id = world_model_behavior_id
        self._code_version = code_version
        self._configuration_hash = configuration_hash

    @property
    def behavior_hash(self) -> str:
        return context_forecast_behavior_hash(
            self._runtime,
            self._policy,
            self._contracts,
            self._target_state_behaviors,
            self._world_model_behavior_id,
        )

    @property
    def contract_ids(self) -> tuple[str, ...]:
        return tuple(item.contract_id for item in self._contracts)

    def build(
        self,
        *,
        targets: tuple[ContextForecastAnalysisTarget, ...],
        assessment: ContextAssessment,
        packet: DecisionPacket,
        target: Path,
    ) -> RunBundle:
        projected = context_forecast_input_projection(
            targets=targets,
            assessment=assessment,
            packet=packet,
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
            targets=targets,
            assessment=assessment,
        )
        slot_ids = tuple(item.slot.slot_id for item in targets)
        cycle_id = stable_id("context_forecast_set", *slot_ids)
        input_text = canonical_json(projected) + "\n"
        return write_run_bundle(
            cycle_id=cycle_id,
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
                "decision_slot_ids": slot_ids,
                "contract_ids": self.contract_ids,
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
        targets: tuple[ContextForecastAnalysisTarget, ...],
        assessment: ContextAssessment,
        packet: DecisionPacket,
    ) -> AnalystResult:
        slot_ids = tuple(item.slot.slot_id for item in targets)
        cycle_id = stable_id("context_forecast_set", *slot_ids)
        target = self._bundle_root / stable_id(
            "context_forecast_bundle",
            cycle_id,
            assessment.assessment_id,
            self._builder.behavior_hash,
        )
        try:
            bundle = load_existing_bundle(
                cycle_id=cycle_id,
                target=target,
                expected_manifest={
                    "analysis_mode": "FORECAST_ESTIMATE",
                    "decision_slot_ids": slot_ids,
                    "contract_ids": self._builder.contract_ids,
                    "world_model_id": assessment.assessment_id,
                    "analysis_behavior_hash": self._builder.behavior_hash,
                },
            )
            if bundle is None:
                bundle = self._builder.build(
                    targets=targets,
                    assessment=assessment,
                    packet=packet,
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
    contracts: tuple[ForecastContract, ...],
    target_state_behaviors: tuple[ContextForecastTargetStateBehavior, ...],
    code_version: str,
    leases: AccountLeaseStore,
    audit: RouterAuditStore,
) -> CodexContextForecastAnalyst:
    expected = context_forecast_behavior_hash(
        config.codex_runtime,
        policy,
        contracts,
        target_state_behaviors,
        configured_assess_behavior_hash(config),
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
            contracts,
            target_state_behaviors,
            configured_assess_behavior_hash(config),
            code_version=code_version,
            configuration_hash=content_hash(config),
        ),
        router,
    )
