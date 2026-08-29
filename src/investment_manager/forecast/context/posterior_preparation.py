"""Point-in-time preparation and obligation recording for context posterior calls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from investment_manager.forecast.context.posterior_contract import (
    POSTERIOR_PRODUCER_ID,
    ContextPosteriorInput,
    ContextPosteriorSeed,
    PosteriorPriorTarget,
    posterior_behavior_hash,
)
from investment_manager.forecast.contract_repository import SqlForecastContractStore
from investment_manager.forecast.contracts import (
    ForecastContract,
    ForecastNoEstimate,
    ForecastNoEstimateReason,
    ForecastPermission,
    ForecastProducerBinding,
    ForecastProducerKind,
)
from investment_manager.forecast.models import ContextAssessment
from investment_manager.forecast.policy import CodexRuntimePolicy
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.forecast.results import BaseForecast
from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.state.decision.packet import DecisionPacket

PriorResult = BaseForecast | ForecastNoEstimate


@dataclass(frozen=True, slots=True)
class ContextPosteriorPreparation:
    contracts: tuple[ForecastContract, ...]
    prior_bindings: tuple[ForecastProducerBinding, ...]
    runtime: CodexRuntimePolicy
    world_model_behavior_id: str
    activated_at: datetime
    contract_store: SqlForecastContractStore
    forecast_store: SqlForecastStore

    def __post_init__(self) -> None:
        require_utc(self.activated_at)
        if len(self.world_model_behavior_id) != 64:
            raise ValueError("Posterior preparation 缺少 WorldModel 行为身份")
        contract_ids = tuple(item.contract_id for item in self.contracts)
        if tuple(sorted(set(contract_ids))) != contract_ids:
            raise ValueError("Posterior contracts 必须按唯一 ID 排序")
        prior_contract_ids = tuple(item.contract_id for item in self.prior_bindings)
        if prior_contract_ids != contract_ids or len(set(prior_contract_ids)) != len(
            prior_contract_ids
        ):
            raise ValueError("Posterior prior bindings 必须与 contracts 唯一且逐项对应")

    @property
    def producer_behavior_id(self) -> str:
        return posterior_behavior_hash(
            self.runtime,
            contracts=self.contracts,
            prior_bindings=self.prior_bindings,
            world_model_behavior_id=self.world_model_behavior_id,
        )

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

    def reserve(
        self,
        prior_results: tuple[PriorResult, ...],
        *,
        as_of: datetime,
    ) -> ContextPosteriorSeed | None:
        """Record obligations and freeze prior-side input without reading an old WorldModel."""

        attempted_at = require_utc(as_of)
        if not prior_results:
            return None
        by_contract = {item.contract_id: item for item in prior_results}
        expected_prior_bindings = {
            item.contract_id: item for item in self.prior_bindings
        }
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
            expected_prior = expected_prior_bindings[contract.contract_id]
            if (
                result.producer_id != expected_prior.producer_id
                or result.producer_behavior_id != expected_prior.producer_behavior_id
                or (
                    isinstance(result, ForecastNoEstimate)
                    and result.producer_kind != expected_prior.producer_kind
                )
            ):
                raise ValueError("Posterior prior 结果与冻结 ProducerBinding 不一致")
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
        return ContextPosteriorSeed.create(
            information_cutoff_at=cutoff,
            targets=tuple(targets),
        )

    @staticmethod
    def build_input(
        seed: ContextPosteriorSeed,
        *,
        world_model: ContextAssessment,
        packet: DecisionPacket,
    ) -> ContextPosteriorInput:
        """Bind the newly generated same-cutoff WorldModel to the reserved prior seed."""

        if packet.as_of != seed.information_cutoff_at:
            raise ValueError("Posterior WorldModel Packet 与 Forecast 信息截止不一致")
        if (
            world_model.as_of != seed.information_cutoff_at
            or world_model.decision_packet_hash != packet.content_hash
        ):
            raise ValueError("Posterior WorldModel 不属于冻结的同截止 Packet")
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
        return ContextPosteriorInput.create(
            information_cutoff_at=seed.information_cutoff_at,
            world_model=world_model,
            mechanism_observations=(),
            eligible_mechanism_ids=eligible,
            targets=seed.targets,
        )

    def close_seed(
        self,
        seed: ContextPosteriorSeed,
        *,
        attempted_at: datetime,
        reason: ForecastNoEstimateReason,
        detail: str,
        extra_refs: tuple[str, ...] = (),
    ) -> tuple[ForecastNoEstimate, ...]:
        """Close every reserved target after a failed same-cutoff world update."""

        completed = require_utc(attempted_at)
        results: list[ForecastNoEstimate] = []
        for target in seed.targets:
            absence_id = stable_id(
                "forecast_no_estimate",
                target.slot.slot_id,
                self.producer_behavior_id,
            )
            existing = self.contract_store.no_estimate(absence_id)
            if existing is not None:
                results.append(existing)
                continue
            if self.forecast_store.result_for_behavior(
                decision_slot_id=target.slot.slot_id,
                producer_behavior_id=self.producer_behavior_id,
            ) is not None:
                raise ValueError("已有 Posterior Forecast 时不能再关闭 seed")
            self._record_no_estimate(
                contract=target.contract,
                slot_id=target.slot.slot_id,
                information_cutoff_at=seed.information_cutoff_at,
                attempted_at=completed,
                reason=reason,
                input_refs=(target.prior.forecast_id, *extra_refs),
                detail=detail,
            )
            recorded = self.contract_store.no_estimate(absence_id)
            if recorded is None:
                raise RuntimeError("Posterior seed 关闭后缺少权威 NO_ESTIMATE")
            results.append(recorded)
        return tuple(results)

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
