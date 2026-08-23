from __future__ import annotations

from datetime import timedelta

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.forecast.contracts import (
    ForecastContract,
    ForecastDecisionSlot,
    ForecastNoEstimate,
    ForecastProducerBinding,
)
from investment_manager.forecast.tables import (
    forecast_contracts,
    forecast_decision_slots,
    forecast_no_estimates,
    forecast_producer_bindings,
    forecasts,
)


class SqlForecastContractStore:
    """Immutable contract, cohort-slot, producer-binding, and absence ledger."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record_contract(self, contract: ForecastContract) -> bool:
        return self._insert_or_verify(
            table=forecast_contracts,
            identity_column=forecast_contracts.c.contract_id,
            identity=contract.contract_id,
            values={
                "contract_id": contract.contract_id,
                "contract_version": contract.contract_version,
                "outcome_family_id": contract.outcome_family_id,
                "target_id": contract.target.target_id,
                "horizon_minutes": contract.horizon_minutes,
                "payload": contract.model_dump(mode="json"),
            },
            expected=contract,
            model=ForecastContract,
            conflict="ForecastContract contract_id 已存在且内容不同",
        )

    def contract(self, contract_id: str) -> ForecastContract | None:
        payload = self._payload(
            forecast_contracts,
            forecast_contracts.c.contract_id,
            contract_id,
        )
        return None if payload is None else ForecastContract.model_validate(payload)

    def record_binding(self, binding: ForecastProducerBinding) -> bool:
        if self.contract(binding.contract_id) is None:
            raise ValueError("ForecastProducerBinding 缺少已持久化 ForecastContract")
        return self._insert_or_verify(
            table=forecast_producer_bindings,
            identity_column=forecast_producer_bindings.c.binding_id,
            identity=binding.binding_id,
            values={
                "binding_id": binding.binding_id,
                "contract_id": binding.contract_id,
                "producer_kind": binding.producer_kind.value,
                "producer_id": binding.producer_id,
                "producer_behavior_id": binding.producer_behavior_id,
                "permission": binding.permission.value,
                "payload": binding.model_dump(mode="json"),
            },
            expected=binding,
            model=ForecastProducerBinding,
            conflict="ForecastProducerBinding binding_id 已存在且内容不同",
        )

    def binding(self, binding_id: str) -> ForecastProducerBinding | None:
        payload = self._payload(
            forecast_producer_bindings,
            forecast_producer_bindings.c.binding_id,
            binding_id,
        )
        return None if payload is None else ForecastProducerBinding.model_validate(payload)

    def record_slot(self, slot: ForecastDecisionSlot) -> bool:
        contract = self.contract(slot.contract_id)
        if contract is None:
            raise ValueError("ForecastDecisionSlot 缺少已持久化 ForecastContract")
        if slot.evaluation_at != slot.information_cutoff_at + timedelta(
            minutes=contract.horizon_minutes
        ):
            raise ValueError("ForecastDecisionSlot evaluation_at 与合同 horizon 不一致")
        if slot.cutoff_prices and tuple(item.instrument_id for item in slot.cutoff_prices) != tuple(
            item.instrument.key for item in contract.target.legs
        ):
            raise ValueError("ForecastDecisionSlot cutoff prices 未完整覆盖合同 Target")
        return self._insert_or_verify(
            table=forecast_decision_slots,
            identity_column=forecast_decision_slots.c.slot_id,
            identity=slot.slot_id,
            values={
                "slot_id": slot.slot_id,
                "contract_id": slot.contract_id,
                "slot_as_of": slot.slot_as_of,
                "information_cutoff_at": slot.information_cutoff_at,
                "completion_deadline_at": slot.completion_deadline_at,
                "evaluation_at": slot.evaluation_at,
                "payload": slot.model_dump(mode="json"),
            },
            expected=slot,
            model=ForecastDecisionSlot,
            conflict="ForecastDecisionSlot slot_id 已存在且内容不同",
        )

    def slot(self, slot_id: str) -> ForecastDecisionSlot | None:
        payload = self._payload(
            forecast_decision_slots,
            forecast_decision_slots.c.slot_id,
            slot_id,
        )
        return None if payload is None else ForecastDecisionSlot.model_validate(payload)

    def record_no_estimate(self, result: ForecastNoEstimate) -> bool:
        slot = self.slot(result.slot_id)
        if slot is None or slot.contract_id != result.contract_id:
            raise ValueError("ForecastNoEstimate 缺少匹配的 ForecastDecisionSlot")
        with self._engine.connect() as connection:
            existing_forecast = connection.execute(
                select(forecasts.c.forecast_id).where(
                    forecasts.c.decision_slot_id == result.slot_id,
                    forecasts.c.producer_behavior_id == result.producer_behavior_id,
                )
            ).scalar_one_or_none()
        if existing_forecast is not None:
            raise ValueError("同一 decision slot/producer 已记录 Forecast")
        return self._insert_or_verify(
            table=forecast_no_estimates,
            identity_column=forecast_no_estimates.c.result_id,
            identity=result.result_id,
            values={
                "result_id": result.result_id,
                "slot_id": result.slot_id,
                "contract_id": result.contract_id,
                "producer_kind": result.producer_kind.value,
                "producer_id": result.producer_id,
                "producer_behavior_id": result.producer_behavior_id,
                "reason": result.reason.value,
                "completed_at": result.completed_at,
                "payload": result.model_dump(mode="json"),
            },
            expected=result,
            model=ForecastNoEstimate,
            conflict="ForecastNoEstimate result_id 已存在且内容不同",
        )

    def no_estimate(self, result_id: str) -> ForecastNoEstimate | None:
        payload = self._payload(
            forecast_no_estimates,
            forecast_no_estimates.c.result_id,
            result_id,
        )
        return None if payload is None else ForecastNoEstimate.model_validate(payload)

    def _payload(self, table, identity_column, identity: str):
        with self._engine.connect() as connection:
            return connection.execute(
                select(table.c.payload).where(identity_column == identity)
            ).scalar_one_or_none()

    def _insert_or_verify(
        self,
        *,
        table,
        identity_column,
        identity: str,
        values: dict[str, object],
        expected,
        model,
        conflict: str,
    ) -> bool:
        try:
            with self._engine.begin() as connection:
                connection.execute(insert(table).values(**values))
            return True
        except IntegrityError:
            payload = self._payload(table, identity_column, identity)
            if payload is None or model.model_validate(payload) != expected:
                raise ValueError(conflict) from None
            return False
