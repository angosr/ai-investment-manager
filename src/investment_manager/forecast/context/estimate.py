"""Codex boundary for one pre-registered Context probability forecast."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from pydantic import Field, field_validator, model_validator

from investment_manager.forecast.codex.output import strict_output_schema
from investment_manager.forecast.contracts import ForecastContract, ForecastDecisionSlot
from investment_manager.forecast.models import ContextAssessment
from investment_manager.forecast.policy import CodexRuntimePolicy
from investment_manager.forecast.results import ForecastMechanismEffect
from investment_manager.kernel.identity import canonical_json
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.market.models import InstrumentId, InstrumentProduct, SpotVenue
from investment_manager.market.policy import FeaturePolicy
from investment_manager.portfolio.policy import ContextForecastPolicy
from investment_manager.state.decision.packet import (
    DecisionPacket,
    PacketAssetState,
    PacketDerivativeState,
)

CONTEXT_FORECAST_INPUT_VERSION = "context-forecast-input-v12"
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


class ContextForecastComparisonState(FrozenModel):
    """Point-in-time economic cross-check; never an Outcome or capital product."""

    instrument: InstrumentId
    observed_at: datetime
    index_price: Decimal = Field(gt=0)
    comparison_price_multiplier: Decimal = Field(gt=0)
    target_reference_deviation_bps: Decimal
    mark_index_premium_bps: Decimal
    market_session: str = Field(min_length=1)
    input_refs: tuple[str, ...] = Field(min_length=1)

    _utc_observed_at = field_validator("observed_at")(require_utc)

    @field_validator("input_refs")
    @classmethod
    def refs_are_canonical(cls, refs: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(refs))) != refs:
            raise ValueError("Context Forecast comparison refs 必须唯一排序")
        return refs


class ContextForecastTargetState(FrozenModel):
    """Fresh deterministic market state at one Forecast decision slot."""

    as_of: datetime
    asset_states: tuple[PacketAssetState, ...] = ()
    derivative_states: tuple[PacketDerivativeState, ...] = ()
    comparison_states: tuple[ContextForecastComparisonState, ...] = ()
    missing_comparison_instrument_keys: tuple[str, ...] = ()
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
        if any(item.observed_at > self.as_of for item in self.comparison_states):
            raise ValueError("Context Forecast comparison state 不能晚于决策槽")
        comparison_keys = tuple(item.instrument.key for item in self.comparison_states)
        if tuple(sorted(set(comparison_keys))) != comparison_keys:
            raise ValueError("Context Forecast comparison states 必须唯一排序")
        if tuple(sorted(set(self.missing_comparison_instrument_keys))) != (
            self.missing_comparison_instrument_keys
        ):
            raise ValueError("Context Forecast missing comparison keys 必须唯一排序")
        if set(comparison_keys) & set(self.missing_comparison_instrument_keys):
            raise ValueError("Context Forecast comparison 不能同时存在和缺失")
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
        selectors.update(
            f"comparison_state:{item.instrument.base_asset}.{field_name}"
            for item in self.comparison_states
            for field_name in (
                "target_reference_deviation_bps",
                "mark_index_premium_bps",
            )
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


@dataclass(frozen=True, slots=True)
class ContextForecastModelInput:
    payload: dict[str, object]
    source_refs_by_slot: dict[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        slot_ids = tuple(self.source_refs_by_slot)
        if len(slot_ids) != len(set(slot_ids)) or any(
            refs != tuple(sorted(set(refs))) for refs in self.source_refs_by_slot.values()
        ):
            raise ValueError("Context Forecast 附加来源引用必须按槽唯一排序")


class ContextForecastTargetStateBehavior(FrozenModel):
    """Immutable identity of the deterministic point-in-time state producer."""

    feature_policy: FeaturePolicy
    reference_instrument: InstrumentId
    derivative_evidence_instrument: InstrumentId | None = None
    comparison_instrument: InstrumentId | None = None
    comparison_price_multiplier: Decimal | None = Field(default=None, gt=0)
    maximum_comparison_age_seconds: int = Field(default=300, ge=1, le=3_600)
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
        if (
            self.cross_venue_spot_venues
            and tuple(sorted(set(self.cross_venue_spot_venues), key=lambda item: item.value))
            != self.cross_venue_spot_venues
        ):
            raise ValueError("Forecast 跨场所现货 venues 必须唯一排序")
        if (
            self.reference_instrument.product != InstrumentProduct.SPOT
            and self.cross_venue_spot_venues
        ):
            raise ValueError("非 Spot 规范参考不得伪造跨场所现货证据")
        comparison_enabled = self.comparison_instrument is not None
        if comparison_enabled != (self.comparison_price_multiplier is not None):
            raise ValueError("Forecast comparison Instrument 与价格换算必须同时配置")
        if comparison_enabled:
            assert self.comparison_instrument is not None
            if self.reference_instrument.product != InstrumentProduct.SPOT:
                raise ValueError("Forecast comparison 目前只允许比较 Spot Outcome")
            if self.comparison_instrument.product == InstrumentProduct.SPOT:
                raise ValueError("Forecast comparison 必须使用独立指数化线性产品")
            if self.comparison_instrument.quote_asset != self.reference_instrument.quote_asset:
                raise ValueError("Forecast comparison 与 Outcome 必须使用同一计价资产")
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
    "把 forecast_benchmark 作为无条件先验，再联合 target_state 和 world_model "
    "形成条件分布。target_state 中的近期收益、波动和状态、成交量、现货与永续主动流、"
    "未平仓量、资金费率和仓位是目标自身的可预测状态，不需要先被外生机制重复证明才能"
    "改变先验；但不得把单一短窗口动量机械外推到合同终点。",
    "comparison_states 是观察专用经济交叉参考：target_reference_deviation_bps 区分代理 Outcome"
    "与外部指数，mark_index_premium_bps 区分比较产品自身 basis，market_session 标记其交易时段。"
    "它不改变 Outcome 或赋予比较 Instrument 资本身份；缺失键只扩大对应目标的不确定性，"
    "不得从目标自身价格补猜外部参考。",
    "mechanism_contributions 只引用输入 world_model.mechanisms 的 mechanism_id，"
    "说明该机制相对合同基准分布"
    "带来上行、下行、不确定性或无实质影响；不得把市场价格结果冒充外生原因。",
    "evidence_refs 只引用输入 world_model 中已有的 evidence_id。"
    "invalidation_conditions 是可观察的未来重估线索，"
    "没有即时资本权限。中文应清晰、具体、可证伪；资产代码、数值和枚举保留原文。",
)


def context_forecast_prompt(analysis_input: dict[str, object]) -> str:
    """Build the only model-visible prompt for formal and exact-replica calls."""

    return "\n".join(
        (
            *CONTEXT_FORECAST_INSTRUCTIONS,
            "context_forecast_input_json=",
            canonical_json(analysis_input),
        )
    )


def context_forecast_runtime(
    runtime: CodexRuntimePolicy,
    policy: ContextForecastPolicy,
) -> CodexRuntimePolicy:
    """Use the role's frozen effort without weakening the WorldModel analyst."""

    return runtime.model_copy(update={"reasoning_effort": policy.reasoning_effort})


def context_forecast_input_projection(
    *,
    targets: tuple[ContextForecastAnalysisTarget, ...],
    assessment: ContextAssessment,
    packet: DecisionPacket,
) -> ContextForecastModelInput:
    """Exclude raw news and portfolio state from the second, target-only AI call."""

    return ContextForecastModelInput(
        payload={
            "purpose": "FORECAST_ESTIMATE",
            "forecast_targets": tuple(
                {
                    "decision_slot": {
                        "decision_slot_id": item.slot.slot_id,
                        "information_cutoff_at": item.slot.information_cutoff_at,
                        "completion_deadline_at": item.slot.completion_deadline_at,
                        "evaluation_at": item.slot.evaluation_at,
                        "cause_origin": (
                            None if item.slot.cause is None else item.slot.cause.origin.value
                        ),
                    },
                    "forecast_contract": {
                        "contract_id": item.contract.contract_id,
                        "outcome_family_id": item.contract.outcome_family_id,
                        "horizon_minutes": item.contract.horizon_minutes,
                        "reference_instrument": {
                            "symbol": item.contract.target.legs[0].instrument.symbol,
                            "product": item.contract.target.legs[0].instrument.product.value,
                        },
                        "outcome_buckets": tuple(
                            {
                                "bucket_id": bucket.bucket_id,
                                "lower_bps": bucket.lower_bps,
                                "upper_bps": bucket.upper_bps,
                                "representative_bps": bucket.representative_bps,
                            }
                            for bucket in item.contract.outcome_buckets
                        ),
                        "forecast_benchmark": tuple(
                            {
                                "bucket_id": bucket.bucket_id,
                                "probability": bucket.probability,
                            }
                            for bucket in item.contract.forecast_benchmark
                        ),
                    },
                    "target_state": {
                        "as_of": item.target_state.as_of,
                        "asset_states": tuple(
                            _compact_asset_state(state) for state in item.target_state.asset_states
                        ),
                        "derivative_states": tuple(
                            _compact_derivative_state(state)
                            for state in item.target_state.derivative_states
                        ),
                        "comparison_states": tuple(
                            _compact_comparison_state(state)
                            for state in item.target_state.comparison_states
                        ),
                        "missing_comparison_instrument_keys": (
                            item.target_state.missing_comparison_instrument_keys
                        ),
                    },
                }
                for item in targets
            ),
            "world_model": context_forecast_world_model_projection(
                assessment,
                packet=packet,
            ),
        },
        source_refs_by_slot={},
    )


def context_forecast_world_model_projection(
    assessment: ContextAssessment,
    *,
    packet: DecisionPacket,
) -> dict[str, object]:
    """Keep causal decision content; exclude WorldModel maintenance metadata."""

    structural_evidence_ids = _structural_evidence_ids(packet)

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
            }
            for item in assessment.event_references
        ),
        "mechanisms": tuple(
            {
                "mechanism_id": item.mechanism_id,
                "relationship": item.relationship,
                "claim": item.claim,
                "horizon_hours": item.horizon_hours,
                "transmission_stage": item.transmission_stage,
                "evidence_ids": tuple(
                    sorted(
                        {
                            evidence_id
                            for node in item.causal_chain
                            for evidence_id in node.evidence_ids
                        }
                    )
                ),
                "conflicting_evidence_ids": item.conflicting_evidence_ids,
                "structural_evidence_ids": tuple(
                    sorted(
                        {
                            evidence_id
                            for node in item.causal_chain
                            for evidence_id in node.evidence_ids
                            if evidence_id in structural_evidence_ids
                        }
                    )
                ),
            }
            for item in assessment.mechanisms
        ),
    }


def _structural_evidence_ids(packet: DecisionPacket) -> frozenset[str]:
    """Separate external causal evidence from repeatable market-state observations."""

    eligible_events = {
        item.evidence_ref
        for item in packet.intelligence_events
        if item.directional_support_eligible
    }
    evidence = {
        *(item.revision_id for item in packet.facts),
        *eligible_events,
    }
    evidence.update(
        item.delta_id
        for item in packet.deltas
        if item.fact_revision_ids or bool(set(item.intelligence_event_refs) & eligible_events)
    )
    if packet.previous_context is not None:
        evidence.update(
            item.evidence_id
            for item in packet.previous_context.event_references
            if item.impact_state == "ACTIVE"
        )
    return frozenset(evidence)


def _compact_asset_state(state: PacketAssetState) -> dict[str, object]:
    return {
        "asset": state.asset,
        "observed_at": state.observed_at,
        "return_fraction": state.return_fraction,
        "realized_volatility": state.realized_volatility,
        "regime": state.regime,
        "spread_bps": state.spread_bps,
        "volume_ratio": state.volume_ratio,
    }


def _compact_derivative_state(state: PacketDerivativeState) -> dict[str, object]:
    fields = (
        "asset",
        "observed_at",
        "mark_index_premium_bps",
        "executable_short_basis_bps",
        "perpetual_spread_bps",
        "last_funding_rate_bps",
        "trailing_funding_rate_mean_bps",
        "trailing_funding_rate_stddev_bps",
        "open_interest_change_fraction",
        "global_long_account_fraction",
        "taker_buy_sell_ratio",
        "spot_taker_buy_sell_ratio",
        "reference_spot_mid_deviation_bps",
    )
    return {name: value for name in fields if (value := getattr(state, name)) is not None}


def _compact_comparison_state(
    state: ContextForecastComparisonState,
) -> dict[str, object]:
    return {
        "symbol": state.instrument.symbol,
        "product": state.instrument.product.value,
        "observed_at": state.observed_at,
        "index_price": state.index_price,
        "comparison_price_multiplier": state.comparison_price_multiplier,
        "target_reference_deviation_bps": state.target_reference_deviation_bps,
        "mark_index_premium_bps": state.mark_index_premium_bps,
        "market_session": state.market_session,
    }


def context_forecast_output_schema_for_ids(
    *,
    decision_slot_ids: tuple[str, ...],
    bucket_ids: tuple[str, ...],
    mechanism_ids: tuple[str, ...],
    evidence_ids: tuple[str, ...],
    output_model_schema: dict[str, object] | None = None,
    draft_definition: str = "ContextForecastDraft",
) -> dict[str, object]:
    """Build the shared schema without exposing WorldModel semantic content."""

    if not decision_slot_ids or not bucket_ids or not mechanism_ids or not evidence_ids:
        raise ValueError("Context Forecast 输出枚举不能为空")
    if any(
        len(set(values)) != len(values)
        for values in (decision_slot_ids, bucket_ids, mechanism_ids, evidence_ids)
    ):
        raise ValueError("Context Forecast 输出枚举不能重复")
    schema = strict_output_schema(
        ContextForecastStructuredOutput.model_json_schema()
        if output_model_schema is None
        else output_model_schema
    )
    definitions = schema["$defs"]
    draft = definitions[draft_definition]
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
