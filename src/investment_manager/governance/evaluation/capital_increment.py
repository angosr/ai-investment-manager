"""Fee-inclusive capital increment of a WorldModel posterior over its frozen prior."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from threading import Lock

from sqlalchemy import select
from sqlalchemy.engine import Engine

from investment_manager.forecast.contract_repository import SqlForecastContractStore
from investment_manager.forecast.contracts import ForecastSlotStratum
from investment_manager.forecast.product.projector import PointInTimeProductPayoffProjector
from investment_manager.forecast.tables import (
    forecast_producer_bindings,
    forecasts,
)
from investment_manager.governance.evaluation.logical_account import (
    LogicalAccountPath,
    ProducerDecisionPanel,
    ProducerPanelLedger,
    SqlProducerPanelReader,
)
from investment_manager.governance.evaluation.producer_capital import (
    ProducerCapitalReplay,
    evaluate_producer_capital_path,
)
from investment_manager.kernel.errors import PointInTimeInputUnavailable
from investment_manager.kernel.identity import content_hash
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.portfolio.policy import CapitalPolicy

WORLD_MODEL_CAPITAL_INCREMENT_VERSION = "world-model-capital-increment-v2"
EVENT_RESPONSE_CAPITAL_VERSION = "event-response-capital-v1"


class CapitalIncrementStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    AWAITING_FORECAST = "AWAITING_FORECAST"
    AWAITING_SETTLEMENT = "AWAITING_SETTLEMENT"
    INPUT_UNAVAILABLE = "INPUT_UNAVAILABLE"
    EVIDENCE_AVAILABLE = "EVIDENCE_AVAILABLE"


class CapitalPathSummary(FrozenModel):
    producer_behavior_id: str
    panel_count: int
    as_of: datetime
    equity: Decimal
    net_pnl: Decimal
    fee_cost: Decimal
    drawdown_fraction: Decimal
    gross_turnover: Decimal


class WorldModelCapitalIncrementEvidence(FrozenModel):
    """Same-policy capital difference attributable to the complete producer behavior."""

    evaluation_version: str = WORLD_MODEL_CAPITAL_INCREMENT_VERSION
    status: CapitalIncrementStatus
    candidate_behavior_id: str | None = None
    comparator_behavior_id: str | None = None
    settled_panel_count: int = 0
    candidate: CapitalPathSummary | None = None
    comparator: CapitalPathSummary | None = None
    net_equity_increment: Decimal | None = None
    fee_cost_increment: Decimal | None = None
    gross_turnover_increment: Decimal | None = None
    drawdown_improvement_fraction: Decimal | None = None
    reason_code: str | None = None


class EventResponseCapitalEvidence(FrozenModel):
    """Capital value of consuming material slots in addition to fixed cadence slots."""

    evaluation_version: str = EVENT_RESPONSE_CAPITAL_VERSION
    status: CapitalIncrementStatus
    candidate_behavior_id: str | None = None
    settled_material_panel_count: int = 0
    cadence_only_panel_count: int = 0
    cadence_plus_material_panel_count: int = 0
    cadence_only: CapitalPathSummary | None = None
    cadence_plus_material: CapitalPathSummary | None = None
    net_equity_increment: Decimal | None = None
    fee_cost_increment: Decimal | None = None
    gross_turnover_increment: Decimal | None = None
    drawdown_improvement_fraction: Decimal | None = None
    reason_code: str | None = None


@dataclass(slots=True)
class SqlWorldModelCapitalIncrementReader:
    """Rebuild two read-only logical accounts from shared point-in-time facts."""

    engine: Engine
    capital_policy: CapitalPolicy
    initial_cash: Decimal
    funding_lookback_hours: int
    candidate_producer_id: str
    comparator_producer_id: str
    _cache_key: tuple[str, ...] | None = field(default=None, init=False)
    _cache: WorldModelCapitalIncrementEvidence | None = field(default=None, init=False)
    _cache_lock: Lock = field(default_factory=Lock, init=False)
    _event_cache_key: tuple[str, ...] | None = field(default=None, init=False)
    _event_cache: EventResponseCapitalEvidence | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if (
            self.initial_cash <= 0
            or not self.capital_policy.decision.enabled
            or not 8 <= self.funding_lookback_hours <= 720
        ):
            raise ValueError("WorldModel 资本评价配置非法")

    def read(self, *, as_of: datetime | None = None) -> WorldModelCapitalIncrementEvidence:
        now = require_utc(as_of or datetime.now(UTC))
        candidate_behavior = self._latest_behavior_id(self.candidate_producer_id)
        if candidate_behavior is None:
            return self._empty(CapitalIncrementStatus.NOT_STARTED)
        panels = SqlProducerPanelReader(self.engine)
        candidate_ledger = panels.read(
            producer_behavior_id=candidate_behavior,
            as_of=now,
        )
        cadence_panels = self._cadence_panels(candidate_ledger.complete_panels)
        if not cadence_panels:
            return self._empty(
                CapitalIncrementStatus.AWAITING_FORECAST,
                candidate_behavior=candidate_behavior,
            )
        settled_candidate = tuple(
            panel
            for panel in cadence_panels
            if all(slot.evaluation_at <= now for slot in panel.slots)
        )
        if not settled_candidate:
            return self._empty(
                CapitalIncrementStatus.AWAITING_SETTLEMENT,
                candidate_behavior=candidate_behavior,
            )
        comparator_behavior = self._comparator_behavior(settled_candidate)
        if comparator_behavior is None:
            return self._empty(
                CapitalIncrementStatus.AWAITING_FORECAST,
                candidate_behavior=candidate_behavior,
            )
        comparator_ledger = panels.read(
            producer_behavior_id=comparator_behavior,
            as_of=now,
        )
        expected_settled_count = len(settled_candidate)
        settled_candidate, settled_comparator = self._common_panels(
            settled_candidate,
            self._cadence_panels(comparator_ledger.complete_panels),
        )
        if len(settled_candidate) != expected_settled_count:
            return self._empty(
                CapitalIncrementStatus.AWAITING_FORECAST,
                candidate_behavior=candidate_behavior,
                comparator_behavior=comparator_behavior,
            )
        settlement_at = max(
            slot.evaluation_at for panel in settled_candidate for slot in panel.slots
        )
        contract_ids = tuple(
            sorted(
                {
                    obligation.contract_id
                    for panel in settled_candidate
                    for obligation in panel.obligations
                }
            )
        )
        cache_key = (
            content_hash(self.capital_policy),
            str(self.initial_cash),
            candidate_behavior,
            comparator_behavior,
            *(panel.panel_id for panel in settled_candidate),
            *(panel.panel_id for panel in settled_comparator),
        )
        with self._cache_lock:
            if self._cache_key == cache_key and self._cache is not None:
                return self._cache
        try:
            projectors = self._projectors(contract_ids)
            candidate_path = self._path(
                behavior_id=candidate_behavior,
                panels=settled_candidate,
                projectors=projectors,
                mark_at=settlement_at,
            )
            comparator_path = self._path(
                behavior_id=comparator_behavior,
                panels=settled_comparator,
                projectors=projectors,
                mark_at=settlement_at,
            )
        except PointInTimeInputUnavailable:
            return self._empty(
                CapitalIncrementStatus.INPUT_UNAVAILABLE,
                candidate_behavior=candidate_behavior,
                comparator_behavior=comparator_behavior,
                settled_panel_count=len(settled_candidate),
                reason_code="POINT_IN_TIME_PRODUCT_INPUT_UNAVAILABLE",
            )
        if candidate_path is None or comparator_path is None:
            return self._empty(
                CapitalIncrementStatus.INPUT_UNAVAILABLE,
                candidate_behavior=candidate_behavior,
                comparator_behavior=comparator_behavior,
                settled_panel_count=len(settled_candidate),
                reason_code="COMPLETE_CAPITAL_PATH_UNAVAILABLE",
            )
        candidate = self._summary(candidate_path)
        comparator = self._summary(comparator_path)
        evidence = WorldModelCapitalIncrementEvidence(
            status=CapitalIncrementStatus.EVIDENCE_AVAILABLE,
            candidate_behavior_id=candidate_behavior,
            comparator_behavior_id=comparator_behavior,
            settled_panel_count=len(settled_candidate),
            candidate=candidate,
            comparator=comparator,
            net_equity_increment=candidate.equity - comparator.equity,
            fee_cost_increment=candidate.fee_cost - comparator.fee_cost,
            gross_turnover_increment=candidate.gross_turnover - comparator.gross_turnover,
            drawdown_improvement_fraction=(
                comparator.drawdown_fraction - candidate.drawdown_fraction
            ),
        )
        with self._cache_lock:
            self._cache_key = cache_key
            self._cache = evidence
        return evidence

    def read_event_response(
        self,
        *,
        as_of: datetime | None = None,
    ) -> EventResponseCapitalEvidence:
        """Compare the same behavior with and without its material-event panels."""

        now = require_utc(as_of or datetime.now(UTC))
        behavior = self._latest_behavior_id(self.candidate_producer_id)
        if behavior is None:
            return self._empty_event(CapitalIncrementStatus.NOT_STARTED)
        ledger = SqlProducerPanelReader(self.engine).read(
            producer_behavior_id=behavior,
            as_of=now,
        )
        material_panels = self._panels_for_stratum(
            ledger.complete_panels,
            ForecastSlotStratum.MATERIAL_STATE_ONLY,
        )
        if not material_panels:
            return self._empty_event(
                CapitalIncrementStatus.NOT_STARTED,
                candidate_behavior=behavior,
            )
        settled_material = tuple(
            panel
            for panel in material_panels
            if all(slot.evaluation_at <= now for slot in panel.slots)
        )
        if not settled_material:
            return self._empty_event(
                CapitalIncrementStatus.AWAITING_SETTLEMENT,
                candidate_behavior=behavior,
            )
        evaluation_at = max(
            slot.evaluation_at for panel in settled_material for slot in panel.slots
        )
        all_panels = tuple(
            panel
            for panel in ledger.complete_panels
            if panel.available_at <= evaluation_at
        )
        cadence_panels = self._panels_for_stratum(
            all_panels,
            ForecastSlotStratum.CADENCE_ONLY,
        )
        contract_ids = tuple(
            sorted(
                {
                    obligation.contract_id
                    for panel in all_panels
                    for obligation in panel.obligations
                }
            )
        )
        cache_key = (
            content_hash(self.capital_policy),
            str(self.initial_cash),
            behavior,
            evaluation_at.isoformat(),
            *(panel.panel_id for panel in all_panels),
        )
        with self._cache_lock:
            if self._event_cache_key == cache_key and self._event_cache is not None:
                return self._event_cache
        try:
            projectors = self._projectors(contract_ids)
            complete_path = self._path(
                behavior_id=behavior,
                panels=all_panels,
                projectors=projectors,
                mark_at=evaluation_at,
                allowed_strata=tuple(ForecastSlotStratum),
            )
            cadence_path = (
                None
                if not cadence_panels
                else self._path(
                    behavior_id=behavior,
                    panels=cadence_panels,
                    projectors=projectors,
                    mark_at=evaluation_at,
                    allowed_strata=(ForecastSlotStratum.CADENCE_ONLY,),
                )
            )
        except PointInTimeInputUnavailable:
            return self._empty_event(
                CapitalIncrementStatus.INPUT_UNAVAILABLE,
                candidate_behavior=behavior,
                settled_material_panel_count=len(settled_material),
                reason_code="POINT_IN_TIME_PRODUCT_INPUT_UNAVAILABLE",
            )
        if complete_path is None or (cadence_panels and cadence_path is None):
            return self._empty_event(
                CapitalIncrementStatus.INPUT_UNAVAILABLE,
                candidate_behavior=behavior,
                settled_material_panel_count=len(settled_material),
                reason_code="COMPLETE_CAPITAL_PATH_UNAVAILABLE",
            )
        complete = self._summary(complete_path)
        cadence = (
            self._cash_summary(behavior_id=behavior, as_of=evaluation_at)
            if cadence_path is None
            else self._summary(cadence_path)
        )
        evidence = EventResponseCapitalEvidence(
            status=CapitalIncrementStatus.EVIDENCE_AVAILABLE,
            candidate_behavior_id=behavior,
            settled_material_panel_count=len(settled_material),
            cadence_only_panel_count=len(cadence_panels),
            cadence_plus_material_panel_count=len(all_panels),
            cadence_only=cadence,
            cadence_plus_material=complete,
            net_equity_increment=complete.equity - cadence.equity,
            fee_cost_increment=complete.fee_cost - cadence.fee_cost,
            gross_turnover_increment=complete.gross_turnover - cadence.gross_turnover,
            drawdown_improvement_fraction=(
                cadence.drawdown_fraction - complete.drawdown_fraction
            ),
        )
        with self._cache_lock:
            self._event_cache_key = cache_key
            self._event_cache = evidence
        return evidence

    def _latest_behavior_id(self, producer_id: str) -> str | None:
        with self.engine.connect() as connection:
            return connection.execute(
                select(forecast_producer_bindings.c.producer_behavior_id)
                .where(forecast_producer_bindings.c.producer_id == producer_id)
                .order_by(
                    forecast_producer_bindings.c.activated_at.desc(),
                    forecast_producer_bindings.c.producer_behavior_id.desc(),
                )
                .limit(1)
            ).scalar_one_or_none()

    def _comparator_behavior(
        self,
        candidate_panels: tuple[ProducerDecisionPanel, ...],
    ) -> str | None:
        slot_ids = tuple(
            sorted({slot.slot_id for panel in candidate_panels for slot in panel.slots})
        )
        input_refs = tuple(
            sorted(
                {
                    ref
                    for panel in candidate_panels
                    for terminal in (*panel.forecasts, *panel.no_estimates)
                    for ref in terminal.input_refs
                }
            )
        )
        with self.engine.connect() as connection:
            rows = tuple(
                connection.execute(
                    select(
                        forecasts.c.decision_slot_id,
                        forecasts.c.producer_behavior_id,
                    ).where(
                        forecasts.c.producer_id == self.comparator_producer_id,
                        forecasts.c.forecast_id.in_(input_refs),
                    )
                ).all()
            )
        if {row[0] for row in rows} != set(slot_ids):
            return None
        behavior_ids = {row[1] for row in rows}
        if len(behavior_ids) != 1:
            raise ValueError("同一 WorldModel 资本 cohort 引用了多个先验行为")
        return next(iter(behavior_ids))

    @staticmethod
    def _cadence_panels(
        panels: tuple[ProducerDecisionPanel, ...],
    ) -> tuple[ProducerDecisionPanel, ...]:
        return SqlWorldModelCapitalIncrementReader._panels_for_stratum(
            panels,
            ForecastSlotStratum.CADENCE_ONLY,
        )

    @staticmethod
    def _panels_for_stratum(
        panels: tuple[ProducerDecisionPanel, ...],
        stratum: ForecastSlotStratum,
    ) -> tuple[ProducerDecisionPanel, ...]:
        selected = []
        for panel in panels:
            strata = {slot.stratum for slot in panel.slots}
            if len(strata) != 1:
                raise ValueError("WorldModel 资本 panel 混合了不同样本分层")
            if strata == {stratum}:
                selected.append(panel)
        return tuple(selected)

    @staticmethod
    def _common_panels(
        candidate: tuple[ProducerDecisionPanel, ...],
        comparator: tuple[ProducerDecisionPanel, ...],
    ) -> tuple[tuple[ProducerDecisionPanel, ...], tuple[ProducerDecisionPanel, ...]]:
        comparator_by_slots = {
            tuple(sorted(slot.slot_id for slot in panel.slots)): panel for panel in comparator
        }
        candidate_result = []
        comparator_result = []
        for panel in candidate:
            key = tuple(sorted(slot.slot_id for slot in panel.slots))
            matched = comparator_by_slots.get(key)
            if matched is not None:
                candidate_result.append(panel)
                comparator_result.append(matched)
        return tuple(candidate_result), tuple(comparator_result)

    def _projectors(
        self,
        contract_ids: tuple[str, ...],
    ) -> dict[str, PointInTimeProductPayoffProjector]:
        contracts = SqlForecastContractStore(self.engine)
        market = SqlMarketDataStore(self.engine)
        specs_by_key = {item.instrument.key: item for item in self.capital_policy.execution_specs}
        result: dict[str, PointInTimeProductPayoffProjector] = {}
        for contract_id in contract_ids:
            contract = contracts.contract(contract_id)
            if contract is None or len(contract.target.legs) != 1:
                raise PointInTimeInputUnavailable("资本重放缺少有效 ForecastContract")
            reference = contract.target.legs[0].instrument
            policies = tuple(
                policy
                for policy in self.capital_policy.product_payoff_policies
                if all(
                    key in specs_by_key
                    and specs_by_key[key].instrument.base_asset == reference.base_asset
                    and specs_by_key[key].instrument.quote_asset == reference.quote_asset
                    and specs_by_key[key].instrument.settlement_asset == reference.settlement_asset
                    for key in policy.instrument_keys
                )
            )
            if len(policies) != 1:
                raise PointInTimeInputUnavailable("资本重放缺少唯一产品映射政策")
            policy = policies[0]
            specs = tuple(specs_by_key[key] for key in policy.instrument_keys)
            projector = PointInTimeProductPayoffProjector(
                policy=policy,
                contract=contract,
                market=market,
                instruments=tuple(item.instrument for item in specs),
                execution_specs=specs,
                risk=self.capital_policy.sleeve_risk,
                maximum_quote_age_seconds=(self.capital_policy.risk.maximum_quote_age_seconds),
                funding_lookback_hours=self.funding_lookback_hours,
            )
            if contract.outcome_family_id in result:
                raise ValueError("资本重放出现重复 Forecast outcome family")
            result[contract.outcome_family_id] = projector
        return result

    def _path(
        self,
        *,
        behavior_id: str,
        panels: tuple[ProducerDecisionPanel, ...],
        projectors: dict[str, PointInTimeProductPayoffProjector],
        mark_at: datetime,
        allowed_strata: tuple[ForecastSlotStratum, ...] = (
            ForecastSlotStratum.CADENCE_ONLY,
        ),
    ) -> LogicalAccountPath | None:
        ledger = ProducerPanelLedger(
            producer_behavior_id=behavior_id,
            as_of=mark_at,
            obligated_panel_count=len(panels),
            complete_panels=panels,
            pending_panel_count=0,
        )
        replay = ProducerCapitalReplay(
            producer_behavior_id=behavior_id,
            # Deployment enablement controls the real shadow account, not this
            # read-only counterfactual. All economic policy fields stay unchanged.
            capital_policy=self.capital_policy.model_copy(update={"enabled": True}),
            initial_cash=self.initial_cash,
            market=SqlMarketDataStore(self.engine),
            product_payoffs_by_family=projectors,
            sleeve_risk=self.capital_policy.sleeve_risk,
        )
        evidence = evaluate_producer_capital_path(
            initial_cash=self.initial_cash,
            ledger=ledger,
            replay=replay,
            allowed_strata=allowed_strata,
            mark_at=mark_at,
        )
        return None if evidence is None else evidence.path

    @staticmethod
    def _summary(path: LogicalAccountPath) -> CapitalPathSummary:
        accounting = path.account.accounting
        if accounting is None:
            raise ValueError("逻辑账户缺少费用后损益归因")
        return CapitalPathSummary(
            producer_behavior_id=path.producer_behavior_id,
            panel_count=len(path.step_ids),
            as_of=path.account.as_of,
            equity=path.account.equity,
            net_pnl=accounting.net_pnl,
            fee_cost=accounting.fee_cost,
            drawdown_fraction=path.account.drawdown_fraction,
            gross_turnover=path.gross_turnover,
        )

    def _cash_summary(self, *, behavior_id: str, as_of: datetime) -> CapitalPathSummary:
        return CapitalPathSummary(
            producer_behavior_id=behavior_id,
            panel_count=0,
            as_of=as_of,
            equity=self.initial_cash,
            net_pnl=Decimal("0"),
            fee_cost=Decimal("0"),
            drawdown_fraction=Decimal("0"),
            gross_turnover=Decimal("0"),
        )

    @staticmethod
    def _empty(
        status: CapitalIncrementStatus,
        *,
        candidate_behavior: str | None = None,
        comparator_behavior: str | None = None,
        settled_panel_count: int = 0,
        reason_code: str | None = None,
    ) -> WorldModelCapitalIncrementEvidence:
        return WorldModelCapitalIncrementEvidence(
            status=status,
            candidate_behavior_id=candidate_behavior,
            comparator_behavior_id=comparator_behavior,
            settled_panel_count=settled_panel_count,
            reason_code=reason_code,
        )

    @staticmethod
    def _empty_event(
        status: CapitalIncrementStatus,
        *,
        candidate_behavior: str | None = None,
        settled_material_panel_count: int = 0,
        reason_code: str | None = None,
    ) -> EventResponseCapitalEvidence:
        return EventResponseCapitalEvidence(
            status=status,
            candidate_behavior_id=candidate_behavior,
            settled_material_panel_count=settled_material_panel_count,
            reason_code=reason_code,
        )


__all__ = [
    "CapitalIncrementStatus",
    "CapitalPathSummary",
    "EventResponseCapitalEvidence",
    "SqlWorldModelCapitalIncrementReader",
    "WorldModelCapitalIncrementEvidence",
]
