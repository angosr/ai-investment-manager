"""Frozen contract for one joint WorldModel-conditioned Forecast call."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, field_validator, model_validator

from investment_manager.forecast.codex.output import strict_output_schema
from investment_manager.forecast.codex.protocol import codex_execution_contract
from investment_manager.forecast.contracts import (
    ForecastContract,
    ForecastDecisionSlot,
    ForecastPriceAnchor,
    ForecastProducerBinding,
)
from investment_manager.forecast.models import (
    ContextAssessment,
    ContextMechanismObservation,
)
from investment_manager.forecast.policy import CodexRuntimePolicy
from investment_manager.forecast.results import (
    BaseForecast,
    ForecastBucketProbability,
    ForecastMechanismContribution,
    ForecastMechanismEffect,
)
from investment_manager.kernel.identity import canonical_json, content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel

POSTERIOR_INPUT_VERSION = "world-model-posterior-input-v1"
POSTERIOR_MODEL_INPUT_VERSION = "world-model-posterior-projection-v2"
POSTERIOR_SEED_VERSION = "world-model-posterior-seed-v1"
POSTERIOR_OUTPUT_VERSION = "world-model-posterior-output-v1"
POSTERIOR_PRODUCER_ID = "world-model-posterior"
POSTERIOR_PRODUCTION_SEMANTICS_VERSION = "same-cutoff-structural-conditioning-v3"


class PosteriorPriorTarget(FrozenModel):
    contract: ForecastContract
    slot: ForecastDecisionSlot
    prior: BaseForecast

    @model_validator(mode="after")
    def identities_and_timing_match(self):
        if not (self.contract.contract_id == self.slot.contract_id == self.prior.contract_id):
            raise ValueError("Posterior target 的 Contract 身份不一致")
        if self.prior.decision_slot_id != self.slot.slot_id:
            raise ValueError("Posterior target 的 prior 不属于同一决策槽")
        if self.prior.information_cutoff_at != self.slot.information_cutoff_at:
            raise ValueError("Posterior target 的信息截止不一致")
        if self.prior.available_at > self.slot.completion_deadline_at:
            raise ValueError("Posterior target 的 prior 已错过完成期限")
        return self


class ContextPosteriorSeed(FrozenModel):
    """Prior-side facts reserved at the slot boundary before WorldModel generation."""

    schema_version: Literal["world-model-posterior-seed-v1"] = POSTERIOR_SEED_VERSION
    seed_id: str = Field(min_length=1)
    seed_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    information_cutoff_at: datetime
    targets: tuple[PosteriorPriorTarget, ...] = Field(min_length=1)

    _utc_information_cutoff_at = field_validator("information_cutoff_at")(require_utc)

    @classmethod
    def create(cls, **values) -> ContextPosteriorSeed:
        normalized = {
            "information_cutoff_at": require_utc(values["information_cutoff_at"]),
            "targets": tuple(sorted(values["targets"], key=lambda item: item.contract.contract_id)),
        }
        pending = cls.model_construct(
            schema_version=POSTERIOR_SEED_VERSION,
            seed_id="pending",
            seed_hash="0" * 64,
            **normalized,
        )
        digest = content_hash(pending.model_dump(mode="json", exclude={"seed_id", "seed_hash"}))
        return cls(
            seed_id=stable_id("context_posterior_seed", digest),
            seed_hash=digest,
            **normalized,
        )

    @model_validator(mode="after")
    def identity_scope_and_timing_are_canonical(self):
        contract_ids = tuple(item.contract.contract_id for item in self.targets)
        if tuple(sorted(set(contract_ids))) != contract_ids:
            raise ValueError("Posterior seed targets 必须按唯一 Contract 排序")
        if any(
            item.slot.information_cutoff_at != self.information_cutoff_at
            for item in self.targets
        ):
            raise ValueError("Posterior seed targets 必须共享信息截止")
        expected_hash = content_hash(
            self.model_dump(mode="json", exclude={"seed_id", "seed_hash"})
        )
        if self.seed_hash != expected_hash:
            raise ValueError("Posterior seed_hash 与内容不一致")
        if self.seed_id != stable_id("context_posterior_seed", expected_hash):
            raise ValueError("Posterior seed_id 与内容不一致")
        return self


class ContextPosteriorInput(FrozenModel):
    schema_version: Literal["world-model-posterior-input-v1"] = POSTERIOR_INPUT_VERSION
    input_id: str = Field(min_length=1)
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    information_cutoff_at: datetime
    world_model: ContextAssessment
    mechanism_observations: tuple[ContextMechanismObservation, ...] = ()
    eligible_mechanism_ids: tuple[str, ...] = ()
    targets: tuple[PosteriorPriorTarget, ...] = Field(min_length=1)

    _utc_information_cutoff_at = field_validator("information_cutoff_at")(require_utc)

    @classmethod
    def create(cls, **values) -> ContextPosteriorInput:
        normalized = {
            **values,
            "information_cutoff_at": require_utc(values["information_cutoff_at"]),
            "mechanism_observations": tuple(
                sorted(
                    values.get("mechanism_observations", ()),
                    key=lambda item: (item.observed_at, item.observation_id),
                )
            ),
            "eligible_mechanism_ids": tuple(sorted(set(values.get("eligible_mechanism_ids", ())))),
            "targets": tuple(sorted(values["targets"], key=lambda item: item.contract.contract_id)),
        }
        pending = cls.model_construct(
            schema_version=POSTERIOR_INPUT_VERSION,
            input_id="pending",
            input_hash="0" * 64,
            **normalized,
        )
        digest = content_hash(pending.model_dump(mode="json", exclude={"input_id", "input_hash"}))
        return cls(
            input_id=stable_id("context_posterior_input", digest),
            input_hash=digest,
            **normalized,
        )

    @model_validator(mode="after")
    def identity_scope_and_timing_are_canonical(self):
        cutoff = self.information_cutoff_at
        if self.world_model.as_of != cutoff:
            raise ValueError("Posterior 必须读取同一信息截止新生成的 WorldModel")
        deadline = min(item.slot.completion_deadline_at for item in self.targets)
        if self.world_model.available_at > deadline:
            raise ValueError("Posterior WorldModel 在 Forecast 完成期限后才可用")
        contract_ids = tuple(item.contract.contract_id for item in self.targets)
        if tuple(sorted(set(contract_ids))) != contract_ids:
            raise ValueError("Posterior targets 必须按唯一 Contract 排序")
        if any(item.slot.information_cutoff_at != cutoff for item in self.targets):
            raise ValueError("联合 Posterior targets 必须共享信息截止")
        mechanism_ids = {item.mechanism_id for item in self.world_model.mechanisms}
        if not set(self.eligible_mechanism_ids) <= mechanism_ids:
            raise ValueError("Posterior eligible mechanism 不属于冻结 WorldModel")
        if tuple(sorted(set(self.eligible_mechanism_ids))) != self.eligible_mechanism_ids:
            raise ValueError("Posterior eligible mechanism 必须唯一且排序")
        if any(
            item.assessment_id != self.world_model.assessment_id or item.observed_at > cutoff
            for item in self.mechanism_observations
        ):
            raise ValueError("Posterior mechanism observation 越过冻结范围")
        expected_hash = content_hash(
            self.model_dump(mode="json", exclude={"input_id", "input_hash"})
        )
        if self.input_hash != expected_hash:
            raise ValueError("Posterior input_hash 与内容不一致")
        if self.input_id != stable_id("context_posterior_input", expected_hash):
            raise ValueError("Posterior input_id 与内容不一致")
        return self


class PosteriorBucketDraft(FrozenModel):
    bucket_id: str = Field(min_length=1, max_length=64)
    probability: Decimal = Field(ge=0, le=1)
    rationale: str = Field(min_length=1, max_length=400)


class PosteriorTargetDraft(FrozenModel):
    contract_id: str = Field(min_length=1)
    buckets: tuple[PosteriorBucketDraft, ...] = Field(min_length=3)
    mechanism_contributions: tuple[ForecastMechanismContribution, ...] = ()

    @model_validator(mode="after")
    def distribution_and_contributions_are_canonical(self):
        bucket_ids = tuple(item.bucket_id for item in self.buckets)
        if len(set(bucket_ids)) != len(bucket_ids):
            raise ValueError("Posterior bucket 不得重复")
        if sum((item.probability for item in self.buckets), Decimal("0")) != 1:
            raise ValueError("Posterior 概率之和必须为 1")
        mechanism_ids = tuple(item.mechanism_id for item in self.mechanism_contributions)
        if len(set(mechanism_ids)) != len(mechanism_ids):
            raise ValueError("Posterior mechanism contribution 不得重复")
        return self


class ContextPosteriorStructuredOutput(FrozenModel):
    schema_version: Literal["world-model-posterior-output-v1"] = POSTERIOR_OUTPUT_VERSION
    forecasts: tuple[PosteriorTargetDraft, ...] = Field(min_length=1)


POSTERIOR_INSTRUCTIONS = (
    "你是组合级概率预测员。只能读取 posterior_input_json 中冻结的 prior 与 WorldModel，"
    "为全部 targets 一次性输出同一信息截止下的 72 小时收益桶概率；不得输出订单、仓位、"
    "杠杆、交易频率、成本判断或数据建设建议。",
    "prior 是唯一统计起点。只有 eligible_mechanism_ids 中的结构机制能够实质改变 prior；"
    "行情、技术状态、funding、basis、持仓量或价格响应本身不得成为第二套方向信号。"
    "没有可归因的结构增量时必须逐桶保持 prior 原值。",
    "每个 target 必须按 eligible_mechanism_ids 的顺序逐项输出 mechanism_contributions，"
    "明确该机制对本资产、本期限是 UPSIDE、DOWNSIDE、UNCERTAINTY 或 NO_MATERIAL_EFFECT；"
    "没有 eligible mechanism 时该列表必须为空。已经由事件前后预期或政策路径变化以及利率、"
    "美元、信用或流动性响应确认的机制，必须体现其有符号影响，不能仅因慢变量缓冲仍存在而降格为"
    "无实质影响；存在抵消力量时必须分别归因。",
    "每个 bucket 必须给出简洁中文 rationale，明确相对 prior 是上调、下调还是不变及原因。"
    "概率变化必须与机制净方向一致：单边 UPSIDE 不得下调期望收益，单边 DOWNSIDE 不得上调，"
    "只有 UNCERTAINTY 时必须扩大分布离散度；全部为 NO_MATERIAL_EFFECT 时必须保持 prior。"
    "不得为了显得有判断而强行偏离 prior。",
    "所有 contract_id、bucket_id 和 mechanism_id 必须逐字来自输入。概率必须为 0 到 1，"
    "每个 target 的概率和必须精确等于 1。只输出 Schema 要求的 JSON。",
)


def posterior_output_schema(value: ContextPosteriorInput) -> dict[str, object]:
    schema = strict_output_schema(ContextPosteriorStructuredOutput.model_json_schema())
    definitions = schema["$defs"]
    definitions["PosteriorTargetDraft"]["properties"]["contract_id"]["enum"] = [
        item.contract.contract_id for item in value.targets
    ]
    definitions["PosteriorBucketDraft"]["properties"]["bucket_id"]["enum"] = sorted(
        {outcome.bucket_id for item in value.targets for outcome in item.contract.outcome_buckets}
    )
    contribution = definitions["ForecastMechanismContribution"]
    contribution["properties"]["mechanism_id"]["enum"] = list(
        value.eligible_mechanism_ids or ("NO_ELIGIBLE_MECHANISM",)
    )
    contribution_list = definitions["PosteriorTargetDraft"]["properties"]["mechanism_contributions"]
    contribution_list["minItems"] = len(value.eligible_mechanism_ids)
    contribution_list["maxItems"] = len(value.eligible_mechanism_ids)
    return schema


def build_posterior_prompt(value: ContextPosteriorInput) -> str:
    return "\n".join(
        (
            *POSTERIOR_INSTRUCTIONS,
            "posterior_input_json=",
            canonical_json(posterior_analysis_projection(value)),
        )
    )


def posterior_analysis_projection(value: ContextPosteriorInput) -> dict[str, object]:
    """Decision-dense model view; the complete immutable input stays auditable.

    Contracts, slots, quotes and program-input lineage are required to freeze
    identity and settle outcomes, but repeating them to the model dilutes the
    only judgment it owns: whether named structural mechanisms should move the
    prior distribution.  The projection therefore carries each semantic fact
    exactly once and never truncates a mechanism or target opportunistically.
    """

    observations_by_mechanism: dict[str, list[dict[str, object]]] = {}
    for observation in value.mechanism_observations:
        observations_by_mechanism.setdefault(observation.mechanism_id, []).append(
            {
                "feature": observation.feature_selector,
                "value": observation.value,
                "match": observation.match,
                "support_streak": observation.support_streak,
                "contradiction_streak": observation.contradiction_streak,
                "resolution": observation.resolution,
                "observed_at": observation.observed_at,
            }
        )
    eligible = set(value.eligible_mechanism_ids)
    mechanisms = tuple(
        {
            "mechanism_id": mechanism.mechanism_id,
            "relationship": mechanism.relationship,
            "claim": mechanism.claim,
            "horizon_hours": mechanism.horizon_hours,
            "transmission_stage": mechanism.transmission_stage,
            "causal_chain": tuple(node.statement for node in mechanism.causal_chain),
            "observations": tuple(
                observations_by_mechanism.get(mechanism.mechanism_id, ())
            ),
        }
        for mechanism in value.world_model.mechanisms
        if mechanism.mechanism_id in eligible
    )
    targets = tuple(
        {
            "contract_id": target.contract.contract_id,
            "target": tuple(
                {
                    "instrument_id": leg.instrument.key,
                    "direction": leg.direction,
                    "gross_weight": leg.gross_weight,
                }
                for leg in target.contract.target.legs
            ),
            "horizon_minutes": target.contract.horizon_minutes,
            "prior_expected_gross_bps": target.prior.expected_gross_bps,
            "buckets": tuple(
                {
                    "bucket_id": bucket.bucket_id,
                    "lower_bps": bucket.lower_bps,
                    "upper_bps": bucket.upper_bps,
                    "representative_bps": bucket.representative_bps,
                    "prior_probability": probability.probability,
                }
                for bucket, probability in zip(
                    target.contract.outcome_buckets,
                    target.prior.outcome_probabilities,
                    strict=True,
                )
            ),
        }
        for target in value.targets
    )
    return {
        "projection_version": POSTERIOR_MODEL_INPUT_VERSION,
        "input_id": value.input_id,
        "information_cutoff_at": value.information_cutoff_at,
        "world_model": {
            "assessment_id": value.world_model.assessment_id,
            "synthesis": value.world_model.synthesis,
            "synthesis_horizon_hours": value.world_model.synthesis_horizon_hours,
            "active_events": tuple(
                {
                    "title": event.title,
                    "rationale": event.rationale,
                }
                for event in value.world_model.event_references
                if event.impact_state.value == "ACTIVE"
            ),
            "eligible_mechanisms": mechanisms,
        },
        "eligible_mechanism_ids": value.eligible_mechanism_ids,
        "targets": targets,
    }


def posterior_behavior_hash(
    runtime: CodexRuntimePolicy,
    *,
    contracts: tuple[ForecastContract, ...],
    prior_bindings: tuple[ForecastProducerBinding, ...],
    world_model_behavior_id: str,
) -> str:
    if len(world_model_behavior_id) != 64:
        raise ValueError("Posterior 缺少完整 WorldModel 行为身份")
    return content_hash(
        {
            "input_version": POSTERIOR_INPUT_VERSION,
            "model_input_version": POSTERIOR_MODEL_INPUT_VERSION,
            "output_version": POSTERIOR_OUTPUT_VERSION,
            "instructions": POSTERIOR_INSTRUCTIONS,
            "input_schema": ContextPosteriorInput.model_json_schema(),
            "output_schema": strict_output_schema(
                ContextPosteriorStructuredOutput.model_json_schema()
            ),
            "contracts": tuple(
                (item.contract_id, tuple(bucket.bucket_id for bucket in item.outcome_buckets))
                for item in sorted(contracts, key=lambda item: item.contract_id)
            ),
            "prior_bindings": tuple(
                item.model_dump(mode="json")
                for item in sorted(prior_bindings, key=lambda item: item.contract_id)
            ),
            "world_model_behavior_id": world_model_behavior_id,
            "production_semantics_version": POSTERIOR_PRODUCTION_SEMANTICS_VERSION,
            "execution_contract": codex_execution_contract(),
            "runtime_policy_version": runtime.version,
            "expected_cli_version": runtime.expected_cli_version,
            "expected_binary_sha256": runtime.expected_binary_sha256,
            "model": runtime.model,
            "reasoning_effort": runtime.reasoning_effort,
            "maximum_schema_attempts": 1 + runtime.max_account_switches,
        }
    )


def finalize_posterior(
    *,
    output: ContextPosteriorStructuredOutput,
    frozen_input: ContextPosteriorInput,
    producer_behavior_id: str,
    completed_at: datetime,
    entry_anchors: dict[str, tuple[ForecastPriceAnchor, ...]],
) -> tuple[BaseForecast, ...]:
    completed = require_utc(completed_at)
    drafts = {item.contract_id: item for item in output.forecasts}
    expected_ids = tuple(item.contract.contract_id for item in frozen_input.targets)
    if tuple(sorted(drafts)) != expected_ids or len(drafts) != len(output.forecasts):
        raise ValueError("Posterior 输出必须完整且唯一覆盖冻结 targets")
    mechanisms = {item.mechanism_id: item for item in frozen_input.world_model.mechanisms}
    eligible = set(frozen_input.eligible_mechanism_ids)
    input_json = canonical_json(frozen_input)
    output_json = canonical_json(output)
    forecasts = []
    for item in frozen_input.targets:
        if completed > item.slot.completion_deadline_at:
            raise ValueError("Posterior 在完成期限之后返回")
        draft = drafts[item.contract.contract_id]
        bucket_ids = tuple(bucket.bucket_id for bucket in item.contract.outcome_buckets)
        if tuple(bucket.bucket_id for bucket in draft.buckets) != bucket_ids:
            raise ValueError("Posterior bucket 必须按合同顺序完整覆盖")
        prior_probabilities = tuple(
            probability.probability for probability in item.prior.outcome_probabilities
        )
        posterior_probabilities = tuple(bucket.probability for bucket in draft.buckets)
        changed = posterior_probabilities != prior_probabilities
        contribution_ids = tuple(
            contribution.mechanism_id for contribution in draft.mechanism_contributions
        )
        if changed and not eligible:
            raise ValueError("Posterior 偏离 prior 时必须绑定结构机制")
        if contribution_ids != frozen_input.eligible_mechanism_ids:
            raise ValueError("Posterior 必须按顺序逐项评估全部 eligible mechanism")
        effects = tuple(contribution.effect for contribution in draft.mechanism_contributions)
        material_effects = {
            effect for effect in effects if effect != ForecastMechanismEffect.NO_MATERIAL_EFFECT
        }
        if changed and not material_effects:
            raise ValueError("Posterior 全部为 NO_MATERIAL_EFFECT 时必须保持 prior")
        if (
            not changed
            and material_effects
            and not {
                ForecastMechanismEffect.UPSIDE,
                ForecastMechanismEffect.DOWNSIDE,
            }
            <= material_effects
        ):
            raise ValueError("Posterior 未变化时只能保留明确相互抵消的方向机制")
        prior_expected = sum(
            (
                probability * bucket.representative_bps
                for probability, bucket in zip(
                    prior_probabilities,
                    item.contract.outcome_buckets,
                    strict=True,
                )
            ),
            Decimal("0"),
        )
        posterior_expected = sum(
            (
                probability * bucket.representative_bps
                for probability, bucket in zip(
                    posterior_probabilities,
                    item.contract.outcome_buckets,
                    strict=True,
                )
            ),
            Decimal("0"),
        )
        expected_delta = posterior_expected - prior_expected
        directional_effects = material_effects & {
            ForecastMechanismEffect.UPSIDE,
            ForecastMechanismEffect.DOWNSIDE,
        }
        if directional_effects == {ForecastMechanismEffect.UPSIDE} and expected_delta <= 0:
            raise ValueError("Posterior 单边 UPSIDE 机制必须上调期望收益")
        if directional_effects == {ForecastMechanismEffect.DOWNSIDE} and expected_delta >= 0:
            raise ValueError("Posterior 单边 DOWNSIDE 机制必须下调期望收益")
        if (
            changed
            and not directional_effects
            and material_effects == {ForecastMechanismEffect.UNCERTAINTY}
        ):
            prior_variance = sum(
                (
                    probability * (bucket.representative_bps - prior_expected) ** 2
                    for probability, bucket in zip(
                        prior_probabilities,
                        item.contract.outcome_buckets,
                        strict=True,
                    )
                ),
                Decimal("0"),
            )
            posterior_variance = sum(
                (
                    probability * (bucket.representative_bps - posterior_expected) ** 2
                    for probability, bucket in zip(
                        posterior_probabilities,
                        item.contract.outcome_buckets,
                        strict=True,
                    )
                ),
                Decimal("0"),
            )
            if posterior_variance <= prior_variance:
                raise ValueError("Posterior 单独 UNCERTAINTY 机制必须扩大分布离散度")
        anchors = entry_anchors.get(item.contract.contract_id, ())
        if tuple(anchor.instrument_id for anchor in anchors) != tuple(
            leg.instrument.key for leg in item.contract.target.legs
        ):
            raise ValueError("Posterior entry anchors 未覆盖合同 Target")
        evidence = tuple(
            sorted(
                {
                    evidence_id
                    for mechanism_id in contribution_ids
                    for mechanism in (mechanisms[mechanism_id],)
                    for evidence_id in (
                        *(ref for node in mechanism.causal_chain for ref in node.evidence_ids),
                        *mechanism.conflicting_evidence_ids,
                    )
                }
            )
        )
        invalidations = tuple(
            sorted(
                {
                    condition
                    for mechanism_id in contribution_ids
                    for condition in mechanisms[mechanism_id].invalidation_conditions
                }
            )
        )
        distribution = tuple(
            ForecastBucketProbability(
                bucket_id=bucket.bucket_id,
                probability=bucket.probability,
            )
            for bucket in draft.buckets
        )
        forecasts.append(
            BaseForecast(
                forecast_id=stable_id(
                    "base_forecast",
                    item.slot.slot_id,
                    producer_behavior_id,
                ),
                contract_id=item.contract.contract_id,
                decision_slot_id=item.slot.slot_id,
                producer_id=POSTERIOR_PRODUCER_ID,
                producer_behavior_id=producer_behavior_id,
                outcome_family_id=item.contract.outcome_family_id,
                target=item.contract.target,
                horizon_minutes=item.contract.horizon_minutes,
                cutoff_prices=item.slot.cutoff_prices,
                entry_prices=anchors,
                information_cutoff_at=item.slot.information_cutoff_at,
                input_observed_at=item.slot.information_cutoff_at,
                available_at=completed,
                valid_until=item.slot.evaluation_at,
                outcome_probabilities=distribution,
                expected_gross_bps=posterior_expected,
                input_refs=tuple(
                    sorted(
                        {
                            frozen_input.input_id,
                            item.prior.forecast_id,
                            frozen_input.world_model.assessment_id,
                            *(anchor.quote_ref for anchor in item.slot.cutoff_prices),
                            *evidence,
                        }
                    )
                ),
                world_model_id=frozen_input.world_model.assessment_id,
                mechanism_contributions=draft.mechanism_contributions,
                evidence_refs=evidence,
                invalidation_conditions=invalidations,
                analysis_input_json=input_json,
                analysis_input_hash=content_hash(frozen_input),
                analysis_output_json=output_json,
                analysis_output_hash=content_hash(output),
            )
        )
    return tuple(forecasts)
