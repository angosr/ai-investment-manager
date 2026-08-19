from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import Field, field_validator, model_validator
from sqlalchemy import func, insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from quant_core.candidate_evaluation import trade_at_or_before
from quant_core.domain import (
    AnalysisForecastOutcome,
    AnalysisProposal,
    DirectionalForecast,
    DirectionalView,
    ForecastOutcomeStatus,
    FrozenModel,
    _require_utc,
)
from quant_core.ids import content_hash, stable_id
from quant_core.persistence import (
    analysis_cycles,
    analysis_forecast_outcomes,
    analysis_proposals,
    codex_runs,
    market_snapshots,
)


@dataclass(frozen=True, slots=True)
class PendingAnalysisForecast:
    proposal: AnalysisProposal
    forecast: DirectionalForecast
    cycle_id: str
    pipeline_version: str
    analysis_as_of: datetime
    available_at: datetime | None
    source_run_id: str | None
    frozen_reference_price: Decimal


@dataclass(frozen=True, slots=True)
class ForecastSettlementResult:
    settled: int = 0
    abstained: int = 0
    unscorable: int = 0
    pending: int = 0


class ForecastScopeMetrics(FrozenModel):
    symbol: str
    view_horizon_minutes: int = Field(gt=0)
    outcome_count: int = Field(ge=0)
    scored_count: int = Field(ge=0)
    abstained_count: int = Field(ge=0)
    unscorable_count: int = Field(ge=0)
    non_overlapping_scored_count: int = Field(ge=0)
    correct_count: int = Field(ge=0)
    directional_accuracy: Decimal | None = Field(default=None, ge=0, le=1)
    average_directional_return_bps: Decimal | None = None
    average_directional_return_bps_lower_bound: Decimal | None = None
    sample_sufficient: bool


class ForecastEvaluationReport(FrozenModel):
    report_id: str
    report_version: str
    outcome_evaluation_version: str
    pipeline_version: str
    window_start: datetime
    window_end: datetime
    published_at: datetime
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    scopes: tuple[ForecastScopeMetrics, ...] = Field(min_length=1)
    statistically_conclusive: bool
    limitations: tuple[str, ...]
    outcome_ids: tuple[str, ...]

    _utc_window_start = field_validator("window_start")(_require_utc)
    _utc_window_end = field_validator("window_end")(_require_utc)
    _utc_published_at = field_validator("published_at")(_require_utc)

    @model_validator(mode="after")
    def identity_and_window_match(self):
        if not self.window_start < self.window_end <= self.published_at:
            raise ValueError("预测评价窗口和发布时间顺序非法")
        expected_id = stable_id(
            "forecast_evaluation_report",
            self.report_version,
            self.outcome_evaluation_version,
            self.pipeline_version,
            self.window_start,
            self.window_end,
            self.published_at,
            self.source_hash,
        )
        if self.report_id != expected_id:
            raise ValueError("预测评价 report_id 不一致")
        return self


class SqlAnalysisForecastOutcomeStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def pending(
        self,
        *,
        limit: int,
    ) -> tuple[PendingAnalysisForecast, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("方向预测结算批次必须在 1..1000")
        with self._engine.connect() as connection:
            outcome_counts = (
                select(
                    analysis_forecast_outcomes.c.proposal_id,
                    func.count(analysis_forecast_outcomes.c.outcome_id).label(
                        "outcome_count"
                    ),
                )
                .group_by(analysis_forecast_outcomes.c.proposal_id)
                .subquery()
            )
            rows = connection.execute(
                select(
                    analysis_proposals.c.payload.label("proposal"),
                    analysis_proposals.c.forecast_count,
                    analysis_cycles.c.cycle_id,
                    analysis_cycles.c.pipeline_version,
                    analysis_cycles.c.as_of,
                    market_snapshots.c.symbol,
                    market_snapshots.c.payload.label("market"),
                )
                .join(
                    analysis_cycles,
                    analysis_cycles.c.cycle_id == analysis_proposals.c.cycle_id,
                )
                .join(
                    market_snapshots,
                    market_snapshots.c.cycle_id == analysis_proposals.c.cycle_id,
                )
                .outerjoin(
                    outcome_counts,
                    outcome_counts.c.proposal_id == analysis_proposals.c.proposal_id,
                )
                .where(
                    func.coalesce(outcome_counts.c.outcome_count, 0)
                    < analysis_proposals.c.forecast_count,
                )
                .order_by(analysis_cycles.c.as_of, analysis_proposals.c.proposal_id)
                .limit(limit)
            ).mappings()

            proposal_rows = tuple(rows)
            proposal_ids = tuple(
                AnalysisProposal.model_validate(row["proposal"]).proposal_id
                for row in proposal_rows
            )
            existing_keys = set(
                connection.execute(
                    select(
                        analysis_forecast_outcomes.c.proposal_id,
                        analysis_forecast_outcomes.c.view_horizon_minutes,
                    ).where(
                        analysis_forecast_outcomes.c.proposal_id.in_(proposal_ids)
                    )
                ).tuples()
            ) if proposal_ids else set()
            cycle_ids = tuple(str(row["cycle_id"]) for row in proposal_rows)
            run_rows = (
                tuple(
                    connection.execute(
                        select(
                            codex_runs.c.run_id,
                            codex_runs.c.cycle_id,
                            codex_runs.c.status,
                            codex_runs.c.payload,
                        ).where(codex_runs.c.cycle_id.in_(cycle_ids))
                    ).mappings()
                )
                if cycle_ids
                else ()
            )
            runs_by_cycle: dict[str, list[dict]] = {}
            for run in run_rows:
                runs_by_cycle.setdefault(str(run["cycle_id"]), []).append(dict(run))

            pending: list[PendingAnalysisForecast] = []
            for row in proposal_rows:
                proposal = AnalysisProposal.model_validate(row["proposal"])
                if int(row["forecast_count"]) != len(proposal.forecasts):
                    raise ValueError("Proposal 预测数量投影与冻结 Payload 不一致")
                if proposal.symbol != row["symbol"]:
                    raise ValueError("方向预测 Proposal 与 MarketSnapshot 品种不一致")
                market_payload = row["market"]
                if not isinstance(market_payload, dict) or "last" not in market_payload:
                    raise ValueError("方向预测缺少冻结参考价格")
                reference_price = Decimal(str(market_payload["last"]))
                if reference_price <= 0:
                    raise ValueError("方向预测冻结参考价格必须为正")
                cycle_id = str(row["cycle_id"])
                analysis_as_of = _database_utc(row["as_of"])
                available_at, source_run_id = unique_successful_codex_completion(
                    runs_by_cycle.get(cycle_id, []),
                    analysis_as_of=analysis_as_of,
                )
                for forecast in proposal.forecasts:
                    if (proposal.proposal_id, forecast.horizon_minutes) in existing_keys:
                        continue
                    pending.append(
                        PendingAnalysisForecast(
                            proposal=proposal,
                            forecast=forecast,
                            cycle_id=cycle_id,
                            pipeline_version=str(row["pipeline_version"]),
                            analysis_as_of=analysis_as_of,
                            available_at=available_at,
                            source_run_id=source_run_id,
                            frozen_reference_price=reference_price,
                        )
                    )
                    if len(pending) >= limit:
                        return tuple(pending)
        return tuple(pending)

    def record(self, outcome: AnalysisForecastOutcome) -> bool:
        payload = outcome.model_dump(mode="json")
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(analysis_forecast_outcomes).values(
                        outcome_id=outcome.outcome_id,
                        proposal_id=outcome.proposal_id,
                        cycle_id=outcome.cycle_id,
                        pipeline_version=outcome.pipeline_version,
                        view_horizon_minutes=outcome.view_horizon_minutes,
                        status=outcome.status.value,
                        evaluation_at=outcome.evaluation_at,
                        settled_at=outcome.settled_at,
                        directional_return_bps=outcome.directional_return_bps,
                        payload=payload,
                    )
                )
            return True
        except IntegrityError:
            with self._engine.connect() as connection:
                existing = connection.execute(
                    select(analysis_forecast_outcomes.c.payload).where(
                        analysis_forecast_outcomes.c.proposal_id == outcome.proposal_id,
                        analysis_forecast_outcomes.c.view_horizon_minutes
                        == outcome.view_horizon_minutes,
                    )
                ).scalar_one_or_none()
            if existing is None or AnalysisForecastOutcome.model_validate(existing) != outcome:
                raise ValueError("AnalysisForecastOutcome 已存在且内容不同") from None
            return False

    def visible_outcomes(
        self,
        *,
        pipeline_version: str,
        window_start: datetime,
        window_end: datetime,
        published_at: datetime,
    ) -> tuple[AnalysisForecastOutcome, ...]:
        start = _require_utc(window_start)
        end = _require_utc(window_end)
        published = _require_utc(published_at)
        if not start < end <= published:
            raise ValueError("预测评价窗口和发布时间顺序非法")
        with self._engine.connect() as connection:
            payloads = connection.execute(
                select(analysis_forecast_outcomes.c.payload)
                .where(
                    analysis_forecast_outcomes.c.pipeline_version
                    == pipeline_version,
                    analysis_forecast_outcomes.c.evaluation_at >= start,
                    analysis_forecast_outcomes.c.evaluation_at < end,
                    analysis_forecast_outcomes.c.settled_at <= published,
                )
                .order_by(
                    analysis_forecast_outcomes.c.evaluation_at,
                    analysis_forecast_outcomes.c.outcome_id,
                )
            ).scalars()
            return tuple(
                AnalysisForecastOutcome.model_validate(payload) for payload in payloads
            )


@dataclass(frozen=True, slots=True)
class AnalysisForecastEvaluator:
    report_version: str = "analysis-forecast-report-v1"
    minimum_non_overlapping_samples: int = 30
    lower_confidence_z: Decimal = Decimal("1.96")

    def evaluate(
        self,
        *,
        outcomes: tuple[AnalysisForecastOutcome, ...],
        outcome_evaluation_version: str,
        pipeline_version: str,
        window_start: datetime,
        window_end: datetime,
        published_at: datetime,
    ) -> ForecastEvaluationReport:
        start = _require_utc(window_start)
        end = _require_utc(window_end)
        published = _require_utc(published_at)
        if not start < end <= published:
            raise ValueError("预测评价窗口和发布时间顺序非法")
        if not outcomes:
            raise ValueError("预测评价至少需要一个到期结果")
        if self.minimum_non_overlapping_samples < 2:
            raise ValueError("预测评价最小非重叠样本数至少为 2")
        if self.lower_confidence_z <= 0:
            raise ValueError("预测评价置信下界 z 必须为正")
        ids = [item.outcome_id for item in outcomes]
        proposal_horizons = [
            (item.proposal_id, item.view_horizon_minutes) for item in outcomes
        ]
        if len(ids) != len(set(ids)) or len(proposal_horizons) != len(
            set(proposal_horizons)
        ):
            raise ValueError("预测评价结果或 Proposal 周期不得重复")
        if any(
            item.pipeline_version != pipeline_version
            or item.evaluation_version != outcome_evaluation_version
            or not start <= item.evaluation_at < end
            or item.settled_at > published
            for item in outcomes
        ):
            raise ValueError("预测评价包含作用域外或发布时间后才可见的结果")

        ordered = tuple(
            sorted(outcomes, key=lambda item: (item.evaluation_at, item.outcome_id))
        )
        scopes: list[ForecastScopeMetrics] = []
        scope_keys = sorted(
            {(item.symbol, item.view_horizon_minutes) for item in ordered}
        )
        for symbol, horizon in scope_keys:
            scoped = tuple(
                item
                for item in ordered
                if item.symbol == symbol and item.view_horizon_minutes == horizon
            )
            scored = tuple(
                item for item in scoped if item.status == ForecastOutcomeStatus.SETTLED
            )
            independent = _non_overlapping(scored)
            returns = tuple(
                item.directional_return_bps
                for item in independent
                if item.directional_return_bps is not None
            )
            correct = sum(item.direction_correct is True for item in independent)
            sample_sufficient = (
                len(independent) >= self.minimum_non_overlapping_samples
            )
            scopes.append(
                ForecastScopeMetrics(
                    symbol=symbol,
                    view_horizon_minutes=horizon,
                    outcome_count=len(scoped),
                    scored_count=len(scored),
                    abstained_count=sum(
                        item.status == ForecastOutcomeStatus.ABSTAINED
                        for item in scoped
                    ),
                    unscorable_count=sum(
                        item.status == ForecastOutcomeStatus.UNSCORABLE
                        for item in scoped
                    ),
                    non_overlapping_scored_count=len(independent),
                    correct_count=correct,
                    directional_accuracy=(
                        Decimal(correct) / Decimal(len(independent))
                        if independent
                        else None
                    ),
                    average_directional_return_bps=(
                        sum(returns, Decimal("0")) / Decimal(len(returns))
                        if returns
                        else None
                    ),
                    average_directional_return_bps_lower_bound=_mean_lower_bound(
                        returns,
                        z=self.lower_confidence_z,
                    ),
                    sample_sufficient=sample_sufficient,
                )
            )
        conclusive = all(item.sample_sufficient for item in scopes)
        limitations = ["NON_TRADABLE_DIRECTIONAL_FORECAST_ONLY"]
        if not conclusive:
            limitations.append("NON_OVERLAPPING_SAMPLE_TOO_SMALL")
        source_hash = content_hash(
            [item.model_dump(mode="json") for item in ordered]
        )
        return ForecastEvaluationReport(
            report_id=stable_id(
                "forecast_evaluation_report",
                self.report_version,
                outcome_evaluation_version,
                pipeline_version,
                start,
                end,
                published,
                source_hash,
            ),
            report_version=self.report_version,
            outcome_evaluation_version=outcome_evaluation_version,
            pipeline_version=pipeline_version,
            window_start=start,
            window_end=end,
            published_at=published,
            source_hash=source_hash,
            scopes=tuple(scopes),
            statistically_conclusive=conclusive,
            limitations=tuple(limitations),
            outcome_ids=tuple(item.outcome_id for item in ordered),
        )


@dataclass(slots=True)
class AnalysisForecastOutcomeSettler:
    engine: Engine
    store: SqlAnalysisForecastOutcomeStore
    evaluation_version: str
    maximum_market_age_seconds: int
    settlement_grace_minutes: int
    batch_size: int = 100

    def settle(self, *, as_of: datetime) -> ForecastSettlementResult:
        now = _require_utc(as_of)
        settled = abstained = unscorable = pending = 0
        forecasts = self.store.pending(
            limit=self.batch_size,
        )
        for forecast in forecasts:
            proposal = forecast.proposal
            directional = forecast.forecast
            signal_at = forecast.available_at or forecast.analysis_as_of
            evaluation_at = signal_at + timedelta(
                minutes=directional.horizon_minutes
            )
            if evaluation_at > now:
                pending += 1
                continue
            common = {
                "outcome_id": stable_id(
                    "analysis_forecast_outcome",
                    proposal.proposal_id,
                    directional.horizon_minutes,
                    self.evaluation_version,
                ),
                "proposal_id": proposal.proposal_id,
                "cycle_id": forecast.cycle_id,
                "pipeline_version": forecast.pipeline_version,
                "evaluation_version": self.evaluation_version,
                "symbol": proposal.symbol,
                "directional_view": directional.directional_view,
                "confidence": directional.confidence,
                "view_horizon_minutes": directional.horizon_minutes,
                "signal_observed_at": signal_at,
                "evaluation_at": evaluation_at,
                "settled_at": now,
                "reference_price": forecast.frozen_reference_price,
            }
            if forecast.available_at is None or forecast.source_run_id is None:
                outcome = AnalysisForecastOutcome(
                    **common,
                    status=ForecastOutcomeStatus.UNSCORABLE,
                    reason_code="CODEX_COMPLETION_TIME_MISSING_OR_AMBIGUOUS",
                )
                unscorable += int(self.store.record(outcome))
                continue
            reference_trade = trade_at_or_before(
                self.engine,
                symbol=proposal.symbol,
                evaluation_at=forecast.available_at,
                visible_at=forecast.available_at,
            )
            fresh_reference = reference_trade is not None and timedelta(
                0
            ) <= forecast.available_at - reference_trade.event_time <= timedelta(
                seconds=self.maximum_market_age_seconds
            )
            if not fresh_reference:
                outcome = AnalysisForecastOutcome(
                    **common,
                    status=ForecastOutcomeStatus.UNSCORABLE,
                    reason_code="REFERENCE_MARKET_DATA_MISSING_AT_FORECAST_AVAILABILITY",
                )
                unscorable += int(self.store.record(outcome))
                continue
            assert reference_trade is not None
            common["reference_price"] = reference_trade.price
            trade = trade_at_or_before(
                self.engine,
                symbol=proposal.symbol,
                evaluation_at=evaluation_at,
                visible_at=now,
            )
            fresh_trade = trade is not None and timedelta(
                0
            ) <= evaluation_at - trade.event_time <= timedelta(
                seconds=self.maximum_market_age_seconds
            )
            if fresh_trade:
                assert trade is not None
                market_return = (
                    trade.price / reference_trade.price - Decimal("1")
                ) * Decimal("10000")
                if directional.directional_view == DirectionalView.UNCERTAIN:
                    outcome = AnalysisForecastOutcome(
                        **common,
                        status=ForecastOutcomeStatus.ABSTAINED,
                        exit_price=trade.price,
                        exit_event_time=trade.event_time,
                        market_return_bps=market_return,
                        reason_code="DIRECTIONAL_VIEW_ABSTAINED",
                    )
                    abstained += int(self.store.record(outcome))
                    continue
                directional_return = (
                    market_return
                    if directional.directional_view == DirectionalView.UP
                    else -market_return
                )
                outcome = AnalysisForecastOutcome(
                    **common,
                    status=ForecastOutcomeStatus.SETTLED,
                    exit_price=trade.price,
                    exit_event_time=trade.event_time,
                    market_return_bps=market_return,
                    directional_return_bps=directional_return,
                    direction_correct=directional_return > 0,
                    reason_code="DIRECTIONAL_RETURN_AVAILABLE",
                )
                settled += int(self.store.record(outcome))
                continue
            if now - evaluation_at < timedelta(minutes=self.settlement_grace_minutes):
                pending += 1
                continue
            outcome = AnalysisForecastOutcome(
                **common,
                status=ForecastOutcomeStatus.UNSCORABLE,
                reason_code="MARKET_DATA_MISSING_AT_FORECAST_HORIZON",
            )
            unscorable += int(self.store.record(outcome))
        return ForecastSettlementResult(
            settled=settled,
            abstained=abstained,
            unscorable=unscorable,
            pending=pending,
        )


def _database_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def unique_successful_codex_completion(
    rows: list[dict],
    *,
    analysis_as_of: datetime,
) -> tuple[datetime | None, str | None]:
    successful = [row for row in rows if row.get("status") == "SUCCEEDED"]
    if len(successful) != 1:
        return None, None
    row = successful[0]
    payload = row.get("payload")
    if not isinstance(payload, dict):
        return None, None
    raw_completed_at = payload.get("completed_at")
    if not isinstance(raw_completed_at, str):
        return None, None
    try:
        completed_at = _database_utc(datetime.fromisoformat(raw_completed_at))
    except ValueError:
        return None, None
    if completed_at < analysis_as_of:
        return None, None
    run_id = row.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return None, None
    return completed_at, run_id


def _non_overlapping(
    outcomes: tuple[AnalysisForecastOutcome, ...],
) -> tuple[AnalysisForecastOutcome, ...]:
    selected: list[AnalysisForecastOutcome] = []
    last_evaluation_at: datetime | None = None
    for outcome in sorted(
        outcomes,
        key=lambda item: (item.signal_observed_at, item.evaluation_at, item.outcome_id),
    ):
        if (
            last_evaluation_at is not None
            and outcome.signal_observed_at < last_evaluation_at
        ):
            continue
        selected.append(outcome)
        last_evaluation_at = outcome.evaluation_at
    return tuple(selected)


def _mean_lower_bound(
    values: tuple[Decimal, ...],
    *,
    z: Decimal,
) -> Decimal | None:
    if len(values) < 2:
        return None
    count = Decimal(len(values))
    mean = sum(values, Decimal("0")) / count
    variance = sum(((item - mean) ** 2 for item in values), Decimal("0")) / (
        count - 1
    )
    return mean - z * (variance / count).sqrt()
