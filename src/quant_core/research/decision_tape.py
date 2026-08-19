from __future__ import annotations

from bisect import bisect_right
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.engine import Engine

from quant_core.config import AppConfig
from quant_core.domain import (
    AccountSnapshot,
    AnalysisProposal,
    DirectionalForecast,
    DirectionalView,
    FeatureSnapshot,
    FrozenModel,
    MarketSnapshot,
    Side,
    SignalCandidate,
    _require_utc,
)
from quant_core.forecast_evaluation import unique_successful_codex_completion
from quant_core.ids import content_hash, stable_id
from quant_core.persistence import (
    analysis_cycles,
    analysis_proposals,
    codex_runs,
    market_snapshots,
)
from quant_core.research.backtest import (
    BacktestRun,
    ResearchStrategy,
    run_bar_backtest,
)
from quant_core.research.dataset import HistoricalDataset


class ForecastTapeEntry(FrozenModel):
    entry_id: str
    proposal_id: str
    cycle_id: str
    pipeline_version: str
    source_run_id: str
    symbol: str
    available_at: datetime
    horizon_minutes: int = Field(gt=0)
    directional_view: DirectionalView
    confidence: Decimal = Field(ge=0, le=1)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    _utc_available_at = field_validator("available_at")(_require_utc)

    @model_validator(mode="after")
    def identity_matches_frozen_source(self):
        payload = self.model_dump(
            mode="json", exclude={"entry_id", "source_hash"}
        )
        if self.source_hash != content_hash(payload):
            raise ValueError("决策带条目来源哈希不一致")
        expected_id = stable_id("forecast_tape_entry", self.source_hash)
        if self.entry_id != expected_id:
            raise ValueError("决策带条目 ID 不一致")
        return self

    @classmethod
    def freeze(
        cls,
        *,
        proposal: AnalysisProposal,
        forecast: DirectionalForecast,
        cycle_id: str,
        pipeline_version: str,
        source_run_id: str,
        available_at: datetime,
    ) -> ForecastTapeEntry:
        payload = {
            "proposal_id": proposal.proposal_id,
            "cycle_id": cycle_id,
            "pipeline_version": pipeline_version,
            "source_run_id": source_run_id,
            "symbol": proposal.symbol,
            "available_at": _require_utc(available_at),
            "horizon_minutes": forecast.horizon_minutes,
            "directional_view": forecast.directional_view,
            "confidence": forecast.confidence,
        }
        source_hash = content_hash(payload)
        return cls(
            entry_id=stable_id("forecast_tape_entry", source_hash),
            source_hash=source_hash,
            **payload,
        )


class ForecastTapeExclusion(FrozenModel):
    cycle_id: str
    reason_code: Literal["CODEX_COMPLETION_MISSING_OR_AMBIGUOUS"]


class ForecastDecisionTape(FrozenModel):
    tape_id: str
    version: str = "forecast-decision-tape-v1"
    pipeline_version: str
    symbol: str
    window_start: datetime
    window_end: datetime
    entries: tuple[ForecastTapeEntry, ...]
    exclusions: tuple[ForecastTapeExclusion, ...] = ()
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    _utc_window_start = field_validator("window_start")(_require_utc)
    _utc_window_end = field_validator("window_end")(_require_utc)

    @model_validator(mode="after")
    def bounds_order_and_hash_match(self):
        if self.window_start >= self.window_end:
            raise ValueError("决策带窗口起点必须早于终点")
        order = tuple(
            (item.available_at, item.horizon_minutes, item.entry_id)
            for item in self.entries
        )
        if order != tuple(sorted(order)) or len({item.entry_id for item in self.entries}) != len(
            self.entries
        ):
            raise ValueError("决策带条目必须唯一且按可用时间排序")
        if any(
            item.pipeline_version != self.pipeline_version
            or item.symbol != self.symbol
            or not self.window_start <= item.available_at < self.window_end
            for item in self.entries
        ):
            raise ValueError("决策带包含窗口或作用域外条目")
        payload = self.model_dump(mode="json", exclude={"tape_id", "content_hash"})
        if self.content_hash != content_hash(payload):
            raise ValueError("决策带内容哈希不一致")
        if self.tape_id != stable_id("forecast_decision_tape", self.content_hash):
            raise ValueError("决策带 ID 不一致")
        return self


class SqlForecastDecisionTapeReader:
    """只投影当时已存在的 Proposal 与成功完成事实；绝不读取结果标签。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def read(
        self,
        *,
        pipeline_version: str,
        symbol: str,
        window_start: datetime,
        window_end: datetime,
        maximum_completion_lag_seconds: int,
    ) -> ForecastDecisionTape:
        start = _require_utc(window_start)
        end = _require_utc(window_end)
        if start >= end:
            raise ValueError("决策带窗口起点必须早于终点")
        if maximum_completion_lag_seconds <= 0:
            raise ValueError("Codex 最大完成延迟必须为正数")
        query_start = start - timedelta(seconds=maximum_completion_lag_seconds)
        with self._engine.connect() as connection:
            rows = tuple(
                connection.execute(
                    select(
                        analysis_proposals.c.payload.label("proposal"),
                        analysis_cycles.c.cycle_id,
                        analysis_cycles.c.pipeline_version,
                        analysis_cycles.c.as_of,
                    )
                    .join(
                        analysis_cycles,
                        analysis_cycles.c.cycle_id == analysis_proposals.c.cycle_id,
                    )
                    .join(
                        market_snapshots,
                        market_snapshots.c.cycle_id == analysis_cycles.c.cycle_id,
                    )
                    .where(
                        analysis_cycles.c.pipeline_version == pipeline_version,
                        market_snapshots.c.symbol == symbol,
                        analysis_cycles.c.as_of >= query_start,
                        analysis_cycles.c.as_of < end,
                    )
                    .order_by(analysis_cycles.c.as_of, analysis_cycles.c.cycle_id)
                ).mappings()
            )
            cycle_ids = tuple(str(item["cycle_id"]) for item in rows)
            attempts = (
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
        attempts_by_cycle: dict[str, list[dict]] = {}
        for attempt in attempts:
            attempts_by_cycle.setdefault(str(attempt["cycle_id"]), []).append(
                dict(attempt)
            )

        entries: list[ForecastTapeEntry] = []
        exclusions: list[ForecastTapeExclusion] = []
        for row in rows:
            cycle_id = str(row["cycle_id"])
            analysis_as_of = _database_utc(row["as_of"])
            available_at, source_run_id = unique_successful_codex_completion(
                attempts_by_cycle.get(cycle_id, []),
                analysis_as_of=analysis_as_of,
            )
            if available_at is None or source_run_id is None:
                exclusions.append(
                    ForecastTapeExclusion(
                        cycle_id=cycle_id,
                        reason_code="CODEX_COMPLETION_MISSING_OR_AMBIGUOUS",
                    )
                )
                continue
            if not start <= available_at < end:
                continue
            proposal = AnalysisProposal.model_validate(row["proposal"])
            entries.extend(
                ForecastTapeEntry.freeze(
                    proposal=proposal,
                    forecast=forecast,
                    cycle_id=cycle_id,
                    pipeline_version=str(row["pipeline_version"]),
                    source_run_id=source_run_id,
                    available_at=available_at,
                )
                for forecast in proposal.forecasts
            )
        ordered = tuple(
            sorted(
                entries,
                key=lambda item: (
                    item.available_at,
                    item.horizon_minutes,
                    item.entry_id,
                ),
            )
        )
        payload = {
            "version": "forecast-decision-tape-v1",
            "pipeline_version": pipeline_version,
            "symbol": symbol,
            "window_start": start,
            "window_end": end,
            "entries": ordered,
            "exclusions": tuple(exclusions),
        }
        digest = content_hash(payload)
        return ForecastDecisionTape(
            tape_id=stable_id("forecast_decision_tape", digest),
            content_hash=digest,
            **payload,
        )


class ForecastGatePolicy(FrozenModel):
    plan_id: str
    version: str = "directional-alignment-gate-v1"
    registered_at: datetime
    horizon_minutes: int = Field(gt=0)
    maximum_age_minutes: int = Field(gt=0)
    minimum_confidence: Decimal = Field(ge=0, le=1)

    _utc_registered_at = field_validator("registered_at")(_require_utc)

    @model_validator(mode="after")
    def age_does_not_outlive_forecast(self):
        if self.maximum_age_minutes > self.horizon_minutes:
            raise ValueError("AI 门控使用年龄不能超过预测周期")
        return self


class ForecastGatedStrategySpec(FrozenModel):
    version: str = "forecast-gated-research-strategy-v1"
    base_strategy_spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy: ForecastGatePolicy
    tape_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ForecastGatedStrategy:
    """在基础 Q 生成候选后，只用当时已经可见的预测做方向门控。"""

    def __init__(
        self,
        base: ResearchStrategy,
        *,
        policy: ForecastGatePolicy,
        tape: ForecastDecisionTape,
    ) -> None:
        self._base = base
        self._policy = policy
        self._entries = tuple(
            item
            for item in tape.entries
            if item.horizon_minutes == policy.horizon_minutes
            and item.available_at >= policy.registered_at
        )
        self._available_times = tuple(item.available_at for item in self._entries)
        self._spec = ForecastGatedStrategySpec(
            base_strategy_spec_hash=content_hash(base.research_spec),
            policy=policy,
            tape_hash=tape.content_hash,
        )

    @property
    def research_spec(self) -> ForecastGatedStrategySpec:
        return self._spec

    def evaluate(
        self,
        *,
        market: MarketSnapshot,
        account: AccountSnapshot,
        features: FeatureSnapshot,
    ) -> tuple[SignalCandidate, ...]:
        candidates = self._base.evaluate(
            market=market,
            account=account,
            features=features,
        )
        if not candidates:
            return ()
        index = bisect_right(self._available_times, market.as_of) - 1
        if index < 0:
            return ()
        forecast = self._entries[index]
        if (
            forecast.symbol != market.symbol
            or market.as_of - forecast.available_at
            > timedelta(minutes=self._policy.maximum_age_minutes)
            or forecast.confidence < self._policy.minimum_confidence
        ):
            return ()
        return tuple(
            candidate
            for candidate in candidates
            if forecast.directional_view
            == (DirectionalView.UP if candidate.side == Side.BUY else DirectionalView.DOWN)
        )


class PairedDecisionTapeResult(FrozenModel):
    evaluation_id: str
    version: str = "paired-decision-tape-evaluation-v1"
    policy: ForecastGatePolicy
    policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    tape_id: str
    tape_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    tape_entry_ids: tuple[str, ...] = Field(min_length=1)
    baseline: BacktestRun
    gated: BacktestRun
    incremental_net_pnl: Decimal
    incremental_return_fraction: Decimal
    maximum_drawdown_change: Decimal
    trade_count_change: int
    common_candidate_count: int = Field(ge=0)
    baseline_only_candidate_count: int = Field(ge=0)
    gated_only_candidate_count: int = Field(ge=0)
    limitations: tuple[str, ...]

    @model_validator(mode="after")
    def identity_and_pair_match(self):
        if self.policy_hash != content_hash(self.policy):
            raise ValueError("配对回放门控策略哈希不一致")
        if len(self.tape_entry_ids) != len(set(self.tape_entry_ids)):
            raise ValueError("配对回放决策带条目不得重复")
        if (
            self.baseline.dataset_id != self.gated.dataset_id
            or self.baseline.symbol != self.gated.symbol
            or self.baseline.signal_start != self.gated.signal_start
            or self.baseline.signal_end != self.gated.signal_end
        ):
            raise ValueError("配对回放必须使用相同数据集、品种和窗口")
        payload = self.model_dump(mode="json", exclude={"evaluation_id"})
        if self.evaluation_id != stable_id("paired_decision_tape", content_hash(payload)):
            raise ValueError("配对决策带评价 ID 不一致")
        return self


def run_paired_decision_tape_backtest(
    *,
    dataset: HistoricalDataset,
    config: AppConfig,
    tape: ForecastDecisionTape,
    policy: ForecastGatePolicy,
    strategy: ResearchStrategy,
    signal_start: datetime,
    signal_end: datetime,
    replay_start: datetime | None = None,
    replay_end: datetime | None = None,
    starting_equity: Decimal = Decimal("10000"),
    spread_bps: Decimal = Decimal("1"),
) -> PairedDecisionTapeResult:
    start = _require_utc(signal_start)
    end = _require_utc(signal_end)
    if start < policy.registered_at:
        raise ValueError("配对评价窗口不能早于门控策略预登记时间")
    if tape.symbol != dataset.manifest.symbol:
        raise ValueError("决策带与历史数据品种不一致")
    eligible = tuple(
        item
        for item in tape.entries
        if item.horizon_minutes == policy.horizon_minutes
        and policy.registered_at <= item.available_at < end
    )
    if not eligible:
        raise ValueError("评价窗口没有预登记后首次生成的同周期预测")

    common = {
        "dataset": dataset,
        "config": config,
        "signal_start": start,
        "signal_end": end,
        "replay_start": replay_start,
        "replay_end": replay_end,
        "starting_equity": starting_equity,
        "spread_bps": spread_bps,
    }
    baseline = run_bar_backtest(strategy=strategy, **common)
    gated = run_bar_backtest(
        strategy=ForecastGatedStrategy(strategy, policy=policy, tape=tape),
        **common,
    )
    baseline_ids = {item.candidate_id for item in baseline.trades}
    gated_ids = {item.candidate_id for item in gated.trades}
    policy_hash = content_hash(policy)
    payload = {
        "version": "paired-decision-tape-evaluation-v1",
        "policy": policy,
        "policy_hash": policy_hash,
        "tape_id": tape.tape_id,
        "tape_hash": tape.content_hash,
        "tape_entry_ids": tuple(item.entry_id for item in eligible),
        "baseline": baseline,
        "gated": gated,
        "incremental_net_pnl": gated.metrics.net_pnl - baseline.metrics.net_pnl,
        "incremental_return_fraction": (
            gated.metrics.return_fraction - baseline.metrics.return_fraction
        ),
        "maximum_drawdown_change": (
            gated.metrics.maximum_drawdown_fraction
            - baseline.metrics.maximum_drawdown_fraction
        ),
        "trade_count_change": gated.metrics.trade_count - baseline.metrics.trade_count,
        "common_candidate_count": len(baseline_ids & gated_ids),
        "baseline_only_candidate_count": len(baseline_ids - gated_ids),
        "gated_only_candidate_count": len(gated_ids - baseline_ids),
        "limitations": (
            "FORWARD_FROZEN_CODEX_OUTPUTS_ONLY",
            "PAIRED_PATHS_MAY_DIVERGE_AFTER_GATE_DECISIONS",
            "NO_AI_OUTPUT_REGENERATION",
        ),
    }
    return PairedDecisionTapeResult(
        evaluation_id=stable_id("paired_decision_tape", content_hash(payload)),
        **payload,
    )


def _database_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
