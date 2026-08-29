"""Read one prospective Forecast producer's incremental value over its comparator."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine

from investment_manager.forecast.context.evaluation import (
    ForecastPairEvidence,
    ForecastPairPanelCase,
    evaluate_forecast_pair_evidence,
)
from investment_manager.forecast.contracts import (
    ForecastDecisionSlot,
    ForecastNoEstimate,
    ForecastSlotObligation,
)
from investment_manager.forecast.results import (
    BaseForecast,
    ForecastOutcome,
    ForecastOutcomeStatus,
    ForecastResultKind,
)
from investment_manager.forecast.scoring import (
    multiclass_brier_score,
    ordinal_ranked_probability_score,
)
from investment_manager.forecast.tables import (
    forecast_decision_slots,
    forecast_no_estimates,
    forecast_outcomes,
    forecast_producer_bindings,
    forecast_slot_obligations,
    forecasts,
)
from investment_manager.kernel.identity import stable_id


class ForecastIncrementStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    AWAITING_FORECAST = "AWAITING_FORECAST"
    AWAITING_SETTLEMENT = "AWAITING_SETTLEMENT"
    EVIDENCE_AVAILABLE = "EVIDENCE_AVAILABLE"


@dataclass(frozen=True, slots=True)
class ForecastIncrementEvidence:
    status: ForecastIncrementStatus
    candidate_producer_id: str
    comparator_producer_id: str
    candidate_behavior_id: str | None
    due_panel_count: int
    forecast_panel_count: int
    unavailable_panel_count: int
    pending_panel_count: int
    pair: ForecastPairEvidence


@dataclass(frozen=True, slots=True)
class SqlForecastIncrementEvidenceReader:
    """Read a behavior-coherent comparison from the shared immutable ledger."""

    engine: Engine
    outcome_evaluation_version: str
    candidate_producer_id: str
    comparator_producer_id: str

    def read(self) -> ForecastIncrementEvidence:
        behavior_id = self._latest_behavior_id()
        if behavior_id is None:
            return self._empty(ForecastIncrementStatus.NOT_STARTED, behavior_id=None)

        with self.engine.connect() as connection:
            expected_contracts = set(
                connection.execute(
                    select(forecast_producer_bindings.c.contract_id).where(
                        forecast_producer_bindings.c.producer_behavior_id == behavior_id,
                        forecast_producer_bindings.c.producer_id == self.candidate_producer_id,
                    )
                ).scalars()
            )
            obligations = tuple(
                ForecastSlotObligation.model_validate(item)
                for item in connection.execute(
                    select(forecast_slot_obligations.c.payload).where(
                        forecast_slot_obligations.c.producer_behavior_id == behavior_id
                    )
                ).scalars()
            )
            if not obligations:
                return self._empty(ForecastIncrementStatus.NOT_STARTED, behavior_id=behavior_id)
            slot_ids = tuple(sorted({item.slot_id for item in obligations}))
            slots = {
                item.slot_id: item
                for item in (
                    ForecastDecisionSlot.model_validate(payload)
                    for payload in connection.execute(
                        select(forecast_decision_slots.c.payload).where(
                            forecast_decision_slots.c.slot_id.in_(slot_ids)
                        )
                    ).scalars()
                )
            }
            candidate = self._base_forecasts(
                connection,
                slot_ids=slot_ids,
                producer_behavior_id=behavior_id,
            )
            comparator = self._forecasts_by_id(
                connection,
                slot_ids=slot_ids,
                producer_id=self.comparator_producer_id,
            )
            absences = {
                item.slot_id: item
                for item in (
                    ForecastNoEstimate.model_validate(payload)
                    for payload in connection.execute(
                        select(forecast_no_estimates.c.payload).where(
                            forecast_no_estimates.c.slot_id.in_(slot_ids),
                            forecast_no_estimates.c.producer_behavior_id == behavior_id,
                        )
                    ).scalars()
                )
            }
            outcomes = {
                item.decision_slot_id: item
                for item in (
                    ForecastOutcome.model_validate(payload)
                    for payload in connection.execute(
                        select(forecast_outcomes.c.payload).where(
                            forecast_outcomes.c.decision_slot_id.in_(slot_ids),
                            forecast_outcomes.c.evaluation_version
                            == self.outcome_evaluation_version,
                        )
                    ).scalars()
                )
            }

        if not expected_contracts:
            raise ValueError("Forecast 增量评价行为缺少已注册 Contract")
        panels: dict[datetime, list[ForecastSlotObligation]] = defaultdict(list)
        for obligation in obligations:
            slot = slots.get(obligation.slot_id)
            if slot is None:
                raise ValueError("Forecast 增量评价缺少权威 slot")
            panels[slot.information_cutoff_at].append(obligation)

        forecast_panels = unavailable_panels = pending_panels = 0
        pair_cases: list[ForecastPairPanelCase] = []
        for cutoff, panel_obligations in sorted(panels.items()):
            panel_contracts = {item.contract_id for item in panel_obligations}
            panel_slot_ids = {item.slot_id for item in panel_obligations}
            complete_obligation = panel_contracts == expected_contracts
            all_forecasts = complete_obligation and all(
                slot_id in candidate for slot_id in panel_slot_ids
            )
            all_terminal = complete_obligation and all(
                slot_id in candidate or slot_id in absences for slot_id in panel_slot_ids
            )
            if all_forecasts:
                forecast_panels += 1
            elif all_terminal:
                unavailable_panels += 1
            else:
                pending_panels += 1
            case = self._pair_case(
                behavior_id=behavior_id,
                cutoff=cutoff,
                obligations=tuple(panel_obligations),
                slots=slots,
                candidate=candidate,
                comparator=comparator,
                outcomes=outcomes,
            )
            if complete_obligation and case is not None:
                pair_cases.append(case)

        pair = evaluate_forecast_pair_evidence(tuple(pair_cases))
        status = (
            ForecastIncrementStatus.EVIDENCE_AVAILABLE
            if pair.settled_panel_count > 0
            else ForecastIncrementStatus.AWAITING_SETTLEMENT
            if forecast_panels > 0
            else ForecastIncrementStatus.AWAITING_FORECAST
        )
        return ForecastIncrementEvidence(
            status=status,
            candidate_producer_id=self.candidate_producer_id,
            comparator_producer_id=self.comparator_producer_id,
            candidate_behavior_id=behavior_id,
            due_panel_count=len(panels),
            forecast_panel_count=forecast_panels,
            unavailable_panel_count=unavailable_panels,
            pending_panel_count=pending_panels,
            pair=pair,
        )

    def _empty(
        self,
        status: ForecastIncrementStatus,
        *,
        behavior_id: str | None,
    ) -> ForecastIncrementEvidence:
        return ForecastIncrementEvidence(
            status=status,
            candidate_producer_id=self.candidate_producer_id,
            comparator_producer_id=self.comparator_producer_id,
            candidate_behavior_id=behavior_id,
            due_panel_count=0,
            forecast_panel_count=0,
            unavailable_panel_count=0,
            pending_panel_count=0,
            pair=evaluate_forecast_pair_evidence(()),
        )

    def _latest_behavior_id(self) -> str | None:
        with self.engine.connect() as connection:
            return connection.execute(
                select(forecast_producer_bindings.c.producer_behavior_id)
                .where(forecast_producer_bindings.c.producer_id == self.candidate_producer_id)
                .order_by(
                    forecast_producer_bindings.c.activated_at.desc(),
                    forecast_producer_bindings.c.producer_behavior_id.desc(),
                )
                .limit(1)
            ).scalar_one_or_none()

    @staticmethod
    def _base_forecasts(
        connection: Connection,
        *,
        slot_ids: tuple[str, ...],
        producer_behavior_id: str,
    ) -> dict[str, BaseForecast]:
        values = (
            BaseForecast.model_validate(payload)
            for payload in connection.execute(
                select(forecasts.c.payload).where(
                    forecasts.c.kind == ForecastResultKind.BASE.value,
                    forecasts.c.decision_slot_id.in_(slot_ids),
                    forecasts.c.producer_behavior_id == producer_behavior_id,
                )
            ).scalars()
        )
        return {item.decision_slot_id: item for item in values}

    @staticmethod
    def _forecasts_by_id(
        connection: Connection,
        *,
        slot_ids: tuple[str, ...],
        producer_id: str,
    ) -> dict[str, BaseForecast]:
        values = (
            BaseForecast.model_validate(payload)
            for payload in connection.execute(
                select(forecasts.c.payload).where(
                    forecasts.c.kind == ForecastResultKind.BASE.value,
                    forecasts.c.decision_slot_id.in_(slot_ids),
                    forecasts.c.producer_id == producer_id,
                )
            ).scalars()
        )
        return {item.forecast_id: item for item in values}

    @staticmethod
    def _pair_case(
        *,
        behavior_id: str,
        cutoff: datetime,
        obligations: tuple[ForecastSlotObligation, ...],
        slots: dict[str, ForecastDecisionSlot],
        candidate: dict[str, BaseForecast],
        comparator: dict[str, BaseForecast],
        outcomes: dict[str, ForecastOutcome],
    ) -> ForecastPairPanelCase | None:
        rows: list[tuple[Decimal, Decimal, Decimal, Decimal, Decimal, Decimal]] = []
        for obligation in obligations:
            slot_id = obligation.slot_id
            candidate_forecast = candidate.get(slot_id)
            outcome = outcomes.get(slot_id)
            if (
                candidate_forecast is None
                or outcome is None
                or outcome.status != ForecastOutcomeStatus.SETTLED
                or outcome.realized_bucket_id is None
            ):
                return None
            comparator_refs = set(candidate_forecast.input_refs) & comparator.keys()
            if len(comparator_refs) != 1:
                raise ValueError("Forecast 增量评价的候选未唯一引用冻结先验")
            comparator_forecast = comparator[next(iter(comparator_refs))]
            if candidate_forecast.contract_id != comparator_forecast.contract_id:
                raise ValueError("Forecast 增量评价的候选与先验 Contract 不一致")
            if candidate_forecast.decision_slot_id != comparator_forecast.decision_slot_id:
                raise ValueError("Forecast 增量评价的候选与先验 DecisionSlot 不一致")
            candidate_probabilities = tuple(
                (item.bucket_id, item.probability)
                for item in candidate_forecast.outcome_probabilities
            )
            comparator_probabilities = tuple(
                (item.bucket_id, item.probability)
                for item in comparator_forecast.outcome_probabilities
            )
            if tuple(item[0] for item in candidate_probabilities) != tuple(
                item[0] for item in comparator_probabilities
            ):
                raise ValueError("Forecast 增量评价的候选与先验 buckets 不一致")
            realized = outcome.realized_bucket_id
            rows.append(
                (
                    ordinal_ranked_probability_score(candidate_probabilities, realized),
                    ordinal_ranked_probability_score(comparator_probabilities, realized),
                    multiclass_brier_score(candidate_probabilities, realized),
                    multiclass_brier_score(comparator_probabilities, realized),
                    max(
                        abs(candidate_item[1] - comparator_item[1])
                        for candidate_item, comparator_item in zip(
                            candidate_probabilities,
                            comparator_probabilities,
                            strict=True,
                        )
                    ),
                    candidate_forecast.expected_gross_bps - comparator_forecast.expected_gross_bps,
                )
            )
        if not rows:
            return None
        count = Decimal(len(rows))
        panel_slots = tuple(slots[item.slot_id] for item in obligations)
        evaluation_times = {item.evaluation_at for item in panel_slots}
        strata = {item.stratum for item in panel_slots}
        if len(evaluation_times) != 1 or len(strata) != 1:
            raise ValueError("Forecast 增量评价联合 panel 的时间或来源不一致")

        def mean(index: int) -> Decimal:
            return sum((item[index] for item in rows), Decimal("0")) / count

        return ForecastPairPanelCase(
            panel_id=stable_id("forecast_increment_panel", behavior_id, cutoff.isoformat()),
            information_cutoff_at=cutoff,
            evaluation_at=next(iter(evaluation_times)),
            source_stratum=next(iter(strata)),
            paired_target_count=len(rows),
            candidate_ranked_probability_score=mean(0),
            comparator_ranked_probability_score=mean(1),
            candidate_brier_score=mean(2),
            comparator_brier_score=mean(3),
            mean_max_bucket_probability_delta=mean(4),
            mean_expected_gross_bps_delta=mean(5),
        )
