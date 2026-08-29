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
POSTERIOR_OUTPUT_VERSION = "world-model-posterior-output-v1"
POSTERIOR_PRODUCER_ID = "world-model-posterior"


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
        if self.world_model.available_at > cutoff:
            raise ValueError("Posterior 不得读取信息截止后的 WorldModel")
        if min(item.next_review_at for item in self.world_model.mechanisms) <= cutoff:
            raise ValueError("Posterior WorldModel 在信息截止前已到期复核")
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
    "没有可归因的结构增量时必须逐桶保持 prior 原值，mechanism_contributions 留空。",
    "每个 bucket 必须给出简洁中文 rationale，明确相对 prior 是上调、下调还是不变及原因。"
    "发生任何概率变化时，至少引用一个 eligible mechanism，并说明传导与反向证据；"
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
    if not value.eligible_mechanism_ids:
        definitions["PosteriorTargetDraft"]["properties"]["mechanism_contributions"]["maxItems"] = 0
    return schema


def build_posterior_prompt(value: ContextPosteriorInput) -> str:
    return "\n".join(
        (
            *POSTERIOR_INSTRUCTIONS,
            "posterior_input_json=",
            canonical_json(value),
        )
    )


def posterior_behavior_hash(
    runtime: CodexRuntimePolicy,
    *,
    contracts: tuple[ForecastContract, ...],
) -> str:
    return content_hash(
        {
            "input_version": POSTERIOR_INPUT_VERSION,
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
        if changed and not contribution_ids:
            raise ValueError("Posterior 偏离 prior 时必须绑定结构机制")
        if not changed and contribution_ids:
            raise ValueError("Posterior 未偏离 prior 时不得伪造机制贡献")
        if not set(contribution_ids) <= eligible:
            raise ValueError("Posterior 引用了没有结构资格的机制")
        if any(
            contribution.effect == ForecastMechanismEffect.NO_MATERIAL_EFFECT
            for contribution in draft.mechanism_contributions
        ):
            raise ValueError("实质调整不得使用 NO_MATERIAL_EFFECT 机制")
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
                expected_gross_bps=sum(
                    (
                        probability.probability * bucket.representative_bps
                        for probability, bucket in zip(
                            distribution,
                            item.contract.outcome_buckets,
                            strict=True,
                        )
                    ),
                    Decimal("0"),
                ),
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
