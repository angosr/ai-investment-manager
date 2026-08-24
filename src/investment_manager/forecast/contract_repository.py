from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.forecast.contracts import (
    ForecastContract,
    ForecastDecisionSlot,
    ForecastNoEstimate,
    ForecastProducerBinding,
    ForecastSlotObligation,
)
from investment_manager.forecast.tables import (
    forecast_contracts,
    forecast_decision_slots,
    forecast_no_estimates,
    forecast_producer_bindings,
    forecast_slot_obligations,
    forecasts,
)
from investment_manager.kernel.time import require_utc
from investment_manager.platform.time import database_utc


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

    def record_binding(
        self,
        binding: ForecastProducerBinding,
        *,
        activated_at: datetime,
    ) -> bool:
        if self.contract(binding.contract_id) is None:
            raise ValueError("ForecastProducerBinding 缺少已持久化 ForecastContract")
        activated_at = require_utc(activated_at)
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
                "activated_at": activated_at,
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

    def resolve_binding(
        self,
        binding: ForecastProducerBinding,
        *,
        activated_at: datetime,
    ) -> ForecastProducerBinding:
        """Reuse the immutable binding for one contract/behavior, or record it once."""

        with self._engine.connect() as connection:
            payload = connection.execute(
                select(forecast_producer_bindings.c.payload).where(
                    forecast_producer_bindings.c.contract_id == binding.contract_id,
                    forecast_producer_bindings.c.producer_behavior_id
                    == binding.producer_behavior_id,
                )
            ).scalar_one_or_none()
        if payload is None:
            self.record_binding(binding, activated_at=activated_at)
            return binding
        existing = ForecastProducerBinding.model_validate(payload)
        comparable_fields = (
            "contract_id",
            "producer_kind",
            "producer_id",
            "producer_behavior_id",
            "permission",
            "required_feature_keys",
        )
        if any(
            getattr(existing, field) != getattr(binding, field)
            for field in comparable_fields
        ):
            raise ValueError("ForecastProducerBinding 行为身份已绑定到不同语义")
        return existing

    def binding_activation_at(self, binding_id: str) -> datetime:
        with self._engine.connect() as connection:
            value = connection.execute(
                select(forecast_producer_bindings.c.activated_at).where(
                    forecast_producer_bindings.c.binding_id == binding_id
                )
            ).scalar_one_or_none()
        if value is None:
            raise KeyError(binding_id)
        return database_utc(value)

    def record_slot(
        self,
        slot: ForecastDecisionSlot,
        *,
        binding: ForecastProducerBinding,
    ) -> bool:
        contract = self.contract(slot.contract_id)
        if contract is None:
            raise ValueError("ForecastDecisionSlot 缺少已持久化 ForecastContract")
        authoritative_binding = self.binding(binding.binding_id)
        if authoritative_binding != binding or binding.contract_id != slot.contract_id:
            raise ValueError("ForecastDecisionSlot 缺少匹配的 ProducerBinding")
        if slot.evaluation_at != slot.information_cutoff_at + timedelta(
            minutes=contract.horizon_minutes
        ):
            raise ValueError("ForecastDecisionSlot evaluation_at 与合同 horizon 不一致")
        expected_outcome_start = (
            None
            if contract.outcome_start_delay_seconds is None
            else slot.completion_deadline_at
            + timedelta(seconds=contract.outcome_start_delay_seconds)
        )
        if slot.outcome_start_at != expected_outcome_start:
            raise ValueError("ForecastDecisionSlot Outcome 起点与合同不一致")
        if slot.cutoff_prices and tuple(item.instrument_id for item in slot.cutoff_prices) != tuple(
            item.instrument.key for item in contract.target.legs
        ):
            raise ValueError("ForecastDecisionSlot cutoff prices 未完整覆盖合同 Target")
        obligation = ForecastSlotObligation.create(slot=slot, binding=binding)
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(forecast_decision_slots).values(
                        slot_id=slot.slot_id,
                        contract_id=slot.contract_id,
                        slot_as_of=slot.slot_as_of,
                        information_cutoff_at=slot.information_cutoff_at,
                        completion_deadline_at=slot.completion_deadline_at,
                        evaluation_at=slot.evaluation_at,
                        payload=slot.model_dump(mode="json"),
                    )
                )
                self._insert_obligation(connection, obligation)
            return True
        except IntegrityError:
            existing = self.slot(slot.slot_id)
            if existing != slot:
                raise ValueError("ForecastDecisionSlot slot_id 已存在且内容不同") from None
            self.record_obligation(slot=slot, binding=binding)
            return False

    def record_obligation(
        self,
        *,
        slot: ForecastDecisionSlot,
        binding: ForecastProducerBinding,
    ) -> bool:
        if self.slot(slot.slot_id) != slot or self.binding(binding.binding_id) != binding:
            raise ValueError("ForecastSlotObligation 缺少权威 Slot/Binding")
        if binding.contract_id != slot.contract_id:
            raise ValueError("ForecastSlotObligation 的 Slot/Binding 合同不一致")
        obligation = ForecastSlotObligation.create(slot=slot, binding=binding)
        existing = self.obligation(obligation.obligation_id)
        if existing is not None:
            if existing != obligation:
                raise ValueError("ForecastSlotObligation 已存在且内容不同")
            return False
        try:
            with self._engine.begin() as connection:
                existing_result = connection.execute(
                    select(forecasts.c.forecast_id).where(
                        forecasts.c.decision_slot_id == slot.slot_id,
                        forecasts.c.producer_behavior_id == binding.producer_behavior_id,
                    )
                ).first()
                existing_absence = connection.execute(
                    select(forecast_no_estimates.c.result_id).where(
                        forecast_no_estimates.c.slot_id == slot.slot_id,
                        forecast_no_estimates.c.producer_behavior_id
                        == binding.producer_behavior_id,
                    )
                ).first()
                if existing_result is not None or existing_absence is not None:
                    raise ValueError("不能在 Forecast 结果形成后补建槽义务")
                self._insert_obligation(connection, obligation)
            return True
        except IntegrityError:
            existing = self.obligation(obligation.obligation_id)
            if existing != obligation:
                raise ValueError("ForecastSlotObligation 已存在且内容不同") from None
            return False

    def obligation(self, obligation_id: str) -> ForecastSlotObligation | None:
        payload = self._payload(
            forecast_slot_obligations,
            forecast_slot_obligations.c.obligation_id,
            obligation_id,
        )
        return None if payload is None else ForecastSlotObligation.model_validate(payload)

    def latest_obligated_slot_at(self, *, binding_id: str) -> datetime | None:
        with self._engine.connect() as connection:
            return connection.execute(
                select(forecast_decision_slots.c.slot_as_of)
                .select_from(
                    forecast_slot_obligations.join(
                        forecast_decision_slots,
                        forecast_decision_slots.c.slot_id
                        == forecast_slot_obligations.c.slot_id,
                    )
                )
                .where(forecast_slot_obligations.c.binding_id == binding_id)
                .order_by(forecast_decision_slots.c.slot_as_of.desc())
                .limit(1)
            ).scalar_one_or_none()

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
            obligation = connection.execute(
                select(forecast_slot_obligations.c.payload).where(
                    forecast_slot_obligations.c.slot_id == result.slot_id,
                    forecast_slot_obligations.c.producer_behavior_id
                    == result.producer_behavior_id,
                )
            ).scalar_one_or_none()
            if obligation is None:
                raise ValueError("ForecastNoEstimate 缺少事前槽义务")
            duty = ForecastSlotObligation.model_validate(obligation)
            if (
                duty.contract_id != result.contract_id
                or duty.producer_kind != result.producer_kind
                or duty.producer_id != result.producer_id
            ):
                raise ValueError("ForecastNoEstimate 与槽义务身份不一致")
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

    @staticmethod
    def _insert_obligation(connection, obligation: ForecastSlotObligation) -> None:
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
