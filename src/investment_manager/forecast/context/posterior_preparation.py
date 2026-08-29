"""Point-in-time preparation and obligation recording for context posterior calls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from investment_manager.forecast.context.posterior_contract import (
    POSTERIOR_PRODUCER_ID,
    ContextPosteriorInput,
    PosteriorPriorTarget,
    posterior_behavior_hash,
)
from investment_manager.forecast.context.repository import SqlContextAssessmentStore
from investment_manager.forecast.contract_repository import SqlForecastContractStore
from investment_manager.forecast.contracts import (
    ForecastContract,
    ForecastNoEstimate,
    ForecastNoEstimateReason,
    ForecastPermission,
    ForecastProducerBinding,
    ForecastProducerKind,
)
from investment_manager.forecast.policy import CodexRuntimePolicy
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.forecast.results import BaseForecast
from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.time import require_utc

PriorResult = BaseForecast | ForecastNoEstimate


@dataclass(frozen=True, slots=True)
class ContextPosteriorPreparation:
    contracts: tuple[ForecastContract, ...]
    runtime: CodexRuntimePolicy
    analysis_scope: str
    activated_at: datetime
    contract_store: SqlForecastContractStore
    forecast_store: SqlForecastStore
    assessments: SqlContextAssessmentStore

    def __post_init__(self) -> None:
        require_utc(self.activated_at)
        contract_ids = tuple(item.contract_id for item in self.contracts)
        if tuple(sorted(set(contract_ids))) != contract_ids:
            raise ValueError("Posterior contracts 必须按唯一 ID 排序")

    @property
    def producer_behavior_id(self) -> str:
        return posterior_behavior_hash(self.runtime, contracts=self.contracts)

    def binding(self, contract: ForecastContract) -> ForecastProducerBinding:
        if contract.contract_id not in {item.contract_id for item in self.contracts}:
            raise ValueError("Posterior contract 不属于冻结联合行为")
        return ForecastProducerBinding.create(
            contract_id=contract.contract_id,
            producer_kind=ForecastProducerKind.CONTEXT,
            producer_id=POSTERIOR_PRODUCER_ID,
            producer_behavior_id=self.producer_behavior_id,
            permission=ForecastPermission.RESEARCH,
        )

    def prepare(
        self,
        prior_results: tuple[PriorResult, ...],
        *,
        as_of: datetime,
    ) -> ContextPosteriorInput | None:
        attempted_at = require_utc(as_of)
        if not prior_results:
            return None
        by_contract = {item.contract_id: item for item in prior_results}
        expected_ids = tuple(item.contract_id for item in self.contracts)
        if tuple(sorted(by_contract)) != expected_ids or len(by_contract) != len(prior_results):
            raise ValueError("Posterior 必须接收冻结联合行为的全部 prior 结果")
        cutoffs = {item.information_cutoff_at for item in prior_results}
        if len(cutoffs) != 1:
            raise ValueError("Posterior prior 结果必须共享信息截止")
        cutoff = next(iter(cutoffs))
        if cutoff < self.activated_at:
            return None

        targets: list[PosteriorPriorTarget] = []
        for contract in self.contracts:
            result = by_contract[contract.contract_id]
            slot = self.contract_store.slot(
                result.decision_slot_id if isinstance(result, BaseForecast) else result.slot_id
            )
            if slot is None:
                raise ValueError("Posterior prior 缺少权威决策槽")
            binding = self.contract_store.resolve_binding(
                self.binding(contract),
                activated_at=self.activated_at,
            )
            self.contract_store.record_obligation(slot=slot, binding=binding)
            if self._terminal(slot.slot_id):
                continue
            if isinstance(result, ForecastNoEstimate):
                self._record_no_estimate(
                    contract=contract,
                    slot_id=slot.slot_id,
                    information_cutoff_at=cutoff,
                    attempted_at=attempted_at,
                    reason=ForecastNoEstimateReason.REQUIRED_FEATURE_MISSING,
                    input_refs=(result.result_id,),
                    detail=f"PRIOR_UNAVAILABLE:{result.reason.value}",
                )
                continue
            if attempted_at > slot.completion_deadline_at:
                self._record_no_estimate(
                    contract=contract,
                    slot_id=slot.slot_id,
                    information_cutoff_at=cutoff,
                    attempted_at=attempted_at,
                    reason=ForecastNoEstimateReason.DEADLINE_MISSED,
                    input_refs=(result.forecast_id,),
                    detail="POSTERIOR_PREPARATION_DEADLINE_MISSED",
                )
                continue
            targets.append(PosteriorPriorTarget(contract=contract, slot=slot, prior=result))
        if not targets:
            return None

        world_model = self.assessments.latest_before(
            analysis_scope=self.analysis_scope,
            as_of=cutoff,
        )
        if world_model is None:
            self._close_targets(
                targets,
                attempted_at=attempted_at,
                reason=ForecastNoEstimateReason.WORLD_MODEL_UNAVAILABLE,
                detail="NO_WORLD_MODEL_AVAILABLE_AT_CUTOFF",
            )
            return None
        if min(item.next_review_at for item in world_model.mechanisms) <= cutoff:
            self._close_targets(
                targets,
                attempted_at=attempted_at,
                reason=ForecastNoEstimateReason.WORLD_MODEL_STALE,
                detail="WORLD_MODEL_REVIEW_OVERDUE_AT_CUTOFF",
                extra_refs=(world_model.assessment_id,),
            )
            return None
        packet = self.assessments.packet_for_assessment(world_model.assessment_id)
        if packet is None:
            self._close_targets(
                targets,
                attempted_at=attempted_at,
                reason=ForecastNoEstimateReason.WORLD_MODEL_UNAVAILABLE,
                detail="WORLD_MODEL_PACKET_UNAVAILABLE",
                extra_refs=(world_model.assessment_id,),
            )
            return None
        structural_evidence = {
            *(item.revision_id for item in packet.facts),
            *(
                item.evidence_ref
                for item in packet.intelligence_events
                if item.directional_support_eligible
            ),
        }
        eligible = tuple(
            sorted(
                mechanism.mechanism_id
                for mechanism in world_model.mechanisms
                if structural_evidence
                & {
                    *(ref for node in mechanism.causal_chain for ref in node.evidence_ids),
                    *mechanism.conflicting_evidence_ids,
                }
            )
        )
        observations = tuple(
            item
            for item in self.assessments.mechanism_observations(world_model.assessment_id)
            if item.observed_at <= cutoff
        )
        return ContextPosteriorInput.create(
            information_cutoff_at=cutoff,
            world_model=world_model,
            mechanism_observations=observations,
            eligible_mechanism_ids=eligible,
            targets=tuple(targets),
        )

    def _terminal(self, slot_id: str) -> bool:
        if (
            self.forecast_store.result_for_behavior(
                decision_slot_id=slot_id,
                producer_behavior_id=self.producer_behavior_id,
            )
            is not None
        ):
            return True
        return self.forecast_store.no_estimate_exists(
            decision_slot_id=slot_id,
            producer_behavior_id=self.producer_behavior_id,
        )

    def _close_targets(
        self,
        targets: list[PosteriorPriorTarget],
        *,
        attempted_at: datetime,
        reason: ForecastNoEstimateReason,
        detail: str,
        extra_refs: tuple[str, ...] = (),
    ) -> None:
        for target in targets:
            self._record_no_estimate(
                contract=target.contract,
                slot_id=target.slot.slot_id,
                information_cutoff_at=target.slot.information_cutoff_at,
                attempted_at=attempted_at,
                reason=reason,
                input_refs=(target.prior.forecast_id, *extra_refs),
                detail=detail,
            )

    def _record_no_estimate(
        self,
        *,
        contract: ForecastContract,
        slot_id: str,
        information_cutoff_at: datetime,
        attempted_at: datetime,
        reason: ForecastNoEstimateReason,
        input_refs: tuple[str, ...],
        detail: str,
    ) -> None:
        if self._terminal(slot_id):
            return
        result = ForecastNoEstimate(
            result_id=stable_id(
                "forecast_no_estimate",
                slot_id,
                self.producer_behavior_id,
            ),
            slot_id=slot_id,
            contract_id=contract.contract_id,
            producer_kind=ForecastProducerKind.CONTEXT,
            producer_id=POSTERIOR_PRODUCER_ID,
            producer_behavior_id=self.producer_behavior_id,
            reason=reason,
            information_cutoff_at=information_cutoff_at,
            attempted_at=attempted_at,
            completed_at=attempted_at,
            input_refs=tuple(sorted(set(input_refs))),
            detail=detail,
        )
        self.contract_store.record_no_estimate(result)
