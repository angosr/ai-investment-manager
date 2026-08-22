from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel, floor_to_step
from investment_manager.platform.artifacts import write_json_artifact
from investment_manager.research.carry import HistoricalCarryDataset
from investment_manager.research.dataset import HistoricalDataset


class CrossSectionalCarryPolicy(FrozenModel):
    """One preregistered, low-turnover BTC/ETH funding-carry hypothesis."""

    strategy_id: Literal["btc-eth-cross-sectional-carry"] = "btc-eth-cross-sectional-carry"
    version: Literal["btc-eth-weekly-28d-funding-rank-risk-30pct-v1"] = (
        "btc-eth-weekly-28d-funding-rank-risk-30pct-v1"
    )
    family: Literal["cross-sectional-delta-neutral-funding-carry"] = (
        "cross-sectional-delta-neutral-funding-carry"
    )
    symbols: tuple[str, str] = ("BTCUSDT", "ETHUSDT")
    review_weekday: Literal[0] = 0
    lookback_days: Literal[28] = 28
    advantage_horizon_days: Literal[28] = 28
    leg_equity_fraction: Decimal = Field(default=Decimal("0.15"), gt=0, le=Decimal("0.5"))
    spot_cost_bps: Decimal = Field(default=Decimal("12.5"), ge=0)
    futures_cost_bps: Decimal = Field(default=Decimal("7.5"), ge=0)
    maintenance_margin_fraction: Decimal = Field(default=Decimal("0.10"), gt=0, lt=1)
    one_leg_failure_move_bps: Decimal = Field(default=Decimal("100"), gt=0)

    @model_validator(mode="after")
    def universe_is_exact(self):
        if self.symbols != ("BTCUSDT", "ETHUSDT"):
            raise ValueError("横截面 carry v1 只允许冻结的 BTC/ETH 宇宙")
        return self

    @property
    def one_way_cost_bps(self) -> Decimal:
        return self.spot_cost_bps + self.futures_cost_bps

    @property
    def round_trip_cost_bps(self) -> Decimal:
        return Decimal("2") * self.one_way_cost_bps

    @property
    def switch_cost_bps(self) -> Decimal:
        return Decimal("2") * self.one_way_cost_bps


class CrossSectionalCarryPlan(FrozenModel):
    plan_id: str
    fold_count: Literal[5] = 5
    blind_days: Literal[365] = 365
    starting_equity: Decimal = Field(default=Decimal("10000"), gt=0)
    minimum_annualized_return_lower_bound: Decimal = Decimal("0")
    maximum_drawdown_fraction: Decimal = Field(default=Decimal("0.05"), gt=0, le=1)
    minimum_positive_fold_fraction: Decimal = Field(default=Decimal("0.75"), gt=0, le=1)
    minimum_margin_buffer_fraction: Decimal = Field(default=Decimal("0.05"), ge=0, le=1)
    maximum_one_leg_failure_loss_fraction: Decimal = Field(default=Decimal("0.01"), gt=0, le=1)


class CrossSectionalCarryEvaluationSpec(FrozenModel):
    version: Literal["cross-sectional-carry-evaluation-spec-v1"] = (
        "cross-sectional-carry-evaluation-spec-v1"
    )
    evidence_scope: Literal["REJECTION_ONLY_DEVELOPMENT_WINDOW"] = (
        "REJECTION_ONLY_DEVELOPMENT_WINDOW"
    )
    dataset_ids: tuple[tuple[str, str, str], ...]
    evaluator_code_version: str = Field(pattern=r"^[0-9a-f]{40}$")
    evaluator_environment: tuple[tuple[str, str], ...] = Field(min_length=2)
    policy: CrossSectionalCarryPolicy
    plan: CrossSectionalCarryPlan

    @model_validator(mode="after")
    def identities_are_exact(self):
        if tuple(sorted(self.dataset_ids)) != self.dataset_ids:
            raise ValueError("横截面 carry 数据身份必须唯一且排序")
        if tuple(symbol for symbol, _, _ in self.dataset_ids) != self.policy.symbols:
            raise ValueError("横截面 carry 数据身份与冻结宇宙不一致")
        if tuple(sorted(set(self.evaluator_environment))) != self.evaluator_environment:
            raise ValueError("横截面 carry 评价环境必须唯一且排序")
        return self

    @classmethod
    def freeze(
        cls,
        *,
        bundles: Mapping[str, CarryBundle],
        evaluator_code_version: str,
        evaluator_environment: tuple[tuple[str, str], ...],
        policy: CrossSectionalCarryPolicy,
        plan: CrossSectionalCarryPlan,
    ) -> CrossSectionalCarryEvaluationSpec:
        _validate_bundles(bundles, policy)
        return cls(
            dataset_ids=tuple(
                sorted(
                    (
                        symbol,
                        bundle.carry.manifest.dataset_id,
                        bundle.spot.manifest.dataset_id,
                    )
                    for symbol, bundle in bundles.items()
                )
            ),
            evaluator_code_version=evaluator_code_version,
            evaluator_environment=evaluator_environment,
            policy=policy,
            plan=plan,
        )


class CrossSectionalCarryAction(FrozenModel):
    at: datetime
    kind: Literal["ENTRY", "SWITCH"]
    from_symbol: str | None
    to_symbol: str
    projected_advantage_bps: Decimal
    modeled_cost: Decimal = Field(ge=0)

    _utc_at = field_validator("at")(require_utc)


class CrossSectionalCarryMetrics(FrozenModel):
    starting_equity: Decimal = Field(gt=0)
    ending_equity: Decimal
    net_pnl: Decimal
    funding_pnl: Decimal
    basis_pnl: Decimal
    modeled_cost: Decimal = Field(ge=0)
    return_fraction: Decimal
    simple_annualized_return_fraction: Decimal
    maximum_drawdown_fraction: Decimal = Field(ge=0)
    minimum_margin_buffer_fraction: Decimal
    maximum_one_leg_failure_loss_fraction: Decimal = Field(ge=0)
    entry_count: int = Field(ge=0)
    switch_count: int = Field(ge=0)
    review_count: int = Field(ge=0)
    exposed_day_count: int = Field(ge=0)
    liquidated: bool

    @model_validator(mode="after")
    def pnl_reconciles(self):
        if self.ending_equity - self.starting_equity != self.net_pnl:
            raise ValueError("横截面 carry 权益与净损益不一致")
        if self.funding_pnl + self.basis_pnl - self.modeled_cost != self.net_pnl:
            raise ValueError("横截面 carry 资金费、基差和成本无法核对")
        return self


class CrossSectionalCarryRun(FrozenModel):
    version: Literal["cross-sectional-carry-backtest-v1"] = "cross-sectional-carry-backtest-v1"
    run_id: str
    start: datetime
    end: datetime
    completed: bool
    reason_codes: tuple[str, ...]
    assumptions: tuple[str, ...]
    actions: tuple[CrossSectionalCarryAction, ...]
    metrics: CrossSectionalCarryMetrics

    _utc_start = field_validator("start")(require_utc)
    _utc_end = field_validator("end")(require_utc)


class CrossSectionalCarryFold(FrozenModel):
    fold_id: str
    start: datetime
    end: datetime
    run: CrossSectionalCarryRun

    _utc_start = field_validator("start")(require_utc)
    _utc_end = field_validator("end")(require_utc)


class CrossSectionalCarryWalkForwardMetrics(FrozenModel):
    average_annualized_return_fraction: Decimal
    annualized_return_lower_bound: Decimal
    positive_fold_fraction: Decimal = Field(ge=0, le=1)
    maximum_drawdown_fraction: Decimal = Field(ge=0)
    minimum_margin_buffer_fraction: Decimal
    maximum_one_leg_failure_loss_fraction: Decimal = Field(ge=0)
    aggregate_net_pnl: Decimal
    total_entry_count: int = Field(ge=0)
    total_switch_count: int = Field(ge=0)


class CrossSectionalCarryScreenResult(FrozenModel):
    version: Literal["cross-sectional-carry-screen-v1"] = "cross-sectional-carry-screen-v1"
    evaluation_id: str
    evaluation_spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_scope: Literal["REJECTION_ONLY_DEVELOPMENT_WINDOW"] = (
        "REJECTION_ONLY_DEVELOPMENT_WINDOW"
    )
    blind_start: datetime
    blind_end: datetime
    folds: tuple[CrossSectionalCarryFold, ...]
    metrics: CrossSectionalCarryWalkForwardMetrics
    passed_rejection_screen: bool
    reason_codes: tuple[str, ...]

    _utc_blind_start = field_validator("blind_start")(require_utc)
    _utc_blind_end = field_validator("blind_end")(require_utc)


class CrossSectionalCarryEnvelope(FrozenModel):
    result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: CrossSectionalCarryScreenResult


@dataclass(frozen=True, slots=True)
class CarryBundle:
    carry: HistoricalCarryDataset
    spot: HistoricalDataset


class CrossSectionalCarryCatalog:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def store(self, result: CrossSectionalCarryScreenResult) -> Path:
        target = self._root / f"{result.evaluation_id}.json"
        if target.exists():
            if self.load(result.evaluation_id) != result:
                raise ValueError("同一横截面 carry 评价 ID 的内容不一致")
            return target
        envelope = CrossSectionalCarryEnvelope(result_hash=content_hash(result), result=result)
        return write_json_artifact(
            root=self._root,
            target=target,
            prefix=".cross-sectional-carry-",
            payload=envelope,
        )

    def load(self, evaluation_id: str) -> CrossSectionalCarryScreenResult:
        raw = json.loads((self._root / f"{evaluation_id}.json").read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("result_hash") != content_hash(raw.get("result")):
            raise ValueError("横截面 carry 评价制品内容哈希不匹配")
        envelope = CrossSectionalCarryEnvelope.model_validate(raw)
        if envelope.result.evaluation_id != evaluation_id:
            raise ValueError("横截面 carry 评价文件名与内容 ID 不一致")
        return envelope.result


def run_cross_sectional_carry_backtest(
    *,
    bundles: Mapping[str, CarryBundle],
    policy: CrossSectionalCarryPolicy,
    starting_equity: Decimal,
    start: datetime,
    end: datetime,
) -> CrossSectionalCarryRun:
    start = require_utc(start)
    end = require_utc(end)
    if start >= end:
        raise ValueError("横截面 carry 回放起点必须早于终点")
    timeline = _validate_bundles(bundles, policy)
    days = tuple(item for item in timeline if start <= item < end)
    if not days or days[0] != start:
        raise ValueError("横截面 carry 回放必须从冻结日线开盘开始")

    spot_by_symbol = {
        symbol: {item.open_time: item for item in bundle.spot.bars}
        for symbol, bundle in bundles.items()
    }
    carry_by_symbol = {
        symbol: {item.open_time: item for item in bundle.carry.days}
        for symbol, bundle in bundles.items()
    }
    settlements_by_symbol_day = {
        symbol: _settlements_by_day(bundle.carry) for symbol, bundle in bundles.items()
    }

    equity = starting_equity
    peak = equity
    maximum_drawdown = Decimal("0")
    funding_pnl = Decimal("0")
    basis_pnl = Decimal("0")
    modeled_cost = Decimal("0")
    minimum_margin_buffer = Decimal("1")
    maximum_failure_loss = Decimal("0")
    current_symbol: str | None = None
    quantity = Decimal("0")
    futures_margin = Decimal("0")
    previous_spot_close: Decimal | None = None
    previous_contract_close: Decimal | None = None
    entry_count = 0
    switch_count = 0
    review_count = 0
    exposed_day_count = 0
    liquidated = False
    actions: list[CrossSectionalCarryAction] = []

    for day_open in days:
        if current_symbol is not None:
            spot = spot_by_symbol[current_symbol][day_open]
            carry_day = carry_by_symbol[current_symbol][day_open]
            assert previous_spot_close is not None and previous_contract_close is not None
            overnight = quantity * (
                (spot.open - previous_spot_close)
                - (carry_day.contract_open - previous_contract_close)
            )
            equity += overnight
            basis_pnl += overnight
            futures_margin -= quantity * (carry_day.contract_open - previous_contract_close)

        if day_open.weekday() == policy.review_weekday:
            review_count += 1
            scores = {
                symbol: _projected_funding_bps(
                    bundle.carry,
                    as_of=day_open,
                    lookback_days=policy.lookback_days,
                    horizon_days=policy.advantage_horizon_days,
                )
                for symbol, bundle in bundles.items()
            }
            if all(score is not None for score in scores.values()):
                ranked = sorted(
                    ((score, symbol) for symbol, score in scores.items() if score is not None),
                    key=lambda item: (-item[0], item[1]),
                )
                candidate_score, candidate_symbol = ranked[0]
                current_score = scores[current_symbol] if current_symbol is not None else None
                should_enter = (
                    current_symbol is None and candidate_score > policy.round_trip_cost_bps
                )
                should_switch = (
                    current_symbol is not None
                    and candidate_symbol != current_symbol
                    and current_score is not None
                    and candidate_score - current_score > policy.switch_cost_bps
                )
                if should_enter or should_switch:
                    action_cost = Decimal("0")
                    prior_symbol = current_symbol
                    if current_symbol is not None:
                        spot = spot_by_symbol[current_symbol][day_open]
                        carry_day = carry_by_symbol[current_symbol][day_open]
                        close_cost = (
                            quantity
                            * (
                                spot.open * policy.spot_cost_bps
                                + carry_day.contract_open * policy.futures_cost_bps
                            )
                            / Decimal("10000")
                        )
                        equity -= close_cost
                        modeled_cost += close_cost
                        action_cost += close_cost
                    candidate_spot = spot_by_symbol[candidate_symbol][day_open]
                    candidate_carry = carry_by_symbol[candidate_symbol][day_open]
                    quantity_step = max(
                        bundles[candidate_symbol].spot.manifest.instrument.quantity_increment,
                        bundles[candidate_symbol].carry.manifest.instrument.quantity_increment,
                    )
                    target_notional = equity * policy.leg_equity_fraction
                    quantity = floor_to_step(
                        target_notional / max(candidate_spot.open, candidate_carry.contract_open),
                        quantity_step,
                    )
                    _validate_order_quantity(
                        bundles[candidate_symbol],
                        quantity=quantity,
                        spot_price=candidate_spot.open,
                        contract_price=candidate_carry.contract_open,
                    )
                    open_cost = (
                        quantity
                        * (
                            candidate_spot.open * policy.spot_cost_bps
                            + candidate_carry.contract_open * policy.futures_cost_bps
                        )
                        / Decimal("10000")
                    )
                    equity -= open_cost
                    modeled_cost += open_cost
                    action_cost += open_cost
                    current_symbol = candidate_symbol
                    futures_margin = equity - quantity * candidate_spot.open
                    previous_spot_close = None
                    previous_contract_close = None
                    if should_enter:
                        entry_count += 1
                        advantage = candidate_score
                        kind: Literal["ENTRY", "SWITCH"] = "ENTRY"
                    else:
                        switch_count += 1
                        assert current_score is not None
                        advantage = candidate_score - current_score
                        kind = "SWITCH"
                    actions.append(
                        CrossSectionalCarryAction(
                            at=day_open,
                            kind=kind,
                            from_symbol=prior_symbol,
                            to_symbol=candidate_symbol,
                            projected_advantage_bps=advantage,
                            modeled_cost=action_cost,
                        )
                    )

        if current_symbol is None:
            peak = max(peak, equity)
            maximum_drawdown = max(maximum_drawdown, Decimal("1") - equity / peak)
            continue

        spot = spot_by_symbol[current_symbol][day_open]
        carry_day = carry_by_symbol[current_symbol][day_open]
        if previous_spot_close is None:
            previous_spot_close = spot.open
            previous_contract_close = carry_day.contract_open
        worst_short_loss = quantity * max(
            Decimal("0"), carry_day.mark_high - carry_day.contract_open
        )
        maintenance = quantity * carry_day.mark_high * policy.maintenance_margin_fraction
        margin_buffer = (futures_margin - worst_short_loss - maintenance) / equity
        minimum_margin_buffer = min(minimum_margin_buffer, margin_buffer)
        if margin_buffer <= 0:
            liquidated = True

        intraday = quantity * (
            (spot.close - spot.open) - (carry_day.contract_close - carry_day.contract_open)
        )
        equity += intraday
        basis_pnl += intraday
        futures_margin -= quantity * (carry_day.contract_close - carry_day.contract_open)
        daily_funding = sum(
            (
                quantity * settlement.mark_price * settlement.funding_rate
                for settlement in settlements_by_symbol_day[current_symbol].get(day_open, ())
            ),
            Decimal("0"),
        )
        equity += daily_funding
        futures_margin += daily_funding
        funding_pnl += daily_funding
        exposed_day_count += 1
        failure_loss = (
            quantity
            * max(spot.close, carry_day.contract_close)
            * policy.one_leg_failure_move_bps
            / Decimal("10000")
            / equity
        )
        maximum_failure_loss = max(maximum_failure_loss, failure_loss)
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, Decimal("1") - equity / peak)
        previous_spot_close = spot.close
        previous_contract_close = carry_day.contract_close

    if current_symbol is not None:
        assert previous_spot_close is not None and previous_contract_close is not None
        closing_cost = (
            quantity
            * (
                previous_spot_close * policy.spot_cost_bps
                + previous_contract_close * policy.futures_cost_bps
            )
            / Decimal("10000")
        )
        equity -= closing_cost
        modeled_cost += closing_cost
    maximum_drawdown = max(maximum_drawdown, Decimal("1") - equity / peak)
    net_pnl = equity - starting_equity
    elapsed_days = Decimal(str((days[-1] - days[0]).total_seconds() / 86400 + 1))
    annualized = net_pnl / starting_equity * Decimal("365.25") / elapsed_days
    reasons = ("LIQUIDATION_BOUND_BREACHED",) if liquidated else ()
    metrics = CrossSectionalCarryMetrics(
        starting_equity=starting_equity,
        ending_equity=equity,
        net_pnl=net_pnl,
        funding_pnl=funding_pnl,
        basis_pnl=basis_pnl,
        modeled_cost=modeled_cost,
        return_fraction=net_pnl / starting_equity,
        simple_annualized_return_fraction=annualized,
        maximum_drawdown_fraction=maximum_drawdown,
        minimum_margin_buffer_fraction=minimum_margin_buffer,
        maximum_one_leg_failure_loss_fraction=maximum_failure_loss,
        entry_count=entry_count,
        switch_count=switch_count,
        review_count=review_count,
        exposed_day_count=exposed_day_count,
        liquidated=liquidated,
    )
    run_id = stable_id(
        "cross_sectional_carry_run",
        tuple(
            (symbol, bundle.carry.manifest.dataset_id, bundle.spot.manifest.dataset_id)
            for symbol, bundle in sorted(bundles.items())
        ),
        policy,
        starting_equity,
        start,
        end,
        metrics,
        tuple(actions),
    )
    return CrossSectionalCarryRun(
        run_id=run_id,
        start=start,
        end=end,
        completed=not liquidated,
        reason_codes=reasons,
        assumptions=(
            "FIXED_BTC_ETH_POINT_IN_TIME_UNIVERSE",
            "MONDAY_UTC_REVIEW_ONLY",
            "TRAILING_28_DAY_SETTLED_FUNDING_VISIBLE_AFTER_AVAILABLE_AT",
            "PROJECTED_28_DAY_ADVANTAGE_MUST_COVER_FULL_SWITCH_COST",
            "SAME_BASE_QUANTITY_SPOT_LONG_PERPETUAL_SHORT",
            "MAXIMUM_GROSS_EXPOSURE_30_PERCENT",
            "CURRENT_RULE_SNAPSHOT_WITH_CONSERVATIVE_COST_AND_MARGIN",
            "REJECTION_ONLY_DEVELOPMENT_WINDOW",
            "NO_CODEX_REPLAY",
        ),
        actions=tuple(actions),
        metrics=metrics,
    )


def run_cross_sectional_carry_screen(
    *,
    bundles: Mapping[str, CarryBundle],
    spec: CrossSectionalCarryEvaluationSpec,
) -> CrossSectionalCarryScreenResult:
    timeline = _validate_bundles(bundles, spec.policy)
    actual_ids = tuple(
        sorted(
            (
                symbol,
                bundle.carry.manifest.dataset_id,
                bundle.spot.manifest.dataset_id,
            )
            for symbol, bundle in bundles.items()
        )
    )
    if actual_ids != spec.dataset_ids:
        raise ValueError("横截面 carry 运行数据与冻结规格不一致")
    development_count = len(timeline) - spec.plan.blind_days
    if development_count < spec.plan.fold_count * spec.policy.lookback_days * 2:
        raise ValueError("横截面 carry 开发区不足以形成冻结分折")
    base, remainder = divmod(development_count, spec.plan.fold_count)
    folds: list[CrossSectionalCarryFold] = []
    cursor = 0
    for index in range(spec.plan.fold_count):
        size = base + int(index < remainder)
        selected = timeline[cursor : cursor + size]
        start = selected[0]
        end = selected[-1] + timedelta(days=1)
        run = run_cross_sectional_carry_backtest(
            bundles=bundles,
            policy=spec.policy,
            starting_equity=spec.plan.starting_equity,
            start=start,
            end=end,
        )
        folds.append(
            CrossSectionalCarryFold(
                fold_id=stable_id(
                    "cross_sectional_carry_fold", spec.plan.plan_id, index, start, end
                ),
                start=start,
                end=end,
                run=run,
            )
        )
        cursor += size

    annualized = tuple(fold.run.metrics.simple_annualized_return_fraction for fold in folds)
    average = sum(annualized, Decimal("0")) / len(annualized)
    metrics = CrossSectionalCarryWalkForwardMetrics(
        average_annualized_return_fraction=average,
        annualized_return_lower_bound=_decimal_mean_lower_bound(annualized),
        positive_fold_fraction=Decimal(sum(value > 0 for value in annualized)) / len(annualized),
        maximum_drawdown_fraction=max(fold.run.metrics.maximum_drawdown_fraction for fold in folds),
        minimum_margin_buffer_fraction=min(
            fold.run.metrics.minimum_margin_buffer_fraction for fold in folds
        ),
        maximum_one_leg_failure_loss_fraction=max(
            fold.run.metrics.maximum_one_leg_failure_loss_fraction for fold in folds
        ),
        aggregate_net_pnl=sum((fold.run.metrics.net_pnl for fold in folds), Decimal("0")),
        total_entry_count=sum(fold.run.metrics.entry_count for fold in folds),
        total_switch_count=sum(fold.run.metrics.switch_count for fold in folds),
    )
    reasons: list[str] = []
    if any(not fold.run.completed for fold in folds):
        reasons.append("LIQUIDATION_BOUND_BREACHED")
    if metrics.annualized_return_lower_bound <= spec.plan.minimum_annualized_return_lower_bound:
        reasons.append("ANNUALIZED_RETURN_LOWER_BOUND_NOT_POSITIVE")
    if metrics.maximum_drawdown_fraction > spec.plan.maximum_drawdown_fraction:
        reasons.append("MAXIMUM_DRAWDOWN_EXCEEDED")
    if metrics.positive_fold_fraction < spec.plan.minimum_positive_fold_fraction:
        reasons.append("POSITIVE_FOLD_FRACTION_BELOW_GATE")
    if metrics.minimum_margin_buffer_fraction < spec.plan.minimum_margin_buffer_fraction:
        reasons.append("MARGIN_BUFFER_BELOW_GATE")
    if (
        metrics.maximum_one_leg_failure_loss_fraction
        > spec.plan.maximum_one_leg_failure_loss_fraction
    ):
        reasons.append("ONE_LEG_FAILURE_LOSS_EXCEEDED")
    if metrics.total_entry_count == 0:
        reasons.append("NO_NATURAL_ENTRY_SIGNAL")
    spec_hash = content_hash(spec)
    evaluation_id = stable_id(
        "cross_sectional_carry_screen",
        spec_hash,
        tuple(fold.run.run_id for fold in folds),
        metrics,
    )
    return CrossSectionalCarryScreenResult(
        evaluation_id=evaluation_id,
        evaluation_spec_hash=spec_hash,
        blind_start=timeline[development_count],
        blind_end=timeline[-1] + timedelta(days=1),
        folds=tuple(folds),
        metrics=metrics,
        passed_rejection_screen=not reasons,
        reason_codes=tuple(reasons),
    )


def _validate_bundles(
    bundles: Mapping[str, CarryBundle], policy: CrossSectionalCarryPolicy
) -> tuple[datetime, ...]:
    if tuple(sorted(bundles)) != policy.symbols:
        raise ValueError("横截面 carry 数据必须恰好覆盖冻结的 BTC/ETH 宇宙")
    timeline: tuple[datetime, ...] | None = None
    for symbol, bundle in sorted(bundles.items()):
        if (
            bundle.carry.manifest.symbol != symbol
            or bundle.spot.manifest.symbol != symbol
            or bundle.carry.manifest.spot_dataset_id != bundle.spot.manifest.dataset_id
            or bundle.carry.manifest.requested_start != bundle.spot.manifest.requested_start
            or bundle.carry.manifest.requested_end != bundle.spot.manifest.requested_end
        ):
            raise ValueError("横截面 carry 数据束身份不一致")
        current = tuple(day.open_time for day in bundle.carry.days)
        if current != tuple(bar.open_time for bar in bundle.spot.bars):
            raise ValueError("横截面 carry 现货与永续日线不对齐")
        if timeline is None:
            timeline = current
        elif timeline != current:
            raise ValueError("横截面 carry 资产时间轴不对齐")
    assert timeline is not None
    return timeline


def _settlements_by_day(
    carry: HistoricalCarryDataset,
) -> dict[datetime, tuple]:
    grouped: dict[datetime, list] = {}
    for settlement in carry.settlements:
        day = settlement.funding_time.replace(hour=0, minute=0, second=0, microsecond=0)
        grouped.setdefault(day, []).append(settlement)
    return {day: tuple(values) for day, values in grouped.items()}


def _projected_funding_bps(
    carry: HistoricalCarryDataset,
    *,
    as_of: datetime,
    lookback_days: int,
    horizon_days: int,
) -> Decimal | None:
    start = as_of - timedelta(days=lookback_days)
    visible = tuple(
        settlement
        for settlement in carry.settlements
        if start <= settlement.funding_time < as_of and settlement.available_at <= as_of
    )
    visible_hours = sum((settlement.funding_interval_hours for settlement in visible), 0)
    if visible_hours < lookback_days * 24:
        return None
    covered_days = Decimal(str((as_of - start).total_seconds() / 86400))
    trailing_daily_rate = (
        sum((settlement.funding_rate for settlement in visible), Decimal("0")) / covered_days
    )
    return trailing_daily_rate * Decimal(horizon_days) * Decimal("10000")


def _validate_order_quantity(
    bundle: CarryBundle,
    *,
    quantity: Decimal,
    spot_price: Decimal,
    contract_price: Decimal,
) -> None:
    if (
        quantity < bundle.spot.manifest.instrument.minimum_quantity
        or quantity < bundle.carry.manifest.instrument.minimum_quantity
        or quantity * spot_price < bundle.spot.manifest.instrument.minimum_notional
        or quantity * contract_price < bundle.carry.manifest.instrument.minimum_notional
    ):
        raise ValueError("横截面 carry 目标双腿低于冻结交易规则")


def _decimal_mean_lower_bound(values: tuple[Decimal, ...]) -> Decimal:
    if len(values) < 2:
        raise ValueError("横截面 carry 保守下界至少需要两个独立分折")
    count = Decimal(len(values))
    mean = sum(values, Decimal("0")) / count
    variance = sum(((value - mean) ** 2 for value in values), Decimal("0")) / (count - 1)
    return mean - Decimal("1.96") * (variance / count).sqrt()
