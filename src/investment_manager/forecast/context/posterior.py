"""Prospective research-only AI posterior over frozen Quant priors."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from pydantic import Field, TypeAdapter, field_validator, model_validator
from sqlalchemy import func, insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.forecast.codex.bundle import load_existing_bundle, write_run_bundle
from investment_manager.forecast.codex.protocol import codex_execution_contract
from investment_manager.forecast.codex.repository import SqlAccountLeaseStore, SqlCodexAuditStore
from investment_manager.forecast.codex.router import (
    AnalystResult,
    CodexAccountRouter,
    assemble_codex_router,
)
from investment_manager.forecast.context.analyst import configured_assess_behavior_hash
from investment_manager.forecast.context.estimate import (
    CONTEXT_FORECAST_INPUT_VERSION,
    CONTEXT_FORECAST_OUTPUT_VERSION,
    ContextForecastAnalysisTarget,
    ContextForecastDraft,
    ContextForecastProbabilityDraft,
    ContextForecastStructuredOutput,
    ContextForecastTargetStateBehavior,
    QuantContextPosteriorDraft,
    QuantContextPosteriorStructuredOutput,
    context_forecast_input_projection,
    context_forecast_output_schema_for_ids,
    context_forecast_runtime,
)
from investment_manager.forecast.context.posterior_prompt import (
    POSTERIOR_INPUT_VERSION,
    POSTERIOR_INSTRUCTIONS,
    quant_context_posterior_prompt,
)
from investment_manager.forecast.context.producer import finalize_context_base_forecast
from investment_manager.forecast.context.repository import SqlContextAssessmentStore
from investment_manager.forecast.context.stability import (
    SqlContextForecastStabilityRepository,
    preregister_context_forecast_stability,
)
from investment_manager.forecast.context.targets import ContextCapitalTargetDefinition
from investment_manager.forecast.contract_repository import SqlForecastContractStore
from investment_manager.forecast.contracts import (
    ForecastContract,
    ForecastDecisionSlot,
    ForecastNoEstimate,
    ForecastNoEstimateReason,
    ForecastPermission,
    ForecastPriceAnchor,
    ForecastProducerBinding,
    ForecastProducerKind,
    ForecastSlotCause,
    ForecastSlotObligation,
)
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.forecast.results import BaseForecast, ForecastMechanismEffect
from investment_manager.forecast.tables import (
    context_forecast_posterior_assignments,
    forecast_decision_slots,
    forecast_no_estimates,
    forecast_producer_bindings,
    forecast_slot_obligations,
    forecasts,
)
from investment_manager.governance.policy import (
    ContextForecastStabilityPolicy,
    QuantContextPosteriorPolicy,
)
from investment_manager.kernel.errors import PointInTimeInputUnavailable
from investment_manager.kernel.identity import canonical_json, content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.market.models import InstrumentId, InstrumentProduct
from investment_manager.market.repository import MarketDataStore, SqlMarketDataStore
from investment_manager.settings import AppConfig

POSTERIOR_FINALIZATION_VERSION = "quant-context-posterior-finalization-v1"


def quant_context_input_behavior_id(
    *,
    config: AppConfig,
    contracts: tuple[ForecastContract, ...],
    target_state_behaviors: tuple[
        tuple[str, ContextForecastTargetStateBehavior], ...
    ],
) -> str:
    """Identify only the shared point-in-time input projection, not retired AI output."""

    context = config.capital.context_forecast
    if context is None:
        raise ValueError("Quant Context posterior 缺少 Context 输入政策")
    return content_hash(
        {
            "input_version": CONTEXT_FORECAST_INPUT_VERSION,
            "contracts": tuple(
                sorted(contracts, key=lambda item: item.outcome_family_id)
            ),
            "target_state_behaviors": tuple(sorted(target_state_behaviors)),
            "world_model_behavior_id": configured_assess_behavior_hash(config),
            "input_policy": context.model_dump(
                mode="json",
                exclude={
                    "producer_behavior_id": True,
                    "enabled": True,
                    "reasoning_effort": True,
                    "targets": {"__all__": {"product_payoffs"}},
                },
            ),
        }
    )


def quant_context_posterior_behavior_id(
    *,
    config: AppConfig,
    contracts: tuple[ForecastContract, ...],
    target_state_behaviors: tuple[
        tuple[str, ContextForecastTargetStateBehavior], ...
    ],
    quant_producer_behavior_id: str,
) -> str:
    policy = config.outcome_evaluation.quant_context_posterior
    context = config.capital.context_forecast
    if policy is None or context is None:
        raise ValueError("Quant Context posterior 配置不完整")
    runtime = context_forecast_runtime(config.codex_runtime, context)
    ordered_contracts = tuple(sorted(contracts, key=lambda item: item.outcome_family_id))
    return content_hash(
        {
            "input_version": POSTERIOR_INPUT_VERSION,
            "output_version": CONTEXT_FORECAST_OUTPUT_VERSION,
            "finalization_version": POSTERIOR_FINALIZATION_VERSION,
            "instructions": POSTERIOR_INSTRUCTIONS,
            "output_model": QuantContextPosteriorStructuredOutput.model_json_schema(),
            "contracts": ordered_contracts,
            "context_input_behavior_id": quant_context_input_behavior_id(
                config=config,
                contracts=ordered_contracts,
                target_state_behaviors=target_state_behaviors,
            ),
            "quant_producer_behavior_id": quant_producer_behavior_id,
            "policy": policy.model_dump(mode="json", exclude={"enabled": True}),
            "execution_contract": codex_execution_contract(),
            "runtime_policy_version": runtime.version,
            "expected_cli_version": runtime.expected_cli_version,
            "expected_binary_sha256": runtime.expected_binary_sha256,
            "model": runtime.model,
            "reasoning_effort": runtime.reasoning_effort,
            "maximum_schema_attempts": 1 + runtime.max_account_switches,
        }
    )


class QuantContextPosteriorTarget(FrozenModel):
    slot: ForecastDecisionSlot
    contract: ForecastContract
    binding: ForecastProducerBinding
    instrument: InstrumentId
    input_observed_at: datetime
    quant_forecast_id: str | None = None
    quant_no_estimate_id: str | None = None
    quant_reason: ForecastNoEstimateReason | None = None
    quant_input_refs: tuple[str, ...] = ()

    _utc_input_observed_at = field_validator("input_observed_at")(require_utc)

    @model_validator(mode="after")
    def identities_and_quant_terminal_are_complete(self):
        if (
            self.slot.contract_id != self.contract.contract_id
            or self.binding.contract_id != self.contract.contract_id
            or self.binding.producer_kind != ForecastProducerKind.CONTEXT
            or self.binding.permission != ForecastPermission.RESEARCH
            or self.contract.target.legs[0].instrument != self.instrument
        ):
            raise ValueError("Quant posterior target 的 Slot/Contract/Binding/Instrument 不一致")
        forecast = self.quant_forecast_id is not None
        absence = self.quant_no_estimate_id is not None
        if forecast == absence or absence != (self.quant_reason is not None):
            raise ValueError("Quant posterior target 必须绑定唯一 Quant 终态")
        if tuple(sorted(set(self.quant_input_refs))) != self.quant_input_refs:
            raise ValueError("Quant posterior target input refs 必须唯一排序")
        return self

    @property
    def quant_available(self) -> bool:
        return self.quant_forecast_id is not None


class QuantContextPosteriorAssignment(FrozenModel):
    assignment_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    producer_behavior_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    formal_producer_behavior_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    quant_producer_behavior_id: str = Field(min_length=1)
    targets: tuple[QuantContextPosteriorTarget, ...] = Field(min_length=1)
    information_cutoff_at: datetime
    assigned_at: datetime
    completion_deadline_at: datetime
    evaluation_at: datetime
    analysis_input_json: str = Field(min_length=2)
    analysis_input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt: str = Field(min_length=1)
    prompt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_schema_json: str = Field(min_length=2)
    output_schema_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    _utc_times = field_validator(
        "information_cutoff_at",
        "assigned_at",
        "completion_deadline_at",
        "evaluation_at",
    )(require_utc)

    @model_validator(mode="after")
    def frozen_input_and_identity_are_valid(self):
        if not (
            self.information_cutoff_at <= self.assigned_at < self.evaluation_at
            and self.information_cutoff_at < self.completion_deadline_at < self.evaluation_at
        ):
            raise ValueError("Quant posterior assignment 时间边界非法")
        if tuple(item.slot.slot_id for item in self.targets) != tuple(
            sorted({item.slot.slot_id for item in self.targets})
        ):
            raise ValueError("Quant posterior targets 必须按唯一 slot 排序")
        if any(
            item.slot.information_cutoff_at != self.information_cutoff_at
            or item.slot.completion_deadline_at != self.completion_deadline_at
            or item.slot.evaluation_at != self.evaluation_at
            or item.binding.producer_behavior_id != self.producer_behavior_id
            for item in self.targets
        ):
            raise ValueError("Quant posterior targets 未共享 assignment 边界")
        analysis_input = _canonical_object(self.analysis_input_json, "posterior input")
        output_schema = _canonical_object(self.output_schema_json, "posterior schema")
        if content_hash(analysis_input) != self.analysis_input_hash:
            raise ValueError("Quant posterior input hash 不一致")
        if self.prompt != quant_context_posterior_prompt(analysis_input):
            raise ValueError("Quant posterior prompt 不是冻结输入的唯一投影")
        if content_hash({"prompt": self.prompt}) != self.prompt_hash:
            raise ValueError("Quant posterior prompt hash 不一致")
        if content_hash(output_schema) != self.output_schema_hash:
            raise ValueError("Quant posterior schema hash 不一致")
        expected_id = stable_id(
            "quant_context_posterior_assignment",
            self.policy_version,
            self.producer_behavior_id,
            *(item.slot.slot_id for item in self.targets),
        )
        if self.assignment_id != expected_id:
            raise ValueError("Quant posterior assignment_id 不一致")
        if content_hash(self.model_dump(mode="json", exclude={"source_hash"})) != self.source_hash:
            raise ValueError("Quant posterior source_hash 不一致")
        return self

    @property
    def analysis_targets(self) -> tuple[QuantContextPosteriorTarget, ...]:
        return tuple(item for item in self.targets if item.quant_available)


class QuantContextPosteriorReport(FrozenModel):
    policy_version: str
    producer_behavior_id: str
    as_of: datetime
    assignment_count: int = Field(ge=0)
    forecast_count: int = Field(ge=0)
    no_estimate_count: int = Field(ge=0)
    pending_count: int = Field(ge=0)

    _utc_as_of = field_validator("as_of")(require_utc)


def build_quant_context_posterior_assignment(
    *,
    policy: QuantContextPosteriorPolicy,
    producer_behavior_id: str,
    formal_producer_behavior_id: str,
    quant_producer_behavior_id: str,
    targets: tuple[QuantContextPosteriorTarget, ...],
    formal_analysis_input: dict[str, object],
    quant_forecasts: dict[str, BaseForecast],
    assigned_at: datetime,
) -> QuantContextPosteriorAssignment:
    ordered = tuple(sorted(targets, key=lambda item: item.slot.slot_id))
    eligible_slot_ids = {item.slot.slot_id for item in ordered if item.quant_available}
    raw = _canonical_object(canonical_json(formal_analysis_input), "formal input")
    raw_targets = raw.get("forecast_targets")
    world_model = raw.get("world_model")
    if not isinstance(raw_targets, list) or not isinstance(world_model, dict):
        raise ValueError("Quant posterior 正式输入结构非法")
    posterior_targets = []
    for raw_target in raw_targets:
        if not isinstance(raw_target, dict):
            raise ValueError("Quant posterior 正式 target 结构非法")
        decision_slot = raw_target.get("decision_slot")
        if not isinstance(decision_slot, dict):
            raise ValueError("Quant posterior target 缺少 DecisionSlot")
        slot_id = decision_slot.get("decision_slot_id")
        if slot_id not in eligible_slot_ids:
            continue
        quant = quant_forecasts[str(slot_id)]
        if quant.program_input_json is None:
            raise ValueError("Quant posterior 来源 Forecast 缺少程序输入")
        panel = _canonical_object(quant.program_input_json, "quant panel")
        projected = dict(raw_target)
        projected["quant_panel"] = {
            key: panel[key]
            for key in (
                "panel_version",
                "artifact_id",
                "inference_version",
                "features",
                "quant_prior",
                "maximum_bucket_probability_range",
            )
        }
        posterior_targets.append(projected)
    analysis_input = {
        "purpose": "QUANT_CONTEXT_POSTERIOR",
        "forecast_targets": tuple(posterior_targets),
        "world_model": world_model,
    }
    schema = _posterior_output_schema(analysis_input)
    prompt = quant_context_posterior_prompt(analysis_input)
    first = ordered[0].slot
    values = {
        "assignment_id": stable_id(
            "quant_context_posterior_assignment",
            policy.version,
            producer_behavior_id,
            *(item.slot.slot_id for item in ordered),
        ),
        "policy_version": policy.version,
        "producer_behavior_id": producer_behavior_id,
        "formal_producer_behavior_id": formal_producer_behavior_id,
        "quant_producer_behavior_id": quant_producer_behavior_id,
        "targets": ordered,
        "information_cutoff_at": first.information_cutoff_at,
        "assigned_at": require_utc(assigned_at),
        "completion_deadline_at": first.completion_deadline_at,
        "evaluation_at": first.evaluation_at,
        "analysis_input_json": canonical_json(analysis_input),
        "analysis_input_hash": content_hash(analysis_input),
        "prompt": prompt,
        "prompt_hash": content_hash({"prompt": prompt}),
        "output_schema_json": canonical_json(schema),
        "output_schema_hash": content_hash(schema),
    }
    values["source_hash"] = content_hash(values)
    return QuantContextPosteriorAssignment.model_validate(values)


def audit_quant_context_posterior_draft(
    *,
    draft: ContextForecastDraft | QuantContextPosteriorDraft,
    quant_prior: BaseForecast,
    analysis_input: dict[str, object],
) -> QuantContextPosteriorDraft:
    """Make the stored posterior match its declared causal use of the Quant prior."""

    if draft.decision_slot_id != quant_prior.decision_slot_id:
        raise ValueError("Quant posterior draft 与来源 Quant prior 的 Slot 不一致")
    posterior_bucket_ids = tuple(item.bucket_id for item in draft.outcome_probabilities)
    prior_bucket_ids = tuple(item.bucket_id for item in quant_prior.outcome_probabilities)
    if posterior_bucket_ids != prior_bucket_ids:
        raise ValueError("Quant posterior draft 与来源 Quant prior 的 bucket 不一致")

    world_model = analysis_input.get("world_model")
    mechanisms = world_model.get("mechanisms") if isinstance(world_model, dict) else None
    if not isinstance(mechanisms, (list, tuple)):
        raise ValueError("Quant posterior 输入缺少 WorldModel mechanisms")
    evidence_by_mechanism: dict[str, set[str]] = {}
    for mechanism in mechanisms:
        if not isinstance(mechanism, dict):
            raise ValueError("Quant posterior WorldModel mechanism 结构非法")
        mechanism_id = mechanism.get("mechanism_id")
        if not isinstance(mechanism_id, str) or mechanism_id in evidence_by_mechanism:
            raise ValueError("Quant posterior WorldModel mechanism 身份非法")
        evidence_ids: set[str] = set()
        for field_name in ("evidence_ids", "conflicting_evidence_ids"):
            values = mechanism.get(field_name, ())
            if not isinstance(values, (list, tuple)) or not all(
                isinstance(item, str) for item in values
            ):
                raise ValueError("Quant posterior WorldModel evidence 结构非法")
            evidence_ids.update(values)
        evidence_by_mechanism[mechanism_id] = evidence_ids

    substantive = tuple(
        item
        for item in draft.mechanism_contributions
        if item.effect != ForecastMechanismEffect.NO_MATERIAL_EFFECT
    )
    cited = set(draft.evidence_refs)
    for contribution in substantive:
        mechanism_evidence = evidence_by_mechanism.get(contribution.mechanism_id)
        if mechanism_evidence is None:
            raise ValueError("Quant posterior 引用了未知 WorldModel mechanism")
        if not cited.intersection(mechanism_evidence):
            raise ValueError("Quant posterior 实质调整未引用对应 mechanism 的 evidence")

    posterior_probabilities = tuple(
        Decimal(item.probability) for item in draft.outcome_probabilities
    )
    prior_probabilities = tuple(item.probability for item in quant_prior.outcome_probabilities)
    if substantive:
        if posterior_probabilities == prior_probabilities:
            raise ValueError("Quant posterior 声明实质影响但未改变 Quant prior")
        return QuantContextPosteriorDraft.model_validate(draft.model_dump())

    return QuantContextPosteriorDraft(
        decision_slot_id=draft.decision_slot_id,
        outcome_probabilities=tuple(
            ContextForecastProbabilityDraft(
                bucket_id=item.bucket_id,
                probability=format(item.probability, "f"),
            )
            for item in quant_prior.outcome_probabilities
        ),
        mechanism_contributions=draft.mechanism_contributions,
        evidence_refs=draft.evidence_refs,
        invalidation_conditions=draft.invalidation_conditions,
    )


@dataclass(slots=True)
class QuantContextPosteriorAllocator:
    policy: QuantContextPosteriorPolicy
    formal_producer_behavior_id: str
    quant_producer_behavior_id: str
    producer_behavior_id: str
    bindings_by_contract: dict[str, ForecastProducerBinding]
    contracts: SqlForecastContractStore
    forecasts: SqlForecastStore
    repository: SqlQuantContextPosteriorRepository
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def allocate(
        self,
        *,
        slot: ForecastDecisionSlot,
        analysis_input: dict[str, object],
    ) -> QuantContextPosteriorAssignment | None:
        """Freeze one AI+Quant task directly from point-in-time inputs."""

        if slot.information_cutoff_at < self.policy.activated_at:
            return None
        raw = _canonical_object(canonical_json(analysis_input), "posterior input")
        raw_targets = raw.get("forecast_targets")
        if not isinstance(raw_targets, list):
            raise ValueError("Quant posterior 输入 targets 非法")
        raw_slot_ids = {
            str(decision_slot["decision_slot_id"])
            for raw_target in raw_targets
            if isinstance(raw_target, dict)
            and isinstance((decision_slot := raw_target.get("decision_slot")), dict)
            and isinstance(decision_slot.get("decision_slot_id"), str)
        }
        if len(raw_slot_ids) != len(raw_targets):
            raise ValueError("Quant posterior 输入 target 身份重复或缺失")
        expected_slots = self._expected_slots(anchor=slot)
        if not raw_slot_ids.issubset(expected_slots):
            raise ValueError("Quant posterior 输入包含非共同 Slot")
        if not raw_targets:
            return None
        targets = []
        quant_forecasts: dict[str, BaseForecast] = {}
        for raw_target in raw_targets:
            decision_slot = (
                raw_target.get("decision_slot") if isinstance(raw_target, dict) else None
            )
            contract_payload = (
                raw_target.get("forecast_contract") if isinstance(raw_target, dict) else None
            )
            state = raw_target.get("target_state") if isinstance(raw_target, dict) else None
            if not all(isinstance(item, dict) for item in (decision_slot, contract_payload, state)):
                raise ValueError("Quant posterior 正式 target 边界不完整")
            slot_id = decision_slot.get("decision_slot_id")
            contract_id = contract_payload.get("contract_id")
            state_as_of = state.get("as_of")
            if not all(isinstance(item, str) for item in (slot_id, contract_id, state_as_of)):
                raise ValueError("Quant posterior 正式 target 身份非法")
            authoritative_slot = self.contracts.slot(slot_id)
            contract = self.contracts.contract(contract_id)
            binding = self.bindings_by_contract.get(contract_id)
            if authoritative_slot is None or contract is None or binding is None:
                raise ValueError("Quant posterior target 缺少权威 Slot/Contract/Binding")
            quant, absence, quant_refs = self.quant_terminal(slot_id)
            if quant is not None:
                if quant.program_input_json is None:
                    raise ValueError("Quant posterior 来源 Forecast 缺少程序快照")
                quant_forecasts[slot_id] = quant
                quant_forecast_id = quant.forecast_id
                quant_no_estimate_id = None
                quant_reason = None
            else:
                assert absence is not None
                quant_forecast_id = None
                quant_no_estimate_id = absence.result_id
                quant_reason = absence.reason
            targets.append(
                QuantContextPosteriorTarget(
                    slot=authoritative_slot,
                    contract=contract,
                    binding=binding,
                    instrument=contract.target.legs[0].instrument,
                    input_observed_at=datetime.fromisoformat(state_as_of),
                    quant_forecast_id=quant_forecast_id,
                    quant_no_estimate_id=quant_no_estimate_id,
                    quant_reason=quant_reason,
                    quant_input_refs=quant_refs,
                )
            )
        ordered_slot_ids = tuple(sorted(item.slot.slot_id for item in targets))
        assignment_id = stable_id(
            "quant_context_posterior_assignment",
            self.policy.version,
            self.producer_behavior_id,
            *ordered_slot_ids,
        )
        existing = self.repository.assignment(assignment_id)
        assignment = build_quant_context_posterior_assignment(
            policy=self.policy,
            producer_behavior_id=self.producer_behavior_id,
            formal_producer_behavior_id=self.formal_producer_behavior_id,
            quant_producer_behavior_id=self.quant_producer_behavior_id,
            targets=tuple(targets),
            formal_analysis_input=analysis_input,
            quant_forecasts=quant_forecasts,
            assigned_at=(
                existing.assigned_at if existing is not None else require_utc(self.clock())
            ),
        )
        if existing is not None and existing != assignment:
            raise ValueError("Quant posterior 重试绑定了不同冻结输入")
        self.repository.record_assignment(assignment)
        return assignment

    def _expected_slots(
        self,
        *,
        anchor: ForecastDecisionSlot,
    ) -> dict[str, ForecastDecisionSlot]:
        values: dict[str, ForecastDecisionSlot] = {}
        for contract_id in sorted(self.bindings_by_contract):
            slot_id = ForecastDecisionSlot.identity_for(
                contract_id,
                anchor.slot_as_of,
                cause=anchor.cause,
            )
            slot = self.contracts.slot(slot_id)
            if (
                slot is None
                or slot.information_cutoff_at != anchor.information_cutoff_at
                or slot.completion_deadline_at != anchor.completion_deadline_at
                or slot.evaluation_at != anchor.evaluation_at
            ):
                raise ValueError("Quant posterior 缺少同槽 Quant 权威 Slot")
            values[slot_id] = slot
        return values

    def quant_terminal(
        self,
        slot_id: str,
    ) -> tuple[BaseForecast | None, ForecastNoEstimate | None, tuple[str, ...]]:
        quant = self.forecasts.result_for_behavior(
            decision_slot_id=slot_id,
            producer_behavior_id=self.quant_producer_behavior_id,
        )
        absence = self.contracts.no_estimate(
            stable_id("forecast_no_estimate", slot_id, self.quant_producer_behavior_id)
        )
        if isinstance(quant, BaseForecast):
            return quant, None, tuple(sorted({quant.forecast_id, *quant.input_refs}))
        if absence is not None:
            return None, absence, tuple(sorted({absence.result_id, *absence.input_refs}))
        raise ValueError("Quant posterior 同槽 Quant 义务尚未形成终态")


@dataclass(slots=True)
class QuantContextPosteriorAssignmentProducer:
    """Freeze the asynchronous AI+Quant task directly after Quant terminates."""

    policy: QuantContextPosteriorPolicy
    targets: tuple[ContextCapitalTargetDefinition, ...]
    allocator: QuantContextPosteriorAllocator
    contexts: SqlContextAssessmentStore
    analysis_scope: str
    activated_at: datetime
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def __post_init__(self) -> None:
        require_utc(self.activated_at)
        families = tuple(item.contract.outcome_family_id for item in self.targets)
        if not self.targets or families != tuple(sorted(set(families))):
            raise ValueError("Quant posterior assignment targets 必须按唯一 family 排序")

    def produce_all(
        self,
        *,
        as_of: datetime,
        cause: ForecastSlotCause | None = None,
    ) -> tuple[object, ...]:
        slot_at = require_utc(as_of)
        if slot_at < max(self.activated_at, self.policy.activated_at):
            return ()
        slots = {
            item.contract.outcome_family_id: self._slot(
                item,
                as_of=slot_at,
                cause=cause,
            )
            for item in self.targets
        }
        pending = tuple(
            item
            for item in self.targets
            if self._existing(slots[item.contract.outcome_family_id]) is None
        )
        if not pending:
            return tuple(
                self._existing(slots[item.contract.outcome_family_id])
                for item in self.targets
            )

        assessment = self.contexts.latest_before(
            analysis_scope=self.analysis_scope,
            as_of=slot_at,
        )
        packet = (
            None
            if assessment is None
            else self.contexts.packet_for_assessment(assessment.assessment_id)
        )
        if (
            assessment is not None
            and packet is not None
            and assessment.decision_packet_hash != packet.content_hash
        ):
            raise ValueError("Quant posterior 的 WorldModel/Packet 身份不一致")

        analysis_targets: list[ContextForecastAnalysisTarget] = []
        results: list[object] = []
        for target in pending:
            slot = slots[target.contract.outcome_family_id]
            quant, quant_absence, quant_refs = self.allocator.quant_terminal(slot.slot_id)
            if quant is None:
                assert quant_absence is not None
                results.append(
                    self._record_absence(
                        slot=slot,
                        reason=quant_absence.reason,
                        completed_at=quant_absence.completed_at,
                        input_refs=quant_refs,
                        detail=f"QUANT_PRIOR_UNAVAILABLE:{quant_absence.reason.value}",
                    )
                )
                continue
            if assessment is None or packet is None:
                results.append(
                    self._record_absence(
                        slot=slot,
                        reason=ForecastNoEstimateReason.WORLD_MODEL_UNAVAILABLE,
                        completed_at=max(quant.available_at, slot_at),
                        input_refs=quant_refs,
                        detail="WORLD_MODEL_INPUT_UNAVAILABLE",
                    )
                )
                continue
            try:
                target_state = target.target_states.build(as_of=slot_at)
            except (PointInTimeInputUnavailable, ValueError) as exc:
                results.append(
                    self._record_absence(
                        slot=slot,
                        reason=ForecastNoEstimateReason.MARKET_INPUT_INVALID,
                        completed_at=max(quant.available_at, slot_at),
                        input_refs=tuple(
                            sorted(
                                {
                                    assessment.assessment_id,
                                    packet.packet_id,
                                    packet.content_hash,
                                    *quant_refs,
                                }
                            )
                        ),
                        detail=f"TARGET_STATE_UNAVAILABLE:{type(exc).__name__}",
                    )
                )
                continue
            missing = tuple(
                sorted(
                    set(target.policy.required_feature_keys)
                    - set(target_state.feature_selectors)
                )
            )
            if missing:
                results.append(
                    self._record_absence(
                        slot=slot,
                        reason=ForecastNoEstimateReason.REQUIRED_FEATURE_MISSING,
                        completed_at=max(quant.available_at, slot_at),
                        input_refs=tuple(
                            sorted(
                                {
                                    assessment.assessment_id,
                                    packet.packet_id,
                                    packet.content_hash,
                                    *quant_refs,
                                    *target_state.input_refs,
                                }
                            )
                        ),
                        detail=f"MISSING_FEATURES:{','.join(missing)}",
                    )
                )
                continue
            analysis_targets.append(
                ContextForecastAnalysisTarget(
                    slot=slot,
                    contract=target.contract,
                    target_state=target_state,
                )
            )
        if analysis_targets:
            assert assessment is not None and packet is not None
            model_input = context_forecast_input_projection(
                targets=tuple(analysis_targets),
                assessment=assessment,
                packet=packet,
            )
            assignment = self.allocator.allocate(
                slot=analysis_targets[0].slot,
                analysis_input=model_input.payload,
            )
            if assignment is not None:
                results.append(assignment)
        return tuple(results)

    def _slot(
        self,
        target: ContextCapitalTargetDefinition,
        *,
        as_of: datetime,
        cause: ForecastSlotCause | None,
    ) -> ForecastDecisionSlot:
        slot_id = ForecastDecisionSlot.identity_for(
            target.contract.contract_id,
            as_of,
            cause=cause or ForecastSlotCause.cadence(target.contract),
        )
        slot = self.allocator.contracts.slot(slot_id)
        if slot is None:
            raise ValueError("Quant posterior assignment 缺少先行 Quant DecisionSlot")
        return slot

    def _existing(self, slot: ForecastDecisionSlot) -> object | None:
        forecast = self.allocator.forecasts.result_for_behavior(
            decision_slot_id=slot.slot_id,
            producer_behavior_id=self.allocator.producer_behavior_id,
        )
        if forecast is not None:
            return forecast
        return self.allocator.contracts.no_estimate(
            stable_id(
                "forecast_no_estimate",
                slot.slot_id,
                self.allocator.producer_behavior_id,
            )
        )

    def _record_absence(
        self,
        *,
        slot: ForecastDecisionSlot,
        reason: ForecastNoEstimateReason,
        completed_at: datetime,
        input_refs: tuple[str, ...],
        detail: str,
    ) -> ForecastNoEstimate:
        binding = self.allocator.bindings_by_contract[slot.contract_id]
        self.allocator.contracts.record_obligation(slot=slot, binding=binding)
        result = ForecastNoEstimate(
            result_id=stable_id(
                "forecast_no_estimate",
                slot.slot_id,
                binding.producer_behavior_id,
            ),
            slot_id=slot.slot_id,
            contract_id=slot.contract_id,
            producer_kind=binding.producer_kind,
            producer_id=binding.producer_id,
            producer_behavior_id=binding.producer_behavior_id,
            reason=reason,
            information_cutoff_at=slot.information_cutoff_at,
            attempted_at=slot.slot_as_of,
            completed_at=max(require_utc(completed_at), slot.slot_as_of),
            input_refs=tuple(sorted(set(input_refs))),
            detail=detail,
        )
        self.allocator.contracts.record_no_estimate(result)
        return result


class SqlQuantContextPosteriorRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record_assignment(self, assignment: QuantContextPosteriorAssignment) -> bool:
        try:
            with self._engine.begin() as connection:
                for target in assignment.targets:
                    slot_payload = connection.execute(
                        select(forecast_decision_slots.c.payload).where(
                            forecast_decision_slots.c.slot_id == target.slot.slot_id
                        )
                    ).scalar_one_or_none()
                    binding_payload = connection.execute(
                        select(forecast_producer_bindings.c.payload).where(
                            forecast_producer_bindings.c.binding_id == target.binding.binding_id
                        )
                    ).scalar_one_or_none()
                    if (
                        slot_payload is None
                        or binding_payload is None
                        or ForecastDecisionSlot.model_validate(slot_payload) != target.slot
                        or ForecastProducerBinding.model_validate(binding_payload) != target.binding
                    ):
                        raise ValueError("Quant posterior assignment 缺少权威 Slot/Binding")
                    if target.quant_available:
                        quant_terminal_id = connection.execute(
                            select(forecasts.c.forecast_id).where(
                                forecasts.c.forecast_id == target.quant_forecast_id,
                                forecasts.c.decision_slot_id == target.slot.slot_id,
                                forecasts.c.producer_behavior_id
                                == assignment.quant_producer_behavior_id,
                            )
                        ).scalar_one_or_none()
                    else:
                        quant_terminal_id = connection.execute(
                            select(forecast_no_estimates.c.result_id).where(
                                forecast_no_estimates.c.result_id == target.quant_no_estimate_id,
                                forecast_no_estimates.c.slot_id == target.slot.slot_id,
                                forecast_no_estimates.c.producer_behavior_id
                                == assignment.quant_producer_behavior_id,
                            )
                        ).scalar_one_or_none()
                    if quant_terminal_id is None:
                        raise ValueError("Quant posterior assignment 缺少权威 Quant 终态")
                    obligation = ForecastSlotObligation.create(
                        slot=target.slot,
                        binding=target.binding,
                    )
                    existing = connection.execute(
                        select(forecast_slot_obligations.c.payload).where(
                            forecast_slot_obligations.c.obligation_id == obligation.obligation_id
                        )
                    ).scalar_one_or_none()
                    if existing is None:
                        connection.execute(
                            insert(forecast_slot_obligations).values(
                                obligation_id=obligation.obligation_id,
                                slot_id=obligation.slot_id,
                                contract_id=obligation.contract_id,
                                binding_id=obligation.binding_id,
                                producer_kind=obligation.producer_kind.value,
                                producer_id=obligation.producer_id,
                                producer_behavior_id=obligation.producer_behavior_id,
                                assigned_at=obligation.assigned_at,
                                payload=obligation.model_dump(mode="json"),
                            )
                        )
                    elif ForecastSlotObligation.model_validate(existing) != obligation:
                        raise ValueError("Quant posterior 槽义务已绑定不同内容")
                connection.execute(
                    insert(context_forecast_posterior_assignments).values(
                        assignment_id=assignment.assignment_id,
                        policy_version=assignment.policy_version,
                        producer_behavior_id=assignment.producer_behavior_id,
                        quant_producer_behavior_id=assignment.quant_producer_behavior_id,
                        information_cutoff_at=assignment.information_cutoff_at,
                        assigned_at=assignment.assigned_at,
                        completion_deadline_at=assignment.completion_deadline_at,
                        source_hash=assignment.source_hash,
                        payload=assignment.model_dump(mode="json"),
                    )
                )
            return True
        except IntegrityError:
            existing = self.assignment(assignment.assignment_id)
            if existing != assignment:
                raise ValueError("Quant posterior assignment 已存在且内容不同") from None
            return False

    def assignment(self, assignment_id: str) -> QuantContextPosteriorAssignment | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(context_forecast_posterior_assignments.c.payload).where(
                    context_forecast_posterior_assignments.c.assignment_id == assignment_id
                )
            ).scalar_one_or_none()
        return None if payload is None else QuantContextPosteriorAssignment.model_validate(payload)

    def assignments(
        self,
        *,
        policy_version: str,
        producer_behavior_id: str,
    ) -> tuple[QuantContextPosteriorAssignment, ...]:
        with self._engine.connect() as connection:
            payloads = connection.execute(
                select(context_forecast_posterior_assignments.c.payload)
                .where(
                    context_forecast_posterior_assignments.c.policy_version == policy_version,
                    context_forecast_posterior_assignments.c.producer_behavior_id
                    == producer_behavior_id,
                )
                .order_by(
                    context_forecast_posterior_assignments.c.information_cutoff_at,
                    context_forecast_posterior_assignments.c.assignment_id,
                )
            ).scalars()
            return tuple(QuantContextPosteriorAssignment.model_validate(item) for item in payloads)

    def pending_assignments(
        self,
        *,
        policy_version: str,
        producer_behavior_id: str,
    ) -> tuple[QuantContextPosteriorAssignment, ...]:
        """Load only assignments with an unfinished common-ledger obligation."""

        terminal_forecast = forecasts.alias("posterior_terminal_forecast")
        terminal_absence = forecast_no_estimates.alias("posterior_terminal_absence")
        pending_obligation = (
            select(forecast_slot_obligations.c.obligation_id)
            .join(
                forecast_decision_slots,
                forecast_decision_slots.c.slot_id == forecast_slot_obligations.c.slot_id,
            )
            .outerjoin(
                terminal_forecast,
                (terminal_forecast.c.decision_slot_id == forecast_slot_obligations.c.slot_id)
                & (
                    terminal_forecast.c.producer_behavior_id
                    == forecast_slot_obligations.c.producer_behavior_id
                ),
            )
            .outerjoin(
                terminal_absence,
                (terminal_absence.c.slot_id == forecast_slot_obligations.c.slot_id)
                & (
                    terminal_absence.c.producer_behavior_id
                    == forecast_slot_obligations.c.producer_behavior_id
                ),
            )
            .where(
                forecast_slot_obligations.c.producer_behavior_id
                == context_forecast_posterior_assignments.c.producer_behavior_id,
                forecast_decision_slots.c.information_cutoff_at
                == context_forecast_posterior_assignments.c.information_cutoff_at,
                terminal_forecast.c.forecast_id.is_(None),
                terminal_absence.c.result_id.is_(None),
            )
            .correlate(context_forecast_posterior_assignments)
            .exists()
        )
        with self._engine.connect() as connection:
            payloads = connection.execute(
                select(context_forecast_posterior_assignments.c.payload)
                .where(
                    context_forecast_posterior_assignments.c.policy_version == policy_version,
                    context_forecast_posterior_assignments.c.producer_behavior_id
                    == producer_behavior_id,
                    pending_obligation,
                )
                .order_by(
                    context_forecast_posterior_assignments.c.information_cutoff_at,
                    context_forecast_posterior_assignments.c.assignment_id,
                )
            ).scalars()
            return tuple(QuantContextPosteriorAssignment.model_validate(item) for item in payloads)

    def counts(
        self,
        *,
        policy_version: str,
        producer_behavior_id: str,
    ) -> tuple[int, int, int, int]:
        """Count historical assignments and common-ledger target terminal states."""

        terminal_forecast = forecasts.alias("posterior_count_forecast")
        terminal_absence = forecast_no_estimates.alias("posterior_count_absence")
        with self._engine.connect() as connection:
            assignment_count = connection.execute(
                select(func.count())
                .select_from(context_forecast_posterior_assignments)
                .where(
                    context_forecast_posterior_assignments.c.policy_version == policy_version,
                    context_forecast_posterior_assignments.c.producer_behavior_id
                    == producer_behavior_id,
                )
            ).scalar_one()
            forecast_count, no_estimate_count, target_count = connection.execute(
                select(
                    func.count(terminal_forecast.c.forecast_id),
                    func.count(terminal_absence.c.result_id),
                    func.count(forecast_slot_obligations.c.obligation_id),
                )
                .select_from(forecast_slot_obligations)
                .outerjoin(
                    terminal_forecast,
                    (terminal_forecast.c.decision_slot_id == forecast_slot_obligations.c.slot_id)
                    & (
                        terminal_forecast.c.producer_behavior_id
                        == forecast_slot_obligations.c.producer_behavior_id
                    ),
                )
                .outerjoin(
                    terminal_absence,
                    (terminal_absence.c.slot_id == forecast_slot_obligations.c.slot_id)
                    & (
                        terminal_absence.c.producer_behavior_id
                        == forecast_slot_obligations.c.producer_behavior_id
                    ),
                )
                .where(
                    forecast_slot_obligations.c.producer_behavior_id == producer_behavior_id,
                )
            ).one()
        pending_count = target_count - forecast_count - no_estimate_count
        return assignment_count, forecast_count, no_estimate_count, pending_count


@dataclass(slots=True)
class CodexQuantContextPosteriorAnalyst:
    bundle_root: Path
    router: CodexAccountRouter
    maximum_prompt_characters: int

    def estimate(self, assignment: QuantContextPosteriorAssignment) -> AnalystResult:
        if len(assignment.prompt) > self.maximum_prompt_characters:
            return AnalystResult(False, None, "FORECAST_INPUT_TOO_LARGE")
        target = self.bundle_root / stable_id(
            "quant_context_posterior_bundle",
            assignment.assignment_id,
            assignment.producer_behavior_id,
        )
        try:
            bundle = load_existing_bundle(
                cycle_id=assignment.assignment_id,
                target=target,
                expected_manifest={
                    "analysis_mode": "QUANT_CONTEXT_POSTERIOR",
                    "assignment_id": assignment.assignment_id,
                    "analysis_behavior_hash": assignment.producer_behavior_id,
                },
            )
            if bundle is None:
                bundle = write_run_bundle(
                    cycle_id=assignment.assignment_id,
                    target=target,
                    prompt=assignment.prompt,
                    files={
                        "quant_context_posterior_input.json": assignment.analysis_input_json + "\n",
                        "analyst_prompt.md": assignment.prompt + "\n",
                        "output.schema.json": assignment.output_schema_json + "\n",
                    },
                    manifest={
                        "analysis_mode": "QUANT_CONTEXT_POSTERIOR",
                        "input_version": POSTERIOR_INPUT_VERSION,
                        "output_version": CONTEXT_FORECAST_OUTPUT_VERSION,
                        "finalization_version": POSTERIOR_FINALIZATION_VERSION,
                        "assignment_id": assignment.assignment_id,
                        "policy_version": assignment.policy_version,
                        "analysis_behavior_hash": assignment.producer_behavior_id,
                        "analysis_input_hash": assignment.analysis_input_hash,
                        "output_schema_hash": assignment.output_schema_hash,
                    },
                )
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return AnalystResult(False, None, "CODEX_BUNDLE_INVALID")
        return self.router.run(bundle)


@dataclass(slots=True)
class QuantContextPosteriorRunner:
    policy: QuantContextPosteriorPolicy
    producer_behavior_id: str
    repository: SqlQuantContextPosteriorRepository
    contracts: SqlForecastContractStore
    forecasts: SqlForecastStore
    market: MarketDataStore
    analyst: CodexQuantContextPosteriorAnalyst
    maximum_quote_age_seconds: int
    stability_policy: ContextForecastStabilityPolicy | None = None
    stability_repository: SqlContextForecastStabilityRepository | None = None

    def reconcile(self, *, as_of: datetime) -> QuantContextPosteriorReport:
        now = require_utc(as_of)
        assignments = self.repository.pending_assignments(
            policy_version=self.policy.version,
            producer_behavior_id=self.producer_behavior_id,
        )
        for assignment in assignments:
            if self._pending_targets(assignment):
                self._run_assignment(assignment, as_of=now)
        assignment_count, forecasts, no_estimates, pending = self.repository.counts(
            policy_version=self.policy.version,
            producer_behavior_id=self.producer_behavior_id,
        )
        return QuantContextPosteriorReport(
            policy_version=self.policy.version,
            producer_behavior_id=self.producer_behavior_id,
            as_of=now,
            assignment_count=assignment_count,
            forecast_count=forecasts,
            no_estimate_count=no_estimates,
            pending_count=pending,
        )

    def _run_assignment(
        self,
        assignment: QuantContextPosteriorAssignment,
        *,
        as_of: datetime,
    ) -> None:
        missing_quant = tuple(
            item for item in self._pending_targets(assignment) if not item.quant_available
        )
        for target in missing_quant:
            self._record_no_estimate(
                target,
                reason=ForecastNoEstimateReason.REQUIRED_FEATURE_MISSING,
                completed_at=max(as_of, target.slot.slot_as_of),
                detail=f"QUANT_PRIOR_UNAVAILABLE:{target.quant_reason.value}",
                input_refs=target.quant_input_refs,
            )
        analysis_targets = tuple(
            item for item in self._pending_targets(assignment) if item.quant_available
        )
        if not analysis_targets:
            return
        if as_of >= assignment.completion_deadline_at:
            for target in analysis_targets:
                self._record_no_estimate(
                    target,
                    reason=ForecastNoEstimateReason.DEADLINE_MISSED,
                    completed_at=as_of,
                    detail="QUANT_POSTERIOR_ASSIGNMENT_DEADLINE_EXCEEDED",
                    input_refs=target.quant_input_refs,
                )
            return
        if self.stability_policy is not None:
            if self.stability_repository is None:
                raise ValueError("Quant posterior stability 缺少统一证据仓库")
            stability_assignment = preregister_context_forecast_stability(
                policy=self.stability_policy,
                repository=self.stability_repository,
                slot=analysis_targets[0].slot,
                producer_behavior_id=assignment.producer_behavior_id,
                analysis_input=_canonical_object(
                    assignment.analysis_input_json,
                    "posterior input",
                ),
                output_schema=_canonical_object(
                    assignment.output_schema_json,
                    "posterior schema",
                ),
                assigned_at=as_of,
            )
            if (
                stability_assignment is not None
                and stability_assignment.formal_prompt != assignment.prompt
            ):
                raise ValueError("Quant posterior stability 未冻结正式 prompt")
        result = self.analyst.estimate(assignment)
        completed_at = max(require_utc(result.completed_at or as_of), as_of)
        if (
            completed_at > assignment.completion_deadline_at
            or not result.success
            or not isinstance(
                result.output,
                (ContextForecastStructuredOutput, QuantContextPosteriorStructuredOutput),
            )
        ):
            reason = (
                ForecastNoEstimateReason.DEADLINE_MISSED
                if completed_at > assignment.completion_deadline_at
                else ForecastNoEstimateReason.PRODUCER_FAILED
            )
            detail = (
                "QUANT_POSTERIOR_COMPLETION_DEADLINE_EXCEEDED"
                if reason == ForecastNoEstimateReason.DEADLINE_MISSED
                else result.reason_code
            )
            for target in analysis_targets:
                self._record_no_estimate(
                    target,
                    reason=reason,
                    completed_at=completed_at,
                    detail=detail,
                    input_refs=target.quant_input_refs,
                )
            return
        drafts = {item.decision_slot_id: item for item in result.output.forecasts}
        expected = {item.slot.slot_id for item in assignment.analysis_targets}
        if len(drafts) != len(result.output.forecasts) or set(drafts) != expected:
            for target in analysis_targets:
                self._record_no_estimate(
                    target,
                    reason=ForecastNoEstimateReason.PRODUCER_FAILED,
                    completed_at=completed_at,
                    detail="QUANT_POSTERIOR_TARGET_SET_INVALID",
                    input_refs=target.quant_input_refs,
                )
            return
        analysis_input = _canonical_object(assignment.analysis_input_json, "posterior input")
        for target in analysis_targets:
            if target.slot.evaluation_at - completed_at < timedelta(
                minutes=target.contract.minimum_remaining_horizon_minutes
            ):
                self._record_no_estimate(
                    target,
                    reason=ForecastNoEstimateReason.INSUFFICIENT_REMAINING_HORIZON,
                    completed_at=completed_at,
                    detail=None,
                    input_refs=target.quant_input_refs,
                )
                continue
            quote = self._quote(target.instrument, at=completed_at)
            if quote is None or self._quote_age(target.instrument, quote, completed_at) > (
                self.maximum_quote_age_seconds
            ):
                self._record_no_estimate(
                    target,
                    reason=ForecastNoEstimateReason.STALE_BEFORE_AVAILABLE,
                    completed_at=completed_at,
                    detail="QUANT_POSTERIOR_ENTRY_QUOTE_MISSING_OR_STALE",
                    input_refs=target.quant_input_refs,
                )
                continue
            try:
                if target.quant_forecast_id is None:
                    raise ValueError("Quant posterior target 缺少来源 Quant prior")
                quant_prior = self.forecasts.forecast(target.quant_forecast_id)
                if not isinstance(quant_prior, BaseForecast):
                    raise ValueError("Quant posterior 来源 Quant prior 不可用")
                audited_draft = audit_quant_context_posterior_draft(
                    draft=drafts[target.slot.slot_id],
                    quant_prior=quant_prior,
                    analysis_input=analysis_input,
                )
                forecast = finalize_context_base_forecast(
                    binding=target.binding,
                    contract=target.contract,
                    slot=target.slot,
                    draft=audited_draft,
                    analysis_input=analysis_input,
                    input_observed_at=target.input_observed_at,
                    available_at=completed_at,
                    entry_prices=self._anchors(
                        target.instrument,
                        quote=quote,
                        available_at=completed_at,
                    ),
                    input_refs=tuple(
                        sorted(
                            {
                                assignment.assignment_id,
                                assignment.source_hash,
                                *target.quant_input_refs,
                            }
                        )
                    ),
                )
                self.forecasts.record(forecast)
            except ValueError:
                self._record_no_estimate(
                    target,
                    reason=ForecastNoEstimateReason.PRODUCER_FAILED,
                    completed_at=completed_at,
                    detail="QUANT_POSTERIOR_OUTPUT_CONTRACT_INVALID",
                    input_refs=target.quant_input_refs,
                )

    def _pending_targets(
        self,
        assignment: QuantContextPosteriorAssignment,
    ) -> tuple[QuantContextPosteriorTarget, ...]:
        return tuple(item for item in assignment.targets if self._terminal(item) == (None, None))

    def _terminal(
        self,
        target: QuantContextPosteriorTarget,
    ) -> tuple[BaseForecast | None, ForecastNoEstimate | None]:
        result = self.forecasts.result_for_behavior(
            decision_slot_id=target.slot.slot_id,
            producer_behavior_id=target.binding.producer_behavior_id,
        )
        absence = self.contracts.no_estimate(
            stable_id(
                "forecast_no_estimate",
                target.slot.slot_id,
                target.binding.producer_behavior_id,
            )
        )
        return (result if isinstance(result, BaseForecast) else None, absence)

    def _record_no_estimate(
        self,
        target: QuantContextPosteriorTarget,
        *,
        reason: ForecastNoEstimateReason,
        completed_at: datetime,
        detail: str | None,
        input_refs: tuple[str, ...],
    ) -> None:
        self.contracts.record_no_estimate(
            ForecastNoEstimate(
                result_id=stable_id(
                    "forecast_no_estimate",
                    target.slot.slot_id,
                    target.binding.producer_behavior_id,
                ),
                slot_id=target.slot.slot_id,
                contract_id=target.contract.contract_id,
                producer_kind=target.binding.producer_kind,
                producer_id=target.binding.producer_id,
                producer_behavior_id=target.binding.producer_behavior_id,
                reason=reason,
                information_cutoff_at=target.slot.information_cutoff_at,
                attempted_at=target.slot.slot_as_of,
                completed_at=max(require_utc(completed_at), target.slot.slot_as_of),
                input_refs=tuple(sorted(set(input_refs))),
                detail=detail,
            )
        )

    def _quote(self, instrument: InstrumentId, *, at: datetime):
        if instrument.product == InstrumentProduct.SPOT:
            return self.market.latest_spot_quote(
                instrument=instrument,
                evaluation_at=at,
                visible_at=at,
            )
        return self.market.latest_perpetual_quote(
            instrument=instrument,
            evaluation_at=at,
            visible_at=at,
        )

    @staticmethod
    def _quote_age(instrument: InstrumentId, quote, at: datetime) -> float:
        observed_at = (
            quote.observed_at
            if instrument.product == InstrumentProduct.SPOT
            else quote.exchange_time
        )
        return max(0.0, (at - observed_at).total_seconds())

    @staticmethod
    def _anchors(
        instrument: InstrumentId,
        *,
        quote,
        available_at: datetime,
    ) -> tuple[ForecastPriceAnchor, ...]:
        return (
            ForecastPriceAnchor(
                instrument_id=instrument.key,
                price=quote.ask,
                observed_at=(
                    quote.observed_at
                    if instrument.product == InstrumentProduct.SPOT
                    else quote.exchange_time
                ),
                available_at=available_at,
                quote_ref=quote.quote_id,
            ),
        )


def assemble_quant_context_posterior_allocator(
    config: AppConfig,
    *,
    engine: Engine,
    contracts: tuple[ForecastContract, ...],
    target_state_behaviors: tuple[
        tuple[str, ContextForecastTargetStateBehavior], ...
    ],
    quant_producer_behavior_id: str,
    producer_activation_at: datetime,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> QuantContextPosteriorAllocator | None:
    policy = config.outcome_evaluation.quant_context_posterior
    context = config.capital.context_forecast
    if policy is None or not policy.enabled:
        return None
    if context is None or not context.enabled:
        raise ValueError("Quant Context posterior 必须绑定正式 Context Forecast")
    store = SqlForecastContractStore(engine)
    behavior_id = quant_context_posterior_behavior_id(
        config=config,
        contracts=contracts,
        target_state_behaviors=target_state_behaviors,
        quant_producer_behavior_id=quant_producer_behavior_id,
    )
    bindings = {}
    for contract in contracts:
        binding = ForecastProducerBinding.create(
            contract_id=contract.contract_id,
            producer_kind=ForecastProducerKind.CONTEXT,
            producer_id=policy.producer_id,
            producer_behavior_id=behavior_id,
            permission=ForecastPermission.RESEARCH,
        )
        bindings[contract.contract_id] = store.resolve_binding(
            binding,
            activated_at=producer_activation_at,
        )
    return QuantContextPosteriorAllocator(
        policy=policy,
        formal_producer_behavior_id=quant_context_input_behavior_id(
            config=config,
            contracts=contracts,
            target_state_behaviors=target_state_behaviors,
        ),
        quant_producer_behavior_id=quant_producer_behavior_id,
        producer_behavior_id=behavior_id,
        bindings_by_contract=bindings,
        contracts=store,
        forecasts=SqlForecastStore(engine),
        repository=SqlQuantContextPosteriorRepository(engine),
        clock=clock,
    )


def assemble_quant_context_posterior_assignment_producer(
    config: AppConfig,
    *,
    engine: Engine,
    targets: tuple[ContextCapitalTargetDefinition, ...],
    quant_producer_behavior_id: str,
    producer_activation_at: datetime,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> QuantContextPosteriorAssignmentProducer | None:
    policy = config.outcome_evaluation.quant_context_posterior
    if policy is None or not policy.enabled:
        return None
    allocator = assemble_quant_context_posterior_allocator(
        config,
        engine=engine,
        contracts=tuple(item.contract for item in targets),
        target_state_behaviors=tuple(
            (item.contract.outcome_family_id, item.state_behavior)
            for item in targets
        ),
        quant_producer_behavior_id=quant_producer_behavior_id,
        producer_activation_at=producer_activation_at,
        clock=clock,
    )
    if allocator is None:
        raise ValueError("Quant Context posterior assignment 未启用")
    return QuantContextPosteriorAssignmentProducer(
        policy=policy,
        targets=targets,
        allocator=allocator,
        contexts=SqlContextAssessmentStore(engine),
        analysis_scope=config.assessment.mandate.analysis_scope,
        activated_at=producer_activation_at,
        clock=clock,
    )


def assemble_quant_context_posterior_runner(
    config: AppConfig,
    *,
    engine: Engine,
    contracts: tuple[ForecastContract, ...],
    target_state_behaviors: tuple[
        tuple[str, ContextForecastTargetStateBehavior], ...
    ],
    quant_producer_behavior_id: str,
) -> QuantContextPosteriorRunner | None:
    policy = config.outcome_evaluation.quant_context_posterior
    context = config.capital.context_forecast
    if policy is None or not policy.enabled:
        return None
    if context is None or not context.enabled:
        raise ValueError("Quant Context posterior 必须绑定正式 Context Forecast")
    behavior_id = quant_context_posterior_behavior_id(
        config=config,
        contracts=contracts,
        target_state_behaviors=target_state_behaviors,
        quant_producer_behavior_id=quant_producer_behavior_id,
    )
    runtime = context_forecast_runtime(config.codex_runtime, context)
    router = assemble_codex_router(
        config,
        leases=SqlAccountLeaseStore(engine),
        audit=SqlCodexAuditStore(engine),
        output_adapter=TypeAdapter(QuantContextPosteriorStructuredOutput),
        runtime_policy=runtime,
    )
    stability_policy = config.outcome_evaluation.context_forecast_stability
    if stability_policy is not None and not stability_policy.enabled:
        stability_policy = None
    return QuantContextPosteriorRunner(
        policy=policy,
        producer_behavior_id=behavior_id,
        repository=SqlQuantContextPosteriorRepository(engine),
        contracts=SqlForecastContractStore(engine),
        forecasts=SqlForecastStore(engine),
        market=SqlMarketDataStore(engine),
        analyst=CodexQuantContextPosteriorAnalyst(
            bundle_root=runtime.bundle_root,
            router=router,
            maximum_prompt_characters=runtime.maximum_prompt_characters,
        ),
        maximum_quote_age_seconds=context.maximum_quote_age_seconds,
        stability_policy=stability_policy,
        stability_repository=(
            SqlContextForecastStabilityRepository(engine)
            if stability_policy is not None
            else None
        ),
    )


def _posterior_output_schema(analysis_input: dict[str, object]) -> dict[str, object]:
    targets = analysis_input.get("forecast_targets")
    world_model = analysis_input.get("world_model")
    if not isinstance(targets, (list, tuple)) or not isinstance(world_model, dict):
        raise ValueError("Quant posterior 输入边界非法")
    if not targets:
        return QuantContextPosteriorStructuredOutput.model_json_schema()
    decision_slot_ids = []
    bucket_ids = set()
    for target in targets:
        if not isinstance(target, dict):
            raise ValueError("Quant posterior target 结构非法")
        decision_slot = target.get("decision_slot")
        contract = target.get("forecast_contract")
        if not isinstance(decision_slot, dict) or not isinstance(contract, dict):
            raise ValueError("Quant posterior target 边界缺失")
        decision_slot_ids.append(str(decision_slot["decision_slot_id"]))
        bucket_ids.update(str(item["bucket_id"]) for item in contract["outcome_buckets"])
    mechanisms = world_model.get("mechanisms")
    events = world_model.get("event_references")
    if not isinstance(mechanisms, (list, tuple)) or not isinstance(events, (list, tuple)):
        raise ValueError("Quant posterior WorldModel 结构非法")
    mechanism_ids = tuple(str(item["mechanism_id"]) for item in mechanisms)
    evidence_ids = {
        str(item["evidence_id"])
        for item in events
        if isinstance(item, dict) and "evidence_id" in item
    }
    for mechanism in mechanisms:
        if isinstance(mechanism, dict):
            evidence_ids.update(str(item) for item in mechanism.get("evidence_ids", ()))
            evidence_ids.update(str(item) for item in mechanism.get("conflicting_evidence_ids", ()))
    return context_forecast_output_schema_for_ids(
        decision_slot_ids=tuple(decision_slot_ids),
        bucket_ids=tuple(sorted(bucket_ids)),
        mechanism_ids=mechanism_ids,
        evidence_ids=tuple(sorted(evidence_ids)),
        output_model_schema=QuantContextPosteriorStructuredOutput.model_json_schema(),
        draft_definition="QuantContextPosteriorDraft",
    )


def _canonical_object(raw: str, name: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} 不是有效 JSON") from exc
    if not isinstance(value, dict) or canonical_json(value) != raw:
        raise ValueError(f"{name} 必须是规范 JSON 对象")
    return value


__all__ = [
    "QuantContextPosteriorAllocator",
    "QuantContextPosteriorAssignment",
    "QuantContextPosteriorAssignmentProducer",
    "QuantContextPosteriorReport",
    "QuantContextPosteriorRunner",
    "QuantContextPosteriorTarget",
    "SqlQuantContextPosteriorRepository",
    "assemble_quant_context_posterior_allocator",
    "assemble_quant_context_posterior_assignment_producer",
    "assemble_quant_context_posterior_runner",
    "audit_quant_context_posterior_draft",
    "build_quant_context_posterior_assignment",
    "quant_context_input_behavior_id",
    "quant_context_posterior_behavior_id",
]
