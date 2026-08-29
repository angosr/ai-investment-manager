from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, TypeAdapter, field_validator, model_validator

from investment_manager.forecast.models import ForecastTarget
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import optional_utc, require_utc
from investment_manager.kernel.types import FrozenModel, PositiveDecimal
from investment_manager.market.models import InstrumentProduct


class ForecastOrientation(StrEnum):
    CANONICAL = "CANONICAL"
    INVERSE = "INVERSE"


class ForecastProducerKind(StrEnum):
    PROGRAM = "PROGRAM"
    CONTEXT = "CONTEXT"


class ForecastPermission(StrEnum):
    RESEARCH = "RESEARCH"
    CAPITAL_CANDIDATE = "CAPITAL_CANDIDATE"

class ForecastNoEstimateReason(StrEnum):
    MARKET_INPUT_INVALID = "MARKET_INPUT_INVALID"
    REQUIRED_FEATURE_MISSING = "REQUIRED_FEATURE_MISSING"
    WORLD_MODEL_UNAVAILABLE = "WORLD_MODEL_UNAVAILABLE"
    WORLD_MODEL_STALE = "WORLD_MODEL_STALE"
    PRODUCER_FAILED = "PRODUCER_FAILED"
    DEADLINE_MISSED = "DEADLINE_MISSED"
    STALE_BEFORE_AVAILABLE = "STALE_BEFORE_AVAILABLE"
    INSUFFICIENT_REMAINING_HORIZON = "INSUFFICIENT_REMAINING_HORIZON"


class ForecastOutcomeBucket(FrozenModel):
    bucket_id: str = Field(min_length=1, max_length=64)
    lower_bps: Decimal | None = None
    upper_bps: Decimal | None = None
    representative_bps: Decimal

    @model_validator(mode="after")
    def bounds_and_representative_are_consistent(self):
        if (
            self.lower_bps is not None
            and self.upper_bps is not None
            and self.lower_bps >= self.upper_bps
        ):
            raise ValueError("Forecast bucket 下界必须小于上界")
        if self.lower_bps is not None and self.representative_bps < self.lower_bps:
            raise ValueError("Forecast bucket 代表收益不能低于下界")
        if self.upper_bps is not None and self.representative_bps >= self.upper_bps:
            raise ValueError("Forecast bucket 代表收益必须低于上界")
        return self


class ForecastBenchmarkProbability(FrozenModel):
    bucket_id: str = Field(min_length=1, max_length=64)
    probability: Decimal = Field(ge=0, le=1)


class ForecastPriceAnchor(FrozenModel):
    instrument_id: str = Field(min_length=1)
    price: PositiveDecimal
    observed_at: datetime
    available_at: datetime
    quote_ref: str = Field(min_length=1)

    _utc_observed_at = field_validator("observed_at")(require_utc)
    _utc_available_at = field_validator("available_at")(require_utc)

    @model_validator(mode="after")
    def market_time_cannot_follow_local_availability(self):
        if self.observed_at > self.available_at:
            raise ValueError("ForecastPriceAnchor 市场时间不能晚于本地可见时间")
        return self


class ForecastContract(FrozenModel):
    """Source-independent economic question and settlement contract."""

    contract_id: str = Field(min_length=1)
    contract_version: str = Field(min_length=1)
    outcome_family_id: str = Field(min_length=1)
    target: ForecastTarget
    allowed_orientations: tuple[ForecastOrientation, ...] = (ForecastOrientation.CANONICAL,)
    outcome_buckets: tuple[ForecastOutcomeBucket, ...] = Field(min_length=3)
    horizon_minutes: int = Field(gt=0)
    decision_slot_rule: str = Field(min_length=1)
    evaluation_trigger: str = Field(min_length=1)
    information_cutoff_rule: str = Field(min_length=1)
    completion_deadline_seconds: int = Field(gt=0)
    minimum_remaining_horizon_minutes: int = Field(gt=0)
    entry_anchor_rule: str = Field(min_length=1)
    cost_semantics_version: str = Field(min_length=1)
    validity_minutes: int = Field(gt=0)
    validity_conditions: tuple[str, ...] = Field(min_length=1)
    settlement_rule: str = Field(min_length=1)
    outcome_start_delay_seconds: int | None = Field(default=None, ge=0)
    forecast_benchmark: tuple[ForecastBenchmarkProbability, ...] = Field(min_length=3)
    decision_benchmark: str = Field(min_length=1)

    @model_validator(mode="after")
    def identity_distribution_and_timing_are_canonical(self):
        if tuple(sorted(set(self.allowed_orientations), key=lambda item: item.value)) != tuple(
            sorted(self.allowed_orientations, key=lambda item: item.value)
        ):
            raise ValueError("ForecastContract orientation 必须唯一")
        if ForecastOrientation.CANONICAL not in self.allowed_orientations:
            raise ValueError("ForecastContract 必须允许规范方向")
        if self.minimum_remaining_horizon_minutes >= self.horizon_minutes:
            raise ValueError("最小剩余可交易时长必须短于 Forecast horizon")
        if (
            self.completion_deadline_seconds
            >= (self.horizon_minutes - self.minimum_remaining_horizon_minutes) * 60
        ):
            raise ValueError("Forecast 完成期限没有留下最小可交易时长")
        if self.outcome_start_delay_seconds is not None and (
            self.completion_deadline_seconds + self.outcome_start_delay_seconds
            >= self.horizon_minutes * 60
        ):
            raise ValueError("Forecast Outcome 起点必须早于评价时点")
        if self.settlement_rule == "spot-midpoint-return-v1" and (
            self.outcome_start_delay_seconds is not None
            or any(
                leg.instrument.product != InstrumentProduct.SPOT
                for leg in self.target.legs
            )
        ):
            raise ValueError("Spot midpoint Outcome 只允许截止点起算的 Spot Target")
        bucket_ids = tuple(item.bucket_id for item in self.outcome_buckets)
        if len(set(bucket_ids)) != len(bucket_ids):
            raise ValueError("Forecast bucket_id 必须唯一")
        if self.outcome_buckets[0].lower_bps is not None:
            raise ValueError("Forecast 首个 bucket 必须覆盖负无穷尾部")
        if self.outcome_buckets[-1].upper_bps is not None:
            raise ValueError("Forecast 最后 bucket 必须覆盖正无穷尾部")
        for previous, current in zip(
            self.outcome_buckets,
            self.outcome_buckets[1:],
            strict=False,
        ):
            if previous.upper_bps != current.lower_bps:
                raise ValueError("Forecast buckets 必须连续且不重叠")
        benchmark_ids = tuple(item.bucket_id for item in self.forecast_benchmark)
        if benchmark_ids != bucket_ids:
            raise ValueError("Forecast benchmark 必须按 bucket 顺序完整覆盖")
        if sum((item.probability for item in self.forecast_benchmark), Decimal("0")) != 1:
            raise ValueError("Forecast benchmark 概率之和必须为 1")
        expected_id = self.identity_for(self.model_dump(mode="json", exclude={"contract_id"}))
        if self.contract_id != expected_id:
            raise ValueError("ForecastContract contract_id 与内容不一致")
        return self

    @staticmethod
    def identity_for(payload: dict[str, object]) -> str:
        normalized = dict(payload)
        # Preserve the immutable identity of pre-anchor contracts.  Missing/None
        # means the historical cutoff-based diagnostic semantics.
        if normalized.get("outcome_start_delay_seconds") is None:
            normalized.pop("outcome_start_delay_seconds", None)
        return stable_id("forecast_contract", content_hash(normalized))

    @property
    def permission_evidence_eligible(self) -> bool:
        return self.outcome_start_delay_seconds is not None

    @classmethod
    def create(cls, **values) -> ForecastContract:
        normalized = {}
        for name, field in cls.model_fields.items():
            if name == "contract_id":
                continue
            raw = values[name] if name in values else field.get_default(call_default_factory=True)
            normalized[name] = TypeAdapter(field.annotation).validate_python(raw)
        payload = cls.model_construct(contract_id="pending", **normalized).model_dump(
            mode="json",
            exclude={"contract_id"},
        )
        return cls(contract_id=cls.identity_for(payload), **normalized)


class ForecastSlotOrigin(StrEnum):
    CADENCE = "CADENCE"
    MATERIAL_STATE = "MATERIAL_STATE"


MATERIAL_SLOT_POLICY_VERSION = "qualified-structural-event-v4"


class ForecastSlotStratum(StrEnum):
    CADENCE_ONLY = "CADENCE_ONLY"
    MATERIAL_STATE_ONLY = "MATERIAL_STATE_ONLY"

    @classmethod
    def for_origins(
        cls,
        origins: tuple[ForecastSlotOrigin, ...],
    ) -> ForecastSlotStratum:
        if origins == (ForecastSlotOrigin.CADENCE,):
            return cls.CADENCE_ONLY
        if origins == (ForecastSlotOrigin.MATERIAL_STATE,):
            return cls.MATERIAL_STATE_ONLY
        raise ValueError("Forecast slot 必须且只能有一个来源")


class ForecastSlotCause(FrozenModel):
    """Why one immutable Forecast obligation exists; never a direction signal."""

    origin: ForecastSlotOrigin
    policy_version: str = Field(min_length=1)
    trigger_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def origin_and_refs_must_be_consistent(self):
        if tuple(sorted(set(self.trigger_refs))) != self.trigger_refs:
            raise ValueError("Forecast slot trigger_refs 必须唯一且排序")
        if self.origin != ForecastSlotOrigin.MATERIAL_STATE and self.trigger_refs:
            raise ValueError("定时 Forecast slot 不得伪造事件引用")
        if self.origin == ForecastSlotOrigin.MATERIAL_STATE and not self.trigger_refs:
            raise ValueError("材料事件 Forecast slot 必须引用触发事实")
        return self

    @property
    def origins(self) -> tuple[ForecastSlotOrigin, ...]:
        return (self.origin,)

    def identity_payload(self) -> dict[str, object]:
        """Keep historical single-origin slot identities byte-for-byte stable."""

        payload: dict[str, object] = {
            "origin": self.origin.value,
            "policy_version": self.policy_version,
            "trigger_refs": self.trigger_refs,
        }
        return payload

    @classmethod
    def cadence(cls, contract: ForecastContract) -> ForecastSlotCause:
        return cls(
            origin=ForecastSlotOrigin.CADENCE,
            policy_version=contract.decision_slot_rule,
        )

    @classmethod
    def material_state(
        cls,
        *,
        policy_version: str,
        trigger_refs: tuple[str, ...],
    ) -> ForecastSlotCause:
        return cls(
            origin=ForecastSlotOrigin.MATERIAL_STATE,
            policy_version=policy_version,
            trigger_refs=tuple(sorted(set(trigger_refs))),
        )

class ForecastDecisionSlot(FrozenModel):
    slot_id: str = Field(min_length=1)
    contract_id: str = Field(min_length=1)
    slot_as_of: datetime
    information_cutoff_at: datetime
    cutoff_prices: tuple[ForecastPriceAnchor, ...] = ()
    completion_deadline_at: datetime
    outcome_start_at: datetime | None = None
    evaluation_at: datetime
    cause: ForecastSlotCause | None = None

    _utc_slot_as_of = field_validator("slot_as_of")(require_utc)
    _utc_information_cutoff_at = field_validator("information_cutoff_at")(require_utc)
    _utc_completion_deadline_at = field_validator("completion_deadline_at")(require_utc)
    _utc_outcome_start_at = field_validator("outcome_start_at")(optional_utc)
    _utc_evaluation_at = field_validator("evaluation_at")(require_utc)

    @property
    def origins(self) -> tuple[ForecastSlotOrigin, ...]:
        return (
            (ForecastSlotOrigin.CADENCE,)
            if self.cause is None
            else self.cause.origins
        )

    @property
    def stratum(self) -> ForecastSlotStratum:
        return ForecastSlotStratum.for_origins(self.origins)

    @model_validator(mode="after")
    def identity_and_time_are_canonical(self):
        if not (
            self.information_cutoff_at
            <= self.slot_as_of
            < self.completion_deadline_at
            < self.evaluation_at
        ):
            raise ValueError("ForecastDecisionSlot 时间顺序非法")
        if self.outcome_start_at is not None and not (
            self.completion_deadline_at <= self.outcome_start_at < self.evaluation_at
        ):
            raise ValueError("ForecastDecisionSlot Outcome 起点必须位于完成期限和评价之间")
        if any(item.available_at > self.information_cutoff_at for item in self.cutoff_prices):
            raise ValueError("ForecastDecisionSlot cutoff price 不得晚于信息截止")
        anchor_ids = tuple(item.instrument_id for item in self.cutoff_prices)
        if len(set(anchor_ids)) != len(anchor_ids):
            raise ValueError("ForecastDecisionSlot cutoff prices 不能重复 Instrument")
        expected = self.identity_for(
            self.contract_id,
            self.slot_as_of,
            cause=self.cause,
        )
        if self.slot_id != expected:
            raise ValueError("ForecastDecisionSlot slot_id 与合同/时点不一致")
        return self

    @staticmethod
    def identity_for(
        contract_id: str,
        slot_as_of: datetime,
        *,
        cause: ForecastSlotCause | None = None,
    ) -> str:
        identity = (
            contract_id,
            require_utc(slot_as_of).isoformat(),
        )
        # ``None`` preserves immutable slots written before slot causes existed.
        return (
            stable_id("forecast_decision_slot", *identity)
            if cause is None
            else stable_id(
                "forecast_decision_slot",
                *identity,
                content_hash(cause.identity_payload()),
            )
        )

    @classmethod
    def create(
        cls,
        contract: ForecastContract,
        *,
        slot_as_of: datetime,
        cutoff_prices: tuple[ForecastPriceAnchor, ...],
        information_cutoff_at: datetime | None = None,
        cause: ForecastSlotCause | None = None,
    ) -> ForecastDecisionSlot:
        slot_at = require_utc(slot_as_of)
        cutoff = require_utc(information_cutoff_at or slot_at)
        completion_deadline = slot_at + timedelta(
            seconds=contract.completion_deadline_seconds
        )
        return cls(
            slot_id=cls.identity_for(contract.contract_id, slot_at, cause=cause),
            contract_id=contract.contract_id,
            slot_as_of=slot_at,
            information_cutoff_at=cutoff,
            cutoff_prices=cutoff_prices,
            completion_deadline_at=completion_deadline,
            outcome_start_at=(
                None
                if contract.outcome_start_delay_seconds is None
                else completion_deadline
                + timedelta(seconds=contract.outcome_start_delay_seconds)
            ),
            evaluation_at=cutoff + timedelta(minutes=contract.horizon_minutes),
            cause=cause,
        )


class ForecastProducerBinding(FrozenModel):
    binding_id: str = Field(min_length=1)
    contract_id: str = Field(min_length=1)
    producer_kind: ForecastProducerKind
    producer_id: str = Field(min_length=1)
    producer_behavior_id: str = Field(min_length=1)
    permission: ForecastPermission
    required_feature_keys: tuple[str, ...] = ()

    @classmethod
    def create(
        cls,
        *,
        contract_id: str,
        producer_kind: ForecastProducerKind,
        producer_id: str,
        producer_behavior_id: str,
        permission: ForecastPermission,
        required_feature_keys: tuple[str, ...] = (),
    ) -> ForecastProducerBinding:
        """Create a binding from investment eligibility, independent of its Venue."""

        feature_keys = tuple(sorted(set(required_feature_keys)))
        return cls(
            binding_id=stable_id(
                "forecast_producer_binding",
                contract_id,
                producer_kind.value,
                producer_id,
                producer_behavior_id,
                permission.value,
                feature_keys,
            ),
            contract_id=contract_id,
            producer_kind=producer_kind,
            producer_id=producer_id,
            producer_behavior_id=producer_behavior_id,
            permission=permission,
            required_feature_keys=feature_keys,
        )

    @model_validator(mode="after")
    def identity_and_requirements_are_canonical(self):
        if tuple(sorted(set(self.required_feature_keys))) != self.required_feature_keys:
            raise ValueError("Forecast Producer 必需特征必须唯一且排序")
        expected = stable_id(
            "forecast_producer_binding",
            self.contract_id,
            self.producer_kind.value,
            self.producer_id,
            self.producer_behavior_id,
            self.permission.value,
            self.required_feature_keys,
        )
        if self.binding_id != expected:
            raise ValueError("ForecastProducerBinding binding_id 与内容不一致")
        return self


class ForecastSlotObligation(FrozenModel):
    """A producer behavior's immutable duty to answer one decision slot."""

    obligation_id: str = Field(min_length=1)
    slot_id: str = Field(min_length=1)
    contract_id: str = Field(min_length=1)
    binding_id: str = Field(min_length=1)
    producer_kind: ForecastProducerKind
    producer_id: str = Field(min_length=1)
    producer_behavior_id: str = Field(min_length=1)
    assigned_at: datetime

    _utc_assigned_at = field_validator("assigned_at")(require_utc)

    @model_validator(mode="after")
    def identity_is_canonical(self):
        expected = stable_id(
            "forecast_slot_obligation",
            self.slot_id,
            self.binding_id,
        )
        if self.obligation_id != expected:
            raise ValueError("ForecastSlotObligation 身份与槽/Binding 不一致")
        return self

    @classmethod
    def create(
        cls,
        *,
        slot: ForecastDecisionSlot,
        binding: ForecastProducerBinding,
    ) -> ForecastSlotObligation:
        return cls(
            obligation_id=stable_id(
                "forecast_slot_obligation",
                slot.slot_id,
                binding.binding_id,
            ),
            slot_id=slot.slot_id,
            contract_id=slot.contract_id,
            binding_id=binding.binding_id,
            producer_kind=binding.producer_kind,
            producer_id=binding.producer_id,
            producer_behavior_id=binding.producer_behavior_id,
            assigned_at=slot.slot_as_of,
        )


class ForecastNoEstimate(FrozenModel):
    result_id: str = Field(min_length=1)
    slot_id: str = Field(min_length=1)
    contract_id: str = Field(min_length=1)
    producer_kind: ForecastProducerKind
    producer_id: str = Field(min_length=1)
    producer_behavior_id: str = Field(min_length=1)
    reason: ForecastNoEstimateReason
    information_cutoff_at: datetime
    attempted_at: datetime
    completed_at: datetime
    input_refs: tuple[str, ...] = ()
    detail: str | None = Field(default=None, max_length=500)

    _utc_information_cutoff_at = field_validator("information_cutoff_at")(require_utc)
    _utc_attempted_at = field_validator("attempted_at")(require_utc)
    _utc_completed_at = field_validator("completed_at")(require_utc)

    @model_validator(mode="after")
    def identity_timing_and_refs_are_canonical(self):
        if not self.information_cutoff_at <= self.attempted_at <= self.completed_at:
            raise ValueError("ForecastNoEstimate 时间顺序非法")
        if tuple(sorted(set(self.input_refs))) != self.input_refs:
            raise ValueError("ForecastNoEstimate input_refs 必须唯一且排序")
        expected = stable_id(
            "forecast_no_estimate",
            self.slot_id,
            self.producer_behavior_id,
        )
        if self.result_id != expected:
            raise ValueError("ForecastNoEstimate result_id 与槽/行为不一致")
        return self
