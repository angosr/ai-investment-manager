"""Immutable cash and passive-Spot counterfactuals for the capital account."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import Field, field_validator, model_validator
from sqlalchemy import insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.governance.tables import capital_benchmark_points
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import FrozenModel, Money, PositiveDecimal, UnitInterval
from investment_manager.market.models import InstrumentId, MarketQuote
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.portfolio.models import PortfolioAccountSnapshot
from investment_manager.portfolio.tables import portfolio_account_snapshots
from investment_manager.settings import AppConfig

_BPS = Decimal("10000")
CAPITAL_BENCHMARK_EVALUATION_VERSION = "capital-cash-passive-spot-v1"


class CapitalBenchmarkPolicy(FrozenModel):
    policy_id: str = Field(min_length=1)
    evaluation_version: str = Field(min_length=1)
    portfolio_id: str = Field(min_length=1)
    instrument: InstrumentId
    allocation_fraction: UnitInterval
    quantity_step: PositiveDecimal
    minimum_order_notional: Money
    fee_bps: Money

    @model_validator(mode="after")
    def identity_and_scope_are_frozen(self):
        if self.allocation_fraction <= 0 or self.fee_bps < 0:
            raise ValueError("Capital benchmark allocation 必须为正且费用不能为负")
        payload = self.model_dump(exclude={"policy_id"}, mode="json")
        if self.policy_id != stable_id("capital_benchmark_policy", content_hash(payload)):
            raise ValueError("Capital benchmark policy_id 与冻结口径不一致")
        return self

    @classmethod
    def create(
        cls,
        *,
        portfolio_id: str,
        instrument: InstrumentId,
        allocation_fraction: Decimal,
        quantity_step: Decimal,
        minimum_order_notional: Decimal,
        fee_bps: Decimal,
    ) -> CapitalBenchmarkPolicy:
        payload = {
            "evaluation_version": CAPITAL_BENCHMARK_EVALUATION_VERSION,
            "portfolio_id": portfolio_id,
            "instrument": instrument,
            "allocation_fraction": allocation_fraction,
            "quantity_step": quantity_step,
            "minimum_order_notional": minimum_order_notional,
            "fee_bps": fee_bps,
        }
        return cls(
            policy_id=stable_id("capital_benchmark_policy", content_hash(payload)),
            **payload,
        )


class CapitalBenchmarkPoint(FrozenModel):
    point_id: str = Field(min_length=1)
    policy_id: str = Field(min_length=1)
    account_snapshot_id: str = Field(min_length=1)
    previous_point_id: str | None = Field(default=None, min_length=1)
    revision: int = Field(ge=0)
    as_of: datetime
    initial_equity: PositiveDecimal
    actual_equity: Money
    cash_equity: Money
    passive_equity: Money
    passive_cash_balance: Money
    passive_quantity: Decimal = Field(gt=0)
    passive_entry_price: PositiveDecimal
    passive_mark_price: PositiveDecimal
    passive_entry_fee: Money
    actual_drawdown_fraction: UnitInterval
    passive_equity_high_water: PositiveDecimal
    passive_drawdown_fraction: UnitInterval
    actual_increment_vs_cash: Decimal
    actual_increment_vs_passive: Decimal
    baseline_account_snapshot_id: str = Field(min_length=1)
    entry_quote_id: str = Field(min_length=1)
    mark_quote_id: str = Field(min_length=1)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    _utc_as_of = field_validator("as_of")(require_utc)

    @model_validator(mode="after")
    def economics_chain_and_identity_reconcile(self):
        expected_passive = self.passive_cash_balance + (
            self.passive_quantity * self.passive_mark_price
        )
        expected_drawdown = (
            (self.passive_equity_high_water - self.passive_equity)
            / self.passive_equity_high_water
        )
        if self.cash_equity != self.initial_equity:
            raise ValueError("Cash benchmark 必须保持初始权益")
        if self.passive_equity != expected_passive:
            raise ValueError("Passive benchmark 权益无法与现金和持仓核对")
        if self.passive_equity_high_water < self.passive_equity:
            raise ValueError("Passive benchmark 高水位不能低于当前权益")
        if self.passive_drawdown_fraction != expected_drawdown:
            raise ValueError("Passive benchmark 回撤无法与高水位核对")
        if self.actual_increment_vs_cash != self.actual_equity - self.cash_equity:
            raise ValueError("Actual 相对 Cash 增量无法核对")
        if self.actual_increment_vs_passive != self.actual_equity - self.passive_equity:
            raise ValueError("Actual 相对 Passive 增量无法核对")
        if (self.revision == 0) != (self.previous_point_id is None):
            raise ValueError("Capital benchmark revision 与前序点身份不一致")
        expected_id = stable_id(
            "capital_benchmark_point",
            self.policy_id,
            self.account_snapshot_id,
            self.source_hash,
        )
        if self.point_id != expected_id:
            raise ValueError("Capital benchmark point_id 与来源不一致")
        return self


def build_capital_benchmark_policy(config: AppConfig) -> CapitalBenchmarkPolicy | None:
    context = config.capital.context_forecast
    if not config.capital.enabled or context is None or not context.enabled:
        return None
    authorization = next(
        (
            item
            for item in config.capital.candidate_capital_authorizations
            if (
                item.producer_id,
                item.producer_behavior_id,
                item.outcome_family_id,
            )
            == (
                context.producer_id,
                context.producer_behavior_id,
                context.outcome_family_id,
            )
        ),
        None,
    )
    execution = next(
        (
            item
            for item in config.capital.execution_specs
            if item.instrument.key == context.target_instrument_key
        ),
        None,
    )
    if authorization is None or execution is None:
        return None
    return CapitalBenchmarkPolicy.create(
        portfolio_id=config.capital.decision.portfolio_id,
        instrument=execution.instrument,
        allocation_fraction=authorization.maximum_allocation_fraction,
        quantity_step=execution.quantity_step,
        minimum_order_notional=execution.minimum_order_notional,
        fee_bps=execution.fee_bps,
    )


class CapitalBenchmarkProjector:
    def __init__(self, policy: CapitalBenchmarkPolicy) -> None:
        self._policy = policy

    def project(
        self,
        *,
        baseline: PortfolioAccountSnapshot,
        account: PortfolioAccountSnapshot,
        entry_quote: MarketQuote,
        mark_quote: MarketQuote,
        previous: CapitalBenchmarkPoint | None,
    ) -> CapitalBenchmarkPoint:
        if baseline.portfolio_id != self._policy.portfolio_id or (
            account.portfolio_id != self._policy.portfolio_id
        ):
            raise ValueError("Capital benchmark 账户不属于冻结 Portfolio")
        if account.revision == 0 and account != baseline:
            raise ValueError("Capital benchmark revision 0 必须是账户基线")
        if previous is not None and (
            previous.policy_id != self._policy.policy_id
            or previous.revision + 1 != account.revision
        ):
            raise ValueError("Capital benchmark 必须按账户 revision 连续推进")
        for quote, at in ((entry_quote, baseline.as_of), (mark_quote, account.as_of)):
            if quote.symbol != self._policy.instrument.symbol or quote.observed_at > at:
                raise ValueError("Capital benchmark 报价不属于冻结产品或点时不可见")

        gross_budget = baseline.equity * self._policy.allocation_fraction
        quantity = (gross_budget / entry_quote.ask // self._policy.quantity_step) * (
            self._policy.quantity_step
        )
        entry_notional = quantity * entry_quote.ask
        if quantity <= 0 or entry_notional < self._policy.minimum_order_notional:
            raise ValueError("Capital benchmark 被动仓位低于合法订单门槛")
        fee = entry_notional * self._policy.fee_bps / _BPS
        passive_cash = baseline.equity - entry_notional - fee
        passive_equity = passive_cash + quantity * mark_quote.bid
        passive_high_water = max(
            passive_equity,
            previous.passive_equity_high_water if previous is not None else baseline.equity,
        )
        source = {
            "policy": self._policy,
            "baseline_snapshot_id": baseline.snapshot_id,
            "account_snapshot_id": account.snapshot_id,
            "account_snapshot_hash": content_hash(account),
            "entry_quote_id": entry_quote.quote_id,
            "entry_quote_hash": content_hash(entry_quote),
            "mark_quote_id": mark_quote.quote_id,
            "mark_quote_hash": content_hash(mark_quote),
            "previous_point_id": previous.point_id if previous is not None else None,
        }
        source_hash = content_hash(source)
        values = {
            "policy_id": self._policy.policy_id,
            "account_snapshot_id": account.snapshot_id,
            "previous_point_id": previous.point_id if previous is not None else None,
            "revision": account.revision,
            "as_of": account.as_of,
            "initial_equity": baseline.equity,
            "actual_equity": account.equity,
            "cash_equity": baseline.equity,
            "passive_equity": passive_equity,
            "passive_cash_balance": passive_cash,
            "passive_quantity": quantity,
            "passive_entry_price": entry_quote.ask,
            "passive_mark_price": mark_quote.bid,
            "passive_entry_fee": fee,
            "actual_drawdown_fraction": account.drawdown_fraction,
            "passive_equity_high_water": passive_high_water,
            "passive_drawdown_fraction": (
                (passive_high_water - passive_equity) / passive_high_water
            ),
            "actual_increment_vs_cash": account.equity - baseline.equity,
            "actual_increment_vs_passive": account.equity - passive_equity,
            "baseline_account_snapshot_id": baseline.snapshot_id,
            "entry_quote_id": entry_quote.quote_id,
            "mark_quote_id": mark_quote.quote_id,
            "source_hash": source_hash,
        }
        return CapitalBenchmarkPoint(
            point_id=stable_id(
                "capital_benchmark_point",
                self._policy.policy_id,
                account.snapshot_id,
                source_hash,
            ),
            **values,
        )


class SqlCapitalBenchmarkEvaluator:
    """Append evaluation facts; never place orders or mutate the capital account."""

    def __init__(self, engine: Engine, policy: CapitalBenchmarkPolicy) -> None:
        self._engine = engine
        self._policy = policy
        self._market = SqlMarketDataStore(engine)
        self._projector = CapitalBenchmarkProjector(policy)

    def reconcile(self, *, as_of: datetime) -> int:
        as_of = require_utc(as_of)
        baseline = self._baseline(as_of)
        if baseline is None:
            return 0
        entry_quote = self._market.latest_spot_quote(
            instrument=self._policy.instrument,
            evaluation_at=baseline.as_of,
            visible_at=baseline.as_of,
        )
        if entry_quote is None:
            raise ValueError("Capital benchmark 缺少账户基线时点可见报价")
        previous = self.latest()
        start_revision = previous.revision + 1 if previous is not None else 0
        snapshots = self._snapshots(start_revision=start_revision, as_of=as_of)
        written = 0
        for account in snapshots:
            mark_quote = self._market.latest_spot_quote(
                instrument=self._policy.instrument,
                evaluation_at=account.as_of,
                visible_at=account.as_of,
            )
            if mark_quote is None:
                raise ValueError("Capital benchmark 缺少账户快照时点可见报价")
            point = self._projector.project(
                baseline=baseline,
                account=account,
                entry_quote=entry_quote,
                mark_quote=mark_quote,
                previous=previous,
            )
            written += self.record(point)
            previous = point
        return written

    def record(self, point: CapitalBenchmarkPoint) -> int:
        if point.policy_id != self._policy.policy_id:
            raise ValueError("Capital benchmark point 不属于当前冻结 Policy")
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(capital_benchmark_points).values(
                        point_id=point.point_id,
                        policy_id=point.policy_id,
                        account_snapshot_id=point.account_snapshot_id,
                        revision=point.revision,
                        as_of=point.as_of,
                        source_hash=point.source_hash,
                        payload=point.model_dump(mode="json"),
                    )
                )
            return 1
        except IntegrityError:
            existing = self.for_account(point.account_snapshot_id)
            if existing != point:
                raise ValueError("Capital benchmark 已存在且内容不同") from None
            return 0

    def latest(self) -> CapitalBenchmarkPoint | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(capital_benchmark_points.c.payload)
                .where(capital_benchmark_points.c.policy_id == self._policy.policy_id)
                .order_by(capital_benchmark_points.c.revision.desc())
                .limit(1)
            ).scalar_one_or_none()
        return None if payload is None else CapitalBenchmarkPoint.model_validate(payload)

    def for_account(self, snapshot_id: str) -> CapitalBenchmarkPoint | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(capital_benchmark_points.c.payload).where(
                    capital_benchmark_points.c.policy_id == self._policy.policy_id,
                    capital_benchmark_points.c.account_snapshot_id == snapshot_id,
                )
            ).scalar_one_or_none()
        return None if payload is None else CapitalBenchmarkPoint.model_validate(payload)

    def _baseline(self, as_of: datetime) -> PortfolioAccountSnapshot | None:
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(portfolio_account_snapshots.c.payload)
                .where(
                    portfolio_account_snapshots.c.portfolio_id == self._policy.portfolio_id,
                    portfolio_account_snapshots.c.revision == 0,
                    portfolio_account_snapshots.c.as_of <= as_of,
                )
                .limit(1)
            ).scalar_one_or_none()
        return None if payload is None else PortfolioAccountSnapshot.model_validate(payload)

    def _snapshots(
        self,
        *,
        start_revision: int,
        as_of: datetime,
    ) -> tuple[PortfolioAccountSnapshot, ...]:
        with self._engine.connect() as connection:
            payloads = connection.execute(
                select(portfolio_account_snapshots.c.payload)
                .where(
                    portfolio_account_snapshots.c.portfolio_id == self._policy.portfolio_id,
                    portfolio_account_snapshots.c.revision >= start_revision,
                    portfolio_account_snapshots.c.as_of <= as_of,
                )
                .order_by(portfolio_account_snapshots.c.revision)
            ).scalars()
            return tuple(PortfolioAccountSnapshot.model_validate(item) for item in payloads)
