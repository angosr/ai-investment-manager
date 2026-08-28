from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any, Literal

import httpx
import yaml
from pydantic import Field, field_validator, model_validator

from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel
from investment_manager.market.models import ClosedMarketBar
from investment_manager.platform.artifacts import write_json_artifact
from investment_manager.research.dataset import HistoricalDataset

_BPS = Decimal("10000")
_HOUR = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class FixedMaturityCarryPlan:
    plan_id: str
    plan_hash: str
    observed_cutoff: datetime
    settlement_start: datetime
    settlement_end: datetime
    starting_equity: Decimal
    entry_days_before_delivery: int
    leg_notional_fraction: Decimal
    futures_collateral_fraction: Decimal
    quantity_step: Decimal
    minimum_notional: Decimal
    spot_fee_bps: Decimal
    futures_fee_bps: Decimal
    friction_bps: Decimal
    total_round_trip_bps: Decimal
    maintenance_margin_fraction: Decimal
    minimum_completed_contracts: int
    minimum_entered_contracts: int
    minimum_positive_trade_fraction: Decimal
    minimum_positive_regimes: int
    maximum_drawdown_fraction: Decimal
    minimum_margin_buffer_fraction: Decimal
    regimes: tuple[tuple[datetime, datetime], ...]


class DatedFutureBar(FrozenModel):
    open_time: datetime
    close_time: datetime
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: Decimal = Field(ge=0)

    _utc_open = field_validator("open_time")(require_utc)
    _utc_close = field_validator("close_time")(require_utc)

    @model_validator(mode="after")
    def candle_is_valid(self):
        if self.close_time - self.open_time != _HOUR - timedelta(milliseconds=1):
            raise ValueError("交割合约必须是完整 1h K 线")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("交割合约 OHLC 边界非法")
        return self


class DatedContractEvidence(FrozenModel):
    contract_symbol: str
    delivery_time: datetime
    delivery_price: Decimal = Field(gt=0)
    bars: tuple[DatedFutureBar, ...]

    _utc_delivery = field_validator("delivery_time")(require_utc)

    @model_validator(mode="after")
    def identity_and_window_are_valid(self):
        suffix = self.delivery_time.strftime("%y%m%d")
        if self.contract_symbol != f"BTCUSDT_{suffix}":
            raise ValueError("交割合约身份与交割日期不一致")
        start = self.delivery_time - timedelta(days=30)
        open_times = tuple(bar.open_time for bar in self.bars)
        if open_times != tuple(sorted(set(open_times))):
            raise ValueError("交割合约 K 线时间必须严格递增且唯一")
        if any(not start <= item < self.delivery_time for item in open_times):
            raise ValueError("交割合约 K 线超出预注册窗口")
        return self


class FixedMaturityCarryDatasetManifest(FrozenModel):
    schema_version: Literal["fixed-maturity-carry-dataset-v1"] = "fixed-maturity-carry-dataset-v1"
    dataset_id: str
    plan_id: str
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    spot_dataset_id: str
    collected_at: datetime
    settlement_start: datetime
    settlement_end: datetime
    contract_count: int = Field(gt=0)
    records_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    _utc_collected = field_validator("collected_at")(require_utc)
    _utc_start = field_validator("settlement_start")(require_utc)
    _utc_end = field_validator("settlement_end")(require_utc)

    @model_validator(mode="after")
    def identity_is_valid(self):
        expected = stable_id(
            "fixed_maturity_carry_dataset",
            self.schema_version,
            self.plan_id,
            self.plan_hash,
            self.spot_dataset_id,
            self.settlement_start,
            self.settlement_end,
            self.records_hash,
        )
        if self.dataset_id != expected:
            raise ValueError("固定到期 carry 数据集身份不一致")
        return self


@dataclass(frozen=True, slots=True)
class FixedMaturityCarryDataset:
    manifest: FixedMaturityCarryDatasetManifest
    contracts: tuple[DatedContractEvidence, ...]

    def __post_init__(self) -> None:
        if len(self.contracts) != self.manifest.contract_count:
            raise ValueError("固定到期 carry 合约数量与 Manifest 不一致")
        if tuple(item.delivery_time for item in self.contracts) != tuple(
            sorted(item.delivery_time for item in self.contracts)
        ):
            raise ValueError("固定到期 carry 合约必须按交割时间排序")
        if content_hash(self.contracts) != self.manifest.records_hash:
            raise ValueError("固定到期 carry 合约内容哈希不一致")


class FixedMaturityContractOutcome(FrozenModel):
    contract_symbol: str
    delivery_time: datetime
    status: Literal["INCOMPLETE", "SKIPPED_COST", "ENTERED", "LIQUIDATED"]
    reason: str | None = None
    equity_before: Decimal = Field(gt=0)
    equity_after: Decimal = Field(gt=0)
    quantity: Decimal = Field(ge=0)
    spot_entry: Decimal | None = Field(default=None, gt=0)
    futures_entry: Decimal | None = Field(default=None, gt=0)
    spot_exit: Decimal | None = Field(default=None, gt=0)
    delivery_price: Decimal = Field(gt=0)
    entry_basis_bps: Decimal | None = None
    modeled_cost: Decimal = Field(ge=0)
    net_pnl: Decimal
    return_fraction: Decimal
    minimum_margin_buffer_fraction: Decimal | None = None
    maximum_drawdown_fraction: Decimal = Field(ge=0)

    _utc_delivery = field_validator("delivery_time")(require_utc)


class FixedMaturityRegimeOutcome(FrozenModel):
    start: datetime
    end: datetime
    entered_contract_count: int = Field(ge=0)
    net_return_fraction: Decimal

    _utc_start = field_validator("start")(require_utc)
    _utc_end = field_validator("end")(require_utc)


class FixedMaturityCarryResult(FrozenModel):
    schema_version: Literal["fixed-maturity-carry-result-v1"] = "fixed-maturity-carry-result-v1"
    result_id: str
    plan_id: str
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    evaluator_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    dataset_id: str
    spot_dataset_id: str
    evaluated_at: datetime
    status: Literal["PASSED_RETROSPECTIVE", "REJECTED_RETROSPECTIVE"]
    reason_codes: tuple[str, ...]
    official_contract_count: int = Field(gt=0)
    complete_contract_count: int = Field(ge=0)
    entered_contract_count: int = Field(ge=0)
    positive_entered_contract_fraction: Decimal | None = None
    starting_equity: Decimal = Field(gt=0)
    ending_equity: Decimal = Field(gt=0)
    net_return_fraction: Decimal
    annualized_net_return_on_deployed_spot_capital: Decimal | None = None
    maximum_account_drawdown_fraction: Decimal = Field(ge=0)
    minimum_futures_margin_buffer_fraction: Decimal | None = None
    regimes: tuple[FixedMaturityRegimeOutcome, ...]
    contracts: tuple[FixedMaturityContractOutcome, ...]
    limitations: tuple[str, ...]

    _utc_evaluated = field_validator("evaluated_at")(require_utc)


def load_fixed_maturity_carry_plan(path: Path) -> FixedMaturityCarryPlan:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("固定到期 carry 计划必须是对象")
    if raw.get("schema_version") != "fixed-maturity-carry-candidate-plan-v1":
        raise ValueError("固定到期 carry 计划 Schema 不一致")
    if raw.get("plan_id") != "btc-quarterly-cash-carry-v1":
        raise ValueError("固定到期 carry 计划身份不一致")
    data = raw["data"]
    rule = raw["rule"]
    cost = raw["cost"]
    risk = raw["risk"]
    evaluation = raw["evaluation"]
    required_literals = {
        data.get("history_semantics"): "retrospective_falsification_not_blind",
        rule.get("entry_time"): ("first_hour_open_at_or_after_30_calendar_days_before_delivery"),
        rule.get("holding"): "no_rebalance_and_hold_to_official_delivery",
        rule.get("parameter_search"): "prohibited",
        evaluation.get("permission"): "REJECTION_OR_FORWARD_RESEARCH_ONLY",
    }
    for actual, expected in required_literals.items():
        if actual != expected:
            raise ValueError(f"固定到期 carry 计划语义必须为 {expected}")
    settlement_start, settlement_end = (_parse_time(item) for item in data["settlement_window"])
    regimes = tuple(
        (_parse_time(start), _parse_time(end)) for start, end in evaluation["fixed_regimes"]
    )
    if any(start >= end for start, end in regimes):
        raise ValueError("固定到期 carry 阶段边界非法")
    plan = FixedMaturityCarryPlan(
        plan_id=str(raw["plan_id"]),
        plan_hash=content_hash(raw),
        observed_cutoff=_parse_time(data["observed_cutoff"]),
        settlement_start=settlement_start,
        settlement_end=settlement_end,
        starting_equity=Decimal(str(rule["starting_equity_usdt"])),
        entry_days_before_delivery=30,
        leg_notional_fraction=Decimal(str(rule["target_leg_notional_fraction_of_current_equity"])),
        futures_collateral_fraction=Decimal(
            str(rule["futures_cash_collateral_fraction_of_current_equity"])
        ),
        quantity_step=Decimal(str(rule["futures_quantity_step"])),
        minimum_notional=Decimal(str(rule["futures_minimum_notional"])),
        spot_fee_bps=Decimal(str(cost["spot_fee_bps_per_side"])),
        futures_fee_bps=Decimal(str(cost["futures_fee_bps_per_side"])),
        friction_bps=Decimal(str(cost["spread_and_impact_allowance_bps_per_leg_side"])),
        total_round_trip_bps=Decimal(str(cost["total_round_trip_bps_on_equal_leg_notional"])),
        maintenance_margin_fraction=Decimal(str(risk["futures_maintenance_margin_fraction"])),
        minimum_completed_contracts=int(evaluation["minimum_completed_contracts"]),
        minimum_entered_contracts=8,
        minimum_positive_trade_fraction=Decimal("0.75"),
        minimum_positive_regimes=3,
        maximum_drawdown_fraction=Decimal("0.10"),
        minimum_margin_buffer_fraction=Decimal("0.10"),
        regimes=regimes,
    )
    if plan.starting_equity != Decimal("10000"):
        raise ValueError("固定到期 carry 起始权益必须与预注册 1 万 USDT 一致")
    if plan.leg_notional_fraction + plan.futures_collateral_fraction != 1:
        raise ValueError("固定到期 carry 现货资金与合约抵押比例必须覆盖完整权益")
    calculated_cost = Decimal("2") * (
        plan.spot_fee_bps + plan.futures_fee_bps + Decimal("2") * plan.friction_bps
    )
    if calculated_cost != plan.total_round_trip_bps:
        raise ValueError("固定到期 carry 逐边成本与总成本不一致")
    return plan


async def fetch_fixed_maturity_carry_dataset(
    *,
    plan: FixedMaturityCarryPlan,
    spot_dataset: HistoricalDataset,
    timeout_seconds: int,
    base_url: str = "https://fapi.binance.com",
    clock: Any | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FixedMaturityCarryDataset:
    if spot_dataset.manifest.symbol != "BTCUSDT" or spot_dataset.manifest.interval != "1h":
        raise ValueError("固定到期 carry 必须使用 BTCUSDT 1h 现货数据")
    collected_at = require_utc((clock or (lambda: datetime.now(UTC)))())
    if collected_at < plan.observed_cutoff:
        raise ValueError("评价时间不能早于预注册信息截止")
    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout_seconds,
        follow_redirects=False,
        transport=transport,
    ) as client:
        response = await client.get(
            "/futures/data/delivery-price",
            params={"pair": "BTCUSDT"},
        )
        response.raise_for_status()
        deliveries = _parse_deliveries(
            response.json(),
            start=plan.settlement_start,
            end=plan.settlement_end,
        )
        contracts = await asyncio.gather(
            *(
                _fetch_contract(client, delivery_time, delivery_price)
                for delivery_time, delivery_price in deliveries
            )
        )
    ordered = tuple(sorted(contracts, key=lambda item: item.delivery_time))
    records_hash = content_hash(ordered)
    payload = {
        "schema_version": "fixed-maturity-carry-dataset-v1",
        "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash,
        "spot_dataset_id": spot_dataset.manifest.dataset_id,
        "settlement_start": plan.settlement_start,
        "settlement_end": plan.settlement_end,
        "records_hash": records_hash,
    }
    manifest = FixedMaturityCarryDatasetManifest(
        dataset_id=stable_id("fixed_maturity_carry_dataset", *payload.values()),
        collected_at=collected_at,
        contract_count=len(ordered),
        **payload,
    )
    return FixedMaturityCarryDataset(manifest=manifest, contracts=ordered)


def evaluate_fixed_maturity_carry(
    *,
    plan: FixedMaturityCarryPlan,
    spot_dataset: HistoricalDataset,
    dataset: FixedMaturityCarryDataset,
    plan_commit: str,
    evaluator_commit: str,
    evaluated_at: datetime,
) -> FixedMaturityCarryResult:
    evaluated_at = require_utc(evaluated_at)
    if dataset.manifest.plan_hash != plan.plan_hash:
        raise ValueError("固定到期 carry 数据与计划不一致")
    spot_by_time = {bar.open_time: bar for bar in spot_dataset.bars}
    equity = plan.starting_equity
    high_water = equity
    maximum_drawdown = Decimal("0")
    outcomes: list[FixedMaturityContractOutcome] = []
    deployed_capital_years = Decimal("0")
    for contract in dataset.contracts:
        outcome, path_values, deployed = _evaluate_contract(
            plan=plan,
            contract=contract,
            spot_by_time=spot_by_time,
            equity_before=equity,
        )
        for marked_equity in path_values:
            high_water = max(high_water, marked_equity)
            maximum_drawdown = max(
                maximum_drawdown,
                (high_water - marked_equity) / high_water,
            )
        equity = outcome.equity_after
        high_water = max(high_water, equity)
        outcomes.append(outcome)
        deployed_capital_years += deployed

    complete = tuple(item for item in outcomes if item.status != "INCOMPLETE")
    entered = tuple(item for item in outcomes if item.status in {"ENTERED", "LIQUIDATED"})
    positive_fraction = (
        None
        if not entered
        else Decimal(sum(item.net_pnl > 0 for item in entered)) / Decimal(len(entered))
    )
    minimum_margin_buffer = (
        min(
            item.minimum_margin_buffer_fraction
            for item in entered
            if item.minimum_margin_buffer_fraction is not None
        )
        if entered
        else None
    )
    regimes = _regime_outcomes(plan, tuple(outcomes))
    positive_regimes = sum(item.net_return_fraction > 0 for item in regimes)
    reasons: list[str] = []
    if len(complete) < plan.minimum_completed_contracts:
        reasons.append("MINIMUM_COMPLETE_CONTRACTS_NOT_MET")
    if len(entered) < plan.minimum_entered_contracts:
        reasons.append("MINIMUM_ENTERED_CONTRACTS_NOT_MET")
    if equity <= plan.starting_equity:
        reasons.append("ACCOUNT_NET_RETURN_NOT_POSITIVE")
    if positive_fraction is None or positive_fraction < plan.minimum_positive_trade_fraction:
        reasons.append("POSITIVE_CONTRACT_FRACTION_BELOW_GATE")
    if positive_regimes < plan.minimum_positive_regimes:
        reasons.append("POSITIVE_REGIME_COUNT_BELOW_GATE")
    if maximum_drawdown > plan.maximum_drawdown_fraction:
        reasons.append("MAXIMUM_DRAWDOWN_EXCEEDED")
    if any(item.status == "LIQUIDATED" for item in entered):
        reasons.append("LIQUIDATION_OCCURRED")
    if minimum_margin_buffer is None or minimum_margin_buffer < plan.minimum_margin_buffer_fraction:
        reasons.append("MINIMUM_MARGIN_BUFFER_BELOW_GATE")
    annualized = (
        None
        if deployed_capital_years <= 0
        else sum(item.net_pnl for item in entered) / deployed_capital_years
    )
    status = "REJECTED_RETROSPECTIVE" if reasons else "PASSED_RETROSPECTIVE"
    identity = {
        "plan_hash": plan.plan_hash,
        "dataset_id": dataset.manifest.dataset_id,
        "spot_dataset_id": spot_dataset.manifest.dataset_id,
        "evaluator_commit": evaluator_commit,
        "contracts": outcomes,
    }
    return FixedMaturityCarryResult(
        result_id=stable_id("fixed_maturity_carry_result", content_hash(identity)),
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        plan_commit=plan_commit,
        evaluator_commit=evaluator_commit,
        dataset_id=dataset.manifest.dataset_id,
        spot_dataset_id=spot_dataset.manifest.dataset_id,
        evaluated_at=evaluated_at,
        status=status,
        reason_codes=tuple(reasons) or ("ALL_PREREGISTERED_GATES_PASSED",),
        official_contract_count=len(outcomes),
        complete_contract_count=len(complete),
        entered_contract_count=len(entered),
        positive_entered_contract_fraction=positive_fraction,
        starting_equity=plan.starting_equity,
        ending_equity=equity,
        net_return_fraction=equity / plan.starting_equity - 1,
        annualized_net_return_on_deployed_spot_capital=annualized,
        maximum_account_drawdown_fraction=maximum_drawdown,
        minimum_futures_margin_buffer_fraction=minimum_margin_buffer,
        regimes=regimes,
        contracts=tuple(outcomes),
        limitations=(
            "历史小时 K 线没有可执行 bid/ask，使用预注册固定摩擦只能否决，不能授权资本。",
            "历史手续费等级与交易规则快照不可得，使用冻结的保守普通费率。",
            "现货盈利不计入交割合约可用保证金，未虚构跨钱包抵押。",
            "结果已查看后禁止搜索其他到期日、阈值、仓位或退出规则。",
        ),
    )


def store_fixed_maturity_dataset(
    dataset: FixedMaturityCarryDataset,
    *,
    root: Path,
) -> Path:
    target = root / dataset.manifest.dataset_id
    manifest_path = target / "manifest.json"
    contracts_path = target / "contracts.json"
    if target.exists():
        manifest = FixedMaturityCarryDatasetManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
        contracts = tuple(
            DatedContractEvidence.model_validate(item)
            for item in json.loads(contracts_path.read_text(encoding="utf-8"))
        )
        if manifest != dataset.manifest or contracts != dataset.contracts:
            raise ValueError("固定到期 carry 数据集身份对应不同内容")
        return target
    target.mkdir(parents=True)
    write_json_artifact(
        root=target,
        target=manifest_path,
        prefix=".manifest-",
        payload=dataset.manifest,
    )
    write_json_artifact(
        root=target,
        target=contracts_path,
        prefix=".contracts-",
        payload=dataset.contracts,
    )
    return target


def store_fixed_maturity_result(
    result: FixedMaturityCarryResult,
    *,
    target: Path,
) -> Path:
    if target.exists():
        existing = FixedMaturityCarryResult.model_validate_json(target.read_text(encoding="utf-8"))
        if existing != result:
            raise ValueError("固定到期 carry 结果目标已存在不同内容")
        return target
    return write_json_artifact(
        root=target.parent,
        target=target,
        prefix=".fixed-maturity-result-",
        payload=result,
    )


def _evaluate_contract(
    *,
    plan: FixedMaturityCarryPlan,
    contract: DatedContractEvidence,
    spot_by_time: dict[datetime, ClosedMarketBar],
    equity_before: Decimal,
) -> tuple[FixedMaturityContractOutcome, tuple[Decimal, ...], Decimal]:
    entry_time = contract.delivery_time - timedelta(days=plan.entry_days_before_delivery)
    spot_entry_bar = spot_by_time.get(entry_time)
    spot_exit_bar = spot_by_time.get(contract.delivery_time)
    future_by_time = {bar.open_time: bar for bar in contract.bars}
    expected_times = tuple(entry_time + index * _HOUR for index in range(30 * 24))
    missing_spot = tuple(item for item in expected_times if item not in spot_by_time)
    missing_future = tuple(item for item in expected_times if item not in future_by_time)
    if spot_entry_bar is None or spot_exit_bar is None or missing_spot or missing_future:
        reason = (
            f"spot_entry={spot_entry_bar is not None},spot_exit={spot_exit_bar is not None},"
            f"missing_spot_hours={len(missing_spot)},missing_future_hours={len(missing_future)}"
        )
        return (
            FixedMaturityContractOutcome(
                contract_symbol=contract.contract_symbol,
                delivery_time=contract.delivery_time,
                status="INCOMPLETE",
                reason=reason,
                equity_before=equity_before,
                equity_after=equity_before,
                quantity=Decimal("0"),
                delivery_price=contract.delivery_price,
                modeled_cost=Decimal("0"),
                net_pnl=Decimal("0"),
                return_fraction=Decimal("0"),
                maximum_drawdown_fraction=Decimal("0"),
            ),
            (equity_before,),
            Decimal("0"),
        )
    future_entry = future_by_time[entry_time].open
    spot_entry = spot_entry_bar.open
    spot_exit = spot_exit_bar.open
    basis_bps = (future_entry / spot_entry - 1) * _BPS
    quantity = _floor_quantity(
        min(
            equity_before * plan.leg_notional_fraction / spot_entry,
            equity_before * plan.leg_notional_fraction / future_entry,
        ),
        plan.quantity_step,
    )
    if (
        basis_bps <= plan.total_round_trip_bps
        or quantity * min(spot_entry, future_entry) < plan.minimum_notional
    ):
        return (
            FixedMaturityContractOutcome(
                contract_symbol=contract.contract_symbol,
                delivery_time=contract.delivery_time,
                status="SKIPPED_COST",
                reason="LOCKED_BASIS_DID_NOT_EXCEED_FULL_COST",
                equity_before=equity_before,
                equity_after=equity_before,
                quantity=Decimal("0"),
                spot_entry=spot_entry,
                futures_entry=future_entry,
                spot_exit=spot_exit,
                delivery_price=contract.delivery_price,
                entry_basis_bps=basis_bps,
                modeled_cost=Decimal("0"),
                net_pnl=Decimal("0"),
                return_fraction=Decimal("0"),
                maximum_drawdown_fraction=Decimal("0"),
            ),
            (equity_before,),
            Decimal("0"),
        )

    spot_side_bps = plan.spot_fee_bps + plan.friction_bps
    future_side_bps = plan.futures_fee_bps + plan.friction_bps
    entry_cost = quantity * (spot_entry * spot_side_bps + future_entry * future_side_bps) / _BPS
    collateral = equity_before - quantity * spot_entry - entry_cost
    path: list[Decimal] = []
    minimum_buffer = Decimal("Infinity")
    local_high = equity_before
    local_drawdown = Decimal("0")
    liquidation: tuple[datetime, Decimal, Decimal] | None = None
    for at in expected_times:
        spot = spot_by_time[at]
        future = future_by_time[at]
        short_pnl = quantity * (future_entry - future.high)
        margin_equity = collateral + short_pnl
        maintenance = quantity * future.high * plan.maintenance_margin_fraction
        buffer = (margin_equity - maintenance) / equity_before
        minimum_buffer = min(minimum_buffer, buffer)
        estimated_exit_cost = (
            quantity * (spot.low * spot_side_bps + future.high * future_side_bps) / _BPS
        )
        marked = collateral + quantity * spot.low + short_pnl - estimated_exit_cost
        marked = max(marked, Decimal("0.00000001"))
        path.append(marked)
        local_high = max(local_high, marked)
        local_drawdown = max(local_drawdown, (local_high - marked) / local_high)
        if buffer < 0:
            liquidation = (at, marked, estimated_exit_cost)
            break

    deployed = quantity * spot_entry * Decimal(plan.entry_days_before_delivery) / Decimal(365)
    if liquidation is not None:
        at, marked, _exit_cost = liquidation
        net_pnl = marked - equity_before
        return (
            FixedMaturityContractOutcome(
                contract_symbol=contract.contract_symbol,
                delivery_time=contract.delivery_time,
                status="LIQUIDATED",
                reason=f"MARGIN_BUFFER_BREACHED_AT_{at.isoformat()}",
                equity_before=equity_before,
                equity_after=marked,
                quantity=quantity,
                spot_entry=spot_entry,
                futures_entry=future_entry,
                spot_exit=spot_by_time[at].low,
                delivery_price=contract.delivery_price,
                entry_basis_bps=basis_bps,
                modeled_cost=entry_cost + _exit_cost,
                net_pnl=net_pnl,
                return_fraction=net_pnl / equity_before,
                minimum_margin_buffer_fraction=minimum_buffer,
                maximum_drawdown_fraction=local_drawdown,
            ),
            tuple(path),
            deployed,
        )

    exit_cost = (
        quantity * (spot_exit * spot_side_bps + contract.delivery_price * future_side_bps) / _BPS
    )
    modeled_cost = entry_cost + exit_cost
    gross_pnl = quantity * (spot_exit - spot_entry + future_entry - contract.delivery_price)
    net_pnl = gross_pnl - modeled_cost
    equity_after = max(equity_before + net_pnl, Decimal("0.00000001"))
    path.append(equity_after)
    local_high = max(local_high, equity_after)
    local_drawdown = max(local_drawdown, (local_high - equity_after) / local_high)
    return (
        FixedMaturityContractOutcome(
            contract_symbol=contract.contract_symbol,
            delivery_time=contract.delivery_time,
            status="ENTERED",
            equity_before=equity_before,
            equity_after=equity_after,
            quantity=quantity,
            spot_entry=spot_entry,
            futures_entry=future_entry,
            spot_exit=spot_exit,
            delivery_price=contract.delivery_price,
            entry_basis_bps=basis_bps,
            modeled_cost=modeled_cost,
            net_pnl=net_pnl,
            return_fraction=net_pnl / equity_before,
            minimum_margin_buffer_fraction=minimum_buffer,
            maximum_drawdown_fraction=local_drawdown,
        ),
        tuple(path),
        deployed,
    )


def _regime_outcomes(
    plan: FixedMaturityCarryPlan,
    outcomes: tuple[FixedMaturityContractOutcome, ...],
) -> tuple[FixedMaturityRegimeOutcome, ...]:
    result: list[FixedMaturityRegimeOutcome] = []
    for index, (start, end) in enumerate(plan.regimes):
        selected = tuple(
            item
            for item in outcomes
            if start <= item.delivery_time
            and (
                item.delivery_time < end
                or (index == len(plan.regimes) - 1 and item.delivery_time == end)
            )
        )
        if selected:
            net_return = selected[-1].equity_after / selected[0].equity_before - 1
        else:
            net_return = Decimal("0")
        result.append(
            FixedMaturityRegimeOutcome(
                start=start,
                end=end,
                entered_contract_count=sum(
                    item.status in {"ENTERED", "LIQUIDATED"} for item in selected
                ),
                net_return_fraction=net_return,
            )
        )
    return tuple(result)


def _parse_deliveries(
    raw: Any,
    *,
    start: datetime,
    end: datetime,
) -> tuple[tuple[datetime, Decimal], ...]:
    if not isinstance(raw, list):
        raise ValueError("Binance 交割价响应必须是数组")
    parsed: dict[datetime, Decimal] = {}
    try:
        for item in raw:
            delivery = datetime.fromtimestamp(int(item["deliveryTime"]) / 1000, tz=UTC)
            if not start <= delivery <= end:
                continue
            price = Decimal(str(item["deliveryPrice"]))
            if price <= 0 or delivery in parsed:
                raise ValueError("Binance 交割价时间或价格非法")
            parsed[delivery] = price
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("Binance"):
            raise
        raise ValueError("Binance 交割价条目非法") from exc
    result = tuple(sorted(parsed.items()))
    if not result or result[0][0] != start or result[-1][0] != end:
        raise ValueError("Binance 交割价没有覆盖预注册边界")
    return result


async def _fetch_contract(
    client: httpx.AsyncClient,
    delivery_time: datetime,
    delivery_price: Decimal,
) -> DatedContractEvidence:
    symbol = f"BTCUSDT_{delivery_time.strftime('%y%m%d')}"
    start = delivery_time - timedelta(days=30)
    response = await client.get(
        "/fapi/v1/klines",
        params={
            "symbol": symbol,
            "interval": "1h",
            "startTime": int(start.timestamp() * 1000),
            "endTime": int(delivery_time.timestamp() * 1000) - 1,
            "limit": 1000,
        },
    )
    response.raise_for_status()
    raw = response.json()
    if not isinstance(raw, list):
        raise ValueError(f"{symbol} K 线响应必须是数组")
    bars: list[DatedFutureBar] = []
    try:
        for item in raw:
            if not isinstance(item, list) or len(item) < 7:
                raise ValueError(f"{symbol} K 线条目非法")
            bars.append(
                DatedFutureBar(
                    open_time=datetime.fromtimestamp(int(item[0]) / 1000, tz=UTC),
                    close_time=datetime.fromtimestamp(int(item[6]) / 1000, tz=UTC),
                    open=Decimal(str(item[1])),
                    high=Decimal(str(item[2])),
                    low=Decimal(str(item[3])),
                    close=Decimal(str(item[4])),
                    volume=Decimal(str(item[5])),
                )
            )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith(symbol):
            raise
        raise ValueError(f"{symbol} K 线条目非法") from exc
    return DatedContractEvidence(
        contract_symbol=symbol,
        delivery_time=delivery_time,
        delivery_price=delivery_price,
        bars=tuple(bars),
    )


def _floor_quantity(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return require_utc(value)
    return require_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
