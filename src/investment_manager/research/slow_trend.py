"""One frozen, low-turnover trend hypothesis; never imported by production."""

from __future__ import annotations

import json
from bisect import bisect_right
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from statistics import mean, stdev
from typing import Any

import yaml

from investment_manager.kernel.identity import content_hash
from investment_manager.research.carry import (
    CarryFundingSettlement,
    CarryMarketBar,
    HistoricalCarryDataset,
)

_BPS = Decimal("10000")
_ANNUAL_WEEKS = Decimal("52")


@dataclass(frozen=True, slots=True)
class SlowTrendPlan:
    plan_id: str
    carry_dataset_id: str
    source_start: datetime
    source_end: datetime
    formation_days: int
    cost_bps_per_executed_notional: Decimal
    regimes: tuple[tuple[datetime, datetime], ...]
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _Signal:
    at: datetime
    direction: int
    formation_return: Decimal


@dataclass(frozen=True, slots=True)
class _Simulation:
    started_at: datetime
    ended_at: datetime
    ending_equity: Decimal
    daily_equity: tuple[Decimal, ...]
    period_pnl: tuple[Decimal, ...]
    turnover_notional: Decimal
    execution_cost: Decimal
    funding_pnl: Decimal
    trade_count: int


def load_slow_trend_plan(path: Path) -> SlowTrendPlan:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("慢趋势计划根节点必须是对象")
    if raw.get("schema_version") != "slow-trend-candidate-plan-v1":
        raise ValueError("慢趋势计划 schema_version 不受支持")
    data = _mapping(raw, "data")
    rule = _mapping(raw, "rule")
    cost = _mapping(raw, "cost")
    evaluation = _mapping(raw, "evaluation")
    source_window = data.get("source_window")
    regimes = evaluation.get("fixed_regimes")
    if not isinstance(source_window, list) or len(source_window) != 2:
        raise ValueError("慢趋势计划必须声明两个 source_window 边界")
    if not isinstance(regimes, list) or not regimes:
        raise ValueError("慢趋势计划必须声明固定市场阶段")
    parsed_regimes = tuple(
        (_utc_datetime(item[0]), _utc_datetime(item[1]))
        for item in regimes
        if isinstance(item, list) and len(item) == 2
    )
    if len(parsed_regimes) != len(regimes):
        raise ValueError("慢趋势固定市场阶段格式非法")
    plan = SlowTrendPlan(
        plan_id=str(raw["plan_id"]),
        carry_dataset_id=str(data["carry_dataset_id"]),
        source_start=_utc_datetime(source_window[0]),
        source_end=_utc_datetime(source_window[1]),
        formation_days=int(rule["formation_days"]),
        cost_bps_per_executed_notional=Decimal(
            str(cost["total_bps_per_executed_notional"])
        ),
        regimes=parsed_regimes,
        raw=raw,
    )
    _validate_frozen_semantics(plan)
    return plan


def evaluate_slow_trend_candidate(
    *,
    plan: SlowTrendPlan,
    dataset: HistoricalCarryDataset,
    plan_commit: str,
    evaluator_code_version: str,
    evaluated_at: datetime,
) -> dict[str, Any]:
    manifest = dataset.manifest
    if (
        manifest.dataset_id != plan.carry_dataset_id
        or manifest.symbol != "BTCUSDT"
        or manifest.interval != "1d"
        or manifest.requested_start != plan.source_start
        or manifest.requested_end != plan.source_end
        or not dataset.settlements
    ):
        raise ValueError("慢趋势计划与冻结 BTC carry 数据身份不一致")
    signals = _weekly_signals(dataset.bars, formation_days=plan.formation_days)
    if not signals or signals[0].at != plan.regimes[0][0]:
        raise ValueError("首个可用周信号与预注册阶段起点不一致")
    start = signals[0].at
    end = plan.source_end
    strategy = _simulate(
        bars=dataset.bars,
        settlements=dataset.settlements,
        signals=signals,
        start=start,
        end=end,
        cost_bps=plan.cost_bps_per_executed_notional,
        always_long=False,
    )
    benchmark = _simulate(
        bars=dataset.bars,
        settlements=dataset.settlements,
        signals=signals,
        start=start,
        end=end,
        cost_bps=plan.cost_bps_per_executed_notional,
        always_long=True,
    )
    regime_results = []
    for regime_start, regime_end in plan.regimes:
        simulation = _simulate(
            bars=dataset.bars,
            settlements=dataset.settlements,
            signals=signals,
            start=regime_start,
            end=regime_end,
            cost_bps=plan.cost_bps_per_executed_notional,
            always_long=False,
        )
        regime_results.append(
            {
                "start": regime_start.isoformat(),
                "end": regime_end.isoformat(),
                "net_return": str(simulation.ending_equity - Decimal("1")),
                "positive": simulation.ending_equity > 1,
            }
        )
    metrics = _metrics(strategy)
    benchmark_metrics = _metrics(benchmark)
    positive_regimes = sum(item["positive"] for item in regime_results)
    gross_weekly_gains = sum((item for item in strategy.period_pnl if item > 0), Decimal(0))
    gross_weekly_losses = -sum(
        (item for item in strategy.period_pnl if item < 0), Decimal(0)
    )
    checks = {
        "positive_net_return": strategy.ending_equity > 1,
        "positive_annualized_sharpe": Decimal(metrics["annualized_sharpe"]) > 0,
        "lower_drawdown_than_always_long": Decimal(metrics["maximum_drawdown"])
        < Decimal(benchmark_metrics["maximum_drawdown"]),
        "at_least_three_positive_regimes": positive_regimes >= 3,
        "weekly_profit_factor_above_one": gross_weekly_gains > gross_weekly_losses,
    }
    passed = all(checks.values())
    return {
        "schema_version": "slow-trend-candidate-result-v1",
        "plan_id": plan.plan_id,
        "plan_commit": plan_commit,
        "plan_hash": content_hash(plan.raw),
        "evaluator_code_version": evaluator_code_version,
        "evaluated_at": evaluated_at.astimezone(UTC).isoformat(),
        "status": "PASSED_RETROSPECTIVE" if passed else "REJECTED_RETROSPECTIVE",
        "permission": "FORWARD_RESEARCH_ONLY" if passed else "REJECTION_ONLY",
        "method": "fixed-84d-sign-weekly-state-transition-v1",
        "dataset": {
            "dataset_id": manifest.dataset_id,
            "bars_hash": manifest.bars_hash,
            "settlements_hash": manifest.settlements_hash,
            "bar_count": manifest.bar_count,
            "settlement_count": manifest.settlement_count,
        },
        "sample": {
            "first_signal_at": signals[0].at.isoformat(),
            "last_signal_at": signals[-1].at.isoformat(),
            "weekly_period_count": len(strategy.period_pnl),
            "long_signal_count": sum(item.direction > 0 for item in signals),
            "short_signal_count": sum(item.direction < 0 for item in signals),
        },
        "strategy": metrics,
        "always_long": benchmark_metrics,
        "regimes": regime_results,
        "checks": checks,
        "rejection_reasons": [name for name, passed_check in checks.items() if not passed_check],
        "capital_change": "NONE",
        "conclusion": (
            "固定 12 周慢趋势通过回顾性否决筛选，只能进入真实报价前向研究。"
            if passed
            else "固定 12 周慢趋势未通过预注册回顾性筛选，不进入前向研究或资本链。"
        ),
    }


def store_slow_trend_result(result: dict[str, Any], target: Path) -> Path:
    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if target.exists():
        if target.read_text(encoding="utf-8") != payload:
            raise ValueError("慢趋势结果已存在且内容不一致")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload, encoding="utf-8")
    return target


def _weekly_signals(
    bars: tuple[CarryMarketBar, ...], *, formation_days: int
) -> tuple[_Signal, ...]:
    signals = []
    for index, bar in enumerate(bars):
        if bar.open_time.weekday() != 0 or index <= formation_days:
            continue
        formation_return = (
            bars[index - 1].contract_close
            / bars[index - 1 - formation_days].contract_close
            - 1
        )
        direction = 1 if formation_return > 0 else -1 if formation_return < 0 else 0
        signals.append(
            _Signal(
                at=bar.open_time,
                direction=direction,
                formation_return=formation_return,
            )
        )
    return tuple(signals)


def _simulate(
    *,
    bars: tuple[CarryMarketBar, ...],
    settlements: tuple[CarryFundingSettlement, ...],
    signals: tuple[_Signal, ...],
    start: datetime,
    end: datetime,
    cost_bps: Decimal,
    always_long: bool,
) -> _Simulation:
    if start >= end:
        raise ValueError("慢趋势模拟起点必须早于终点")
    signal_by_time = {item.at: item.direction for item in signals}
    if start not in signal_by_time:
        raise ValueError("慢趋势模拟阶段必须从冻结周信号开始")
    selected_bars = tuple(item for item in bars if start <= item.open_time < end)
    if not selected_bars or selected_bars[0].open_time != start:
        raise ValueError("慢趋势模拟窗口缺少起点 K 线")

    equity = Decimal("1")
    quantity = Decimal(0)
    last_time = start
    last_price = selected_bars[0].contract_open
    turnover = execution_cost = funding_pnl = Decimal(0)
    trade_count = 0
    daily_equity = [equity]
    period_pnl: list[Decimal] = []
    period_equity = equity
    settlement_times = tuple(item.funding_time for item in settlements)

    for bar in selected_bars:
        if bar.open_time > last_time:
            price_pnl, paid_funding = _holding_pnl(
                quantity,
                last_price,
                bar.contract_open,
                last_time,
                bar.open_time,
                settlements,
                settlement_times,
            )
            equity += price_pnl + paid_funding
            funding_pnl += paid_funding
            last_time = bar.open_time
            last_price = bar.contract_open
        if bar.open_time in signal_by_time:
            direction = 1 if always_long else signal_by_time[bar.open_time]
            current_direction = 1 if quantity > 0 else -1 if quantity < 0 else 0
            if direction != current_direction:
                target_quantity = Decimal(direction) * equity / bar.contract_open
                executed_notional = abs(target_quantity - quantity) * bar.contract_open
                cost = executed_notional * cost_bps / _BPS
                equity -= cost
                turnover += executed_notional
                execution_cost += cost
                quantity = target_quantity
                trade_count += 1
            if bar.open_time > start:
                period_pnl.append(equity - period_equity)
                period_equity = equity
        price_pnl, paid_funding = _holding_pnl(
            quantity,
            last_price,
            bar.contract_close,
            last_time,
            bar.close_time,
            settlements,
            settlement_times,
        )
        equity += price_pnl + paid_funding
        funding_pnl += paid_funding
        last_time = bar.close_time
        last_price = bar.contract_close
        daily_equity.append(equity)
        if equity <= 0:
            raise ValueError("慢趋势模拟权益归零，候选不具备无杠杆可执行性")

    exit_price = next(
        (item.contract_open for item in bars if item.open_time == end),
        selected_bars[-1].contract_close,
    )
    price_pnl, paid_funding = _holding_pnl(
        quantity,
        last_price,
        exit_price,
        last_time,
        end,
        settlements,
        settlement_times,
    )
    equity += price_pnl + paid_funding
    funding_pnl += paid_funding
    exit_notional = abs(quantity) * exit_price
    exit_cost = exit_notional * cost_bps / _BPS
    equity -= exit_cost
    turnover += exit_notional
    execution_cost += exit_cost
    trade_count += int(quantity != 0)
    period_pnl.append(equity - period_equity)
    daily_equity.append(equity)
    if equity <= 0:
        raise ValueError("慢趋势模拟最终权益归零")
    return _Simulation(
        started_at=start,
        ended_at=end,
        ending_equity=equity,
        daily_equity=tuple(daily_equity),
        period_pnl=tuple(period_pnl),
        turnover_notional=turnover,
        execution_cost=execution_cost,
        funding_pnl=funding_pnl,
        trade_count=trade_count,
    )


def _holding_pnl(
    quantity: Decimal,
    old_price: Decimal,
    new_price: Decimal,
    old_time: datetime,
    new_time: datetime,
    settlements: tuple[CarryFundingSettlement, ...],
    settlement_times: tuple[datetime, ...],
) -> tuple[Decimal, Decimal]:
    price_pnl = quantity * (new_price - old_price)
    left = bisect_right(settlement_times, old_time)
    right = bisect_right(settlement_times, new_time)
    funding_pnl = -quantity * sum(
        (
            item.mark_price * item.funding_rate
            for item in settlements[left:right]
        ),
        Decimal(0),
    )
    return price_pnl, funding_pnl


def _metrics(simulation: _Simulation) -> dict[str, Any]:
    period_returns = []
    equity = Decimal("1")
    for pnl in simulation.period_pnl:
        period_returns.append(pnl / equity)
        equity += pnl
    years = Decimal(str((simulation.ended_at - simulation.started_at).total_seconds())) / Decimal(
        str(365.25 * 24 * 60 * 60)
    )
    annualized_return = Decimal(
        str(float(simulation.ending_equity) ** (1 / float(years)) - 1)
    )
    sharpe = Decimal(0)
    if len(period_returns) > 1 and stdev(period_returns) > 0:
        sharpe = Decimal(str(mean(period_returns) / stdev(period_returns))) * _ANNUAL_WEEKS.sqrt()
    peak = simulation.daily_equity[0]
    maximum_drawdown = Decimal(0)
    for point in simulation.daily_equity:
        peak = max(peak, point)
        maximum_drawdown = max(maximum_drawdown, (peak - point) / peak)
    gains = sum((item for item in simulation.period_pnl if item > 0), Decimal(0))
    losses = -sum((item for item in simulation.period_pnl if item < 0), Decimal(0))
    return {
        "net_compounded_return": str(simulation.ending_equity - 1),
        "annualized_net_return": str(annualized_return),
        "annualized_sharpe": str(sharpe),
        "maximum_drawdown": str(maximum_drawdown),
        "weekly_profit_factor": None if losses == 0 else str(gains / losses),
        "turnover_notional_per_initial_equity": str(simulation.turnover_notional),
        "execution_cost_per_initial_equity": str(simulation.execution_cost),
        "funding_pnl_per_initial_equity": str(simulation.funding_pnl),
        "trade_count": simulation.trade_count,
    }


def _validate_frozen_semantics(plan: SlowTrendPlan) -> None:
    expected_regime_edges = [plan.regimes[0][0], *(item[1] for item in plan.regimes)]
    if (
        plan.plan_id != "btc-slow-trend-12w-v1"
        or plan.formation_days != 84
        or plan.cost_bps_per_executed_notional != Decimal("7.5")
        or plan.source_start != datetime(2020, 1, 1, tzinfo=UTC)
        or plan.source_end != datetime(2026, 8, 1, tzinfo=UTC)
        or any(
            left != right
            for left, right in zip(
                expected_regime_edges[1:-1],
                (item[0] for item in plan.regimes[1:]),
                strict=True,
            )
        )
        or plan.regimes[-1][1] != plan.source_end
    ):
        raise ValueError("慢趋势计划偏离已冻结的单候选语义")


def _mapping(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"慢趋势计划缺少 {key} 对象")
    return value


def _utc_datetime(value: Any) -> datetime:
    parsed = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("慢趋势时间必须带 UTC 时区")
    return parsed.astimezone(UTC)
