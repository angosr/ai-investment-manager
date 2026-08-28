from __future__ import annotations

import hashlib
import json
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from math import prod, sqrt
from pathlib import Path
from statistics import mean, stdev
from typing import Any

from investment_manager.research.dataset import HistoricalDatasetCatalog
from investment_manager.research.economic_series import HistoricalEconomicSeriesCatalog

_RISK_ASSETS = ("BTC", "PAXG", "US_EQUITY")
_ONE_SIXTH = 1 / 6


@dataclass(frozen=True, slots=True)
class MonthlyTrendInput:
    month: date
    returns: Mapping[str, float]
    directions: Mapping[str, int]
    cpi_level: float


@dataclass(frozen=True, slots=True)
class MonthlyPortfolioOutcome:
    month: date
    candidate_return: float
    stressed_candidate_return: float
    cash_return: float
    static_return: float
    candidate_turnover: float
    directions: Mapping[str, int]


def evaluate_diversified_economic_trend(
    plan_path: Path,
    *,
    dataset_root: Path,
    economic_root: Path,
    plan_commit: str,
    evaluator_commit: str,
    evaluated_at: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate exactly one preregistered rejection-only portfolio hypothesis."""

    plan_bytes = plan_path.read_bytes()
    plan = json.loads(plan_bytes)
    if plan.get("plan_id") != "diversified-economic-trend-v1":
        raise ValueError("组合趋势 evaluator 只接受冻结 v1 计划")
    _verify_plan_inputs(plan, dataset_root=dataset_root, economic_root=economic_root)
    monthly_inputs = _build_monthly_inputs(
        plan,
        dataset_root=dataset_root,
        economic_root=economic_root,
    )
    outcomes = simulate_monthly_portfolios(
        monthly_inputs,
        friction_bps=float(plan["candidate"]["one_way_friction_bps"]),
        stress_friction_bps=float(
            plan["candidate"]["stress_one_way_friction_bps"]
        ),
    )
    candidate = _metrics(outcomes, return_field="candidate_return", monthly_inputs=monthly_inputs)
    stressed = _metrics(
        outcomes,
        return_field="stressed_candidate_return",
        monthly_inputs=monthly_inputs,
    )
    cash = _metrics(outcomes, return_field="cash_return", monthly_inputs=monthly_inputs)
    static = _metrics(outcomes, return_field="static_return", monthly_inputs=monthly_inputs)
    regimes = tuple(
        _regime_result(outcomes, start=date.fromisoformat(start), end=date.fromisoformat(end))
        for start, end in plan["evaluation"]["regimes"]
    )
    gates = {
        "candidate_compound_above_cash": (
            candidate["compound_return"] > cash["compound_return"]
        ),
        "candidate_sharpe_above_static": (
            candidate["annualized_excess_sharpe"] is not None
            and static["annualized_excess_sharpe"] is not None
            and candidate["annualized_excess_sharpe"]
            > static["annualized_excess_sharpe"]
        ),
        "candidate_drawdown_below_static": (
            candidate["maximum_drawdown"] < static["maximum_drawdown"]
        ),
        "at_least_three_regimes_above_cash": (
            sum(item["candidate_above_cash"] for item in regimes) >= 3
        ),
        "stressed_compound_above_cash": (
            stressed["compound_return"] > cash["compound_return"]
        ),
    }
    passed = all(gates.values())
    return {
        "schema_version": "portfolio-candidate-result-v1",
        "plan_id": plan["plan_id"],
        "plan_commit": plan_commit,
        "plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
        "evaluated_at": (evaluated_at or datetime.now(UTC)).isoformat(),
        "evaluator_commit": evaluator_commit,
        "permission": "RESEARCH_FORWARD_CANDIDATE" if passed else "NONE",
        "status": "PASSED_RETROSPECTIVE" if passed else "REJECTED_RETROSPECTIVE",
        "sample": {
            "first_month": monthly_inputs[0].month.isoformat(),
            "last_month": monthly_inputs[-1].month.isoformat(),
            "month_count": len(monthly_inputs),
        },
        "candidate": _json_metrics(candidate),
        "stress_candidate": _json_metrics(stressed),
        "cash": _json_metrics(cash),
        "static_diversified": _json_metrics(static),
        "regimes": tuple(_json_regime(item) for item in regimes),
        "gates": gates,
        "rejection_reasons": tuple(key for key, value in gates.items() if not value),
        "capital_change": "NONE",
        "limitations": (
            "当前历史代理不证明当时发布日期，只能否决，不能授予资本。",
            "经济层 SHORT 仅取标的收益相反数；funding、basis、保证金和强平必须在产品层另行否决。",
            "结果产生后不得搜索相邻窗口、权重、摩擦或阶段边界。",
        ),
    }


def simulate_monthly_portfolios(
    rows: tuple[MonthlyTrendInput, ...],
    *,
    friction_bps: float,
    stress_friction_bps: float,
) -> tuple[MonthlyPortfolioOutcome, ...]:
    if not rows:
        raise ValueError("组合趋势评价缺少共同月度输入")
    previous_candidate = {asset: 0.0 for asset in _RISK_ASSETS}
    previous_static = {asset: 0.0 for asset in _RISK_ASSETS}
    outcomes: list[MonthlyPortfolioOutcome] = []
    for row in rows:
        if set(row.returns) != {*_RISK_ASSETS, "CASH"}:
            raise ValueError("组合趋势月度收益覆盖不完整")
        if set(row.directions) != set(_RISK_ASSETS) or any(
            value not in {-1, 0, 1} for value in row.directions.values()
        ):
            raise ValueError("组合趋势方向必须完整且属于 -1/0/1")
        candidate_targets = {
            asset: _ONE_SIXTH * row.directions[asset] for asset in _RISK_ASSETS
        }
        static_targets = {asset: _ONE_SIXTH for asset in _RISK_ASSETS}
        candidate_turnover = sum(
            abs(candidate_targets[asset] - previous_candidate[asset])
            for asset in _RISK_ASSETS
        )
        static_turnover = sum(
            abs(static_targets[asset] - previous_static[asset])
            for asset in _RISK_ASSETS
        )
        candidate_cash_weight = 1 - sum(abs(value) for value in candidate_targets.values())
        candidate_gross = candidate_cash_weight * row.returns["CASH"] + sum(
            candidate_targets[asset] * row.returns[asset] for asset in _RISK_ASSETS
        )
        static_gross = 0.5 * row.returns["CASH"] + sum(
            static_targets[asset] * row.returns[asset] for asset in _RISK_ASSETS
        )
        outcomes.append(
            MonthlyPortfolioOutcome(
                month=row.month,
                candidate_return=candidate_gross - candidate_turnover * friction_bps / 10_000,
                stressed_candidate_return=(
                    candidate_gross - candidate_turnover * stress_friction_bps / 10_000
                ),
                cash_return=row.returns["CASH"],
                static_return=static_gross - static_turnover * friction_bps / 10_000,
                candidate_turnover=candidate_turnover,
                directions=dict(row.directions),
            )
        )
        previous_candidate = candidate_targets
        previous_static = static_targets
    return tuple(outcomes)


def _build_monthly_inputs(
    plan: Mapping[str, Any],
    *,
    dataset_root: Path,
    economic_root: Path,
) -> tuple[MonthlyTrendInput, ...]:
    datasets = HistoricalDatasetCatalog(dataset_root)
    economic = HistoricalEconomicSeriesCatalog(economic_root)
    btc = datasets.load(plan["datasets"]["btc_spot_daily"]["dataset_id"])
    paxg = datasets.load(plan["datasets"]["paxg_spot_daily"]["dataset_id"])
    equity = economic.load(plan["datasets"]["us_equity_total_return"]["dataset_id"])
    cash = economic.load(plan["datasets"]["cash_total_return"]["dataset_id"])
    cpi = economic.load(plan["datasets"]["cpi_deflator"]["dataset_id"])
    btc_levels = _monthly_bar_levels(btc.bars)
    paxg_levels = _monthly_bar_levels(paxg.bars)
    btc_returns = _level_returns(btc_levels)
    paxg_returns = _level_returns(paxg_levels)
    equity_returns = _monthly_total_returns(equity.observations)
    cash_returns = _monthly_total_returns(cash.observations)
    cpi_levels = {
        item.effective_date.replace(day=1): float(item.value) for item in cpi.observations
    }
    btc_daily = tuple((item.close_time.date(), float(item.close)) for item in btc.bars)
    months = sorted(set(btc_returns) & set(paxg_returns) & set(equity_returns) & set(cash_returns))
    rows: list[MonthlyTrendInput] = []
    for month in months:
        prior_months = _prior_months(month, count=12)
        if not all(item in paxg_returns and item in equity_returns for item in prior_months):
            continue
        btc_direction = _btc_84_day_direction(btc_daily, month)
        if btc_direction is None:
            continue
        directions = {
            "BTC": btc_direction,
            "PAXG": _sign(prod(1 + paxg_returns[item] for item in prior_months) - 1),
            "US_EQUITY": _sign(
                prod(1 + equity_returns[item] for item in prior_months) - 1
            ),
        }
        if month not in cpi_levels:
            continue
        rows.append(
            MonthlyTrendInput(
                month=month,
                returns={
                    "BTC": btc_returns[month],
                    "PAXG": paxg_returns[month],
                    "US_EQUITY": equity_returns[month],
                    "CASH": cash_returns[month],
                },
                directions=directions,
                cpi_level=cpi_levels[month],
            )
        )
    if not rows:
        raise ValueError("组合趋势没有完成预热的共同月份")
    return tuple(rows)


def _verify_plan_inputs(
    plan: Mapping[str, Any],
    *,
    dataset_root: Path,
    economic_root: Path,
) -> None:
    roots = {
        "btc_spot_daily": dataset_root,
        "paxg_spot_daily": dataset_root,
        "us_equity_total_return": economic_root,
        "cash_total_return": economic_root,
        "cpi_deflator": economic_root,
    }
    for key, root in roots.items():
        requirement = plan["datasets"][key]
        target = root / requirement["dataset_id"]
        manifest = target / "manifest.json"
        payload_name = "bars.json" if root == dataset_root else "observations.json"
        if _sha256(manifest) != requirement["manifest_sha256"]:
            raise ValueError(f"{key} manifest 与预登记哈希不一致")
        if _sha256(target / payload_name) != requirement["payload_sha256"]:
            raise ValueError(f"{key} payload 与预登记哈希不一致")


def _monthly_bar_levels(bars) -> dict[date, float]:
    levels: dict[date, float] = {}
    for bar in bars:
        levels[date(bar.open_time.year, bar.open_time.month, 1)] = float(bar.close)
    return levels


def _level_returns(levels: Mapping[date, float]) -> dict[date, float]:
    ordered = sorted(levels)
    return {
        current: levels[current] / levels[previous] - 1
        for previous, current in pairwise(ordered)
        if current == _next_month(previous)
    }


def _monthly_total_returns(observations) -> dict[date, float]:
    grouped: dict[date, list[float]] = defaultdict(list)
    for item in observations:
        grouped[item.effective_date.replace(day=1)].append(float(item.value))
    return {month: prod(1 + value for value in values) - 1 for month, values in grouped.items()}


def _btc_84_day_direction(
    levels: tuple[tuple[date, float], ...],
    month: date,
) -> int | None:
    dates = tuple(item[0] for item in levels)
    recent_index = bisect_left(dates, month) - 1
    if recent_index < 0:
        return None
    comparison_at = dates[recent_index] - timedelta(days=84)
    comparison_index = bisect_left(dates, comparison_at)
    if comparison_index >= recent_index:
        return None
    return _sign(levels[recent_index][1] / levels[comparison_index][1] - 1)


def _metrics(
    outcomes: tuple[MonthlyPortfolioOutcome, ...],
    *,
    return_field: str,
    monthly_inputs: tuple[MonthlyTrendInput, ...],
) -> dict[str, float | int | None]:
    returns = tuple(float(getattr(item, return_field)) for item in outcomes)
    cash_returns = tuple(item.cash_return for item in outcomes)
    equity = 1.0
    high_water = 1.0
    maximum_drawdown = 0.0
    for value in returns:
        equity *= 1 + value
        high_water = max(high_water, equity)
        maximum_drawdown = max(maximum_drawdown, 1 - equity / high_water)
    excess = tuple(value - cash for value, cash in zip(returns, cash_returns, strict=True))
    sharpe = None
    if len(excess) > 1 and stdev(excess) > 0:
        sharpe = mean(excess) / stdev(excess) * sqrt(12)
    years = len(returns) / 12
    inflation_growth = monthly_inputs[-1].cpi_level / monthly_inputs[0].cpi_level
    return {
        "month_count": len(returns),
        "compound_return": equity - 1,
        "annualized_return": equity ** (1 / years) - 1,
        "annualized_excess_sharpe": sharpe,
        "maximum_drawdown": maximum_drawdown,
        "annualized_turnover": (
            sum(item.candidate_turnover for item in outcomes) / years
            if return_field in {"candidate_return", "stressed_candidate_return"}
            else (0.5 / years if return_field == "static_return" else 0.0)
        ),
        "real_compound_return": equity / inflation_growth - 1,
    }


def _regime_result(
    outcomes: tuple[MonthlyPortfolioOutcome, ...],
    *,
    start: date,
    end: date,
) -> dict[str, Any]:
    selected = tuple(item for item in outcomes if start <= item.month < end)
    if not selected:
        raise ValueError("冻结阶段没有共同观测")
    candidate = prod(1 + item.candidate_return for item in selected) - 1
    cash = prod(1 + item.cash_return for item in selected) - 1
    return {
        "start": start,
        "end": end,
        "month_count": len(selected),
        "candidate_return": candidate,
        "cash_return": cash,
        "candidate_above_cash": candidate > cash,
    }


def _prior_months(month: date, *, count: int) -> tuple[date, ...]:
    values: list[date] = []
    current = month
    for _ in range(count):
        current = date(current.year - 1, 12, 1) if current.month == 1 else date(
            current.year, current.month - 1, 1
        )
        values.append(current)
    return tuple(reversed(values))


def _next_month(month: date) -> date:
    return date(month.year + 1, 1, 1) if month.month == 12 else date(
        month.year, month.month + 1, 1
    )


def _sign(value: float) -> int:
    return (value > 0) - (value < 0)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _decimal_string(value: float | int | None) -> str | int | None:
    if value is None or isinstance(value, int):
        return value
    return format(Decimal(str(value)), "f")


def _json_metrics(values: Mapping[str, float | int | None]) -> dict[str, str | int | None]:
    return {key: _decimal_string(value) for key, value in values.items()}


def _json_regime(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: (
            value.isoformat()
            if isinstance(value, date)
            else _decimal_string(value)
            if isinstance(value, float)
            else value
        )
        for key, value in values.items()
    }
