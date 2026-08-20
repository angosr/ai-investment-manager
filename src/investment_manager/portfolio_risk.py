from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import Field, field_validator, model_validator

from investment_manager.asset_management import PortfolioTarget
from investment_manager.domain import (
    AccountSnapshot,
    FrozenModel,
    GuardState,
    MarketSnapshot,
    Money,
    PositiveDecimal,
    RiskOutcome,
    RuleResult,
    UnitInterval,
    _require_utc,
)
from investment_manager.ids import content_hash, stable_id


class PortfolioRiskPolicy(FrozenModel):
    version: str = Field(min_length=1)
    symbol_allowlist: tuple[str, ...] = Field(min_length=1)
    maximum_market_age_seconds: int = Field(gt=0)
    maximum_account_age_seconds: int = Field(gt=0)
    maximum_daily_loss: Money
    maximum_drawdown_fraction: UnitInterval
    maximum_risk_fraction: UnitInterval
    maximum_total_exposure_fraction: UnitInterval
    maximum_position_notional: PositiveDecimal
    maximum_spread_bps: Money
    kill_switch: bool = False

    @model_validator(mode="after")
    def policy_scope_must_be_unique_and_sorted(self):
        if tuple(sorted(set(self.symbol_allowlist))) != self.symbol_allowlist:
            raise ValueError("Risk symbol_allowlist 必须唯一且排序")
        if self.maximum_risk_fraction <= 0:
            raise ValueError("maximum_risk_fraction 必须为正数")
        return self


class ProtectiveStop(FrozenModel):
    symbol: str = Field(min_length=1)
    stop_price: PositiveDecimal


class ApprovedAssetTarget(FrozenModel):
    symbol: str = Field(min_length=1)
    requested_quote_notional: Money
    approved_quote_notional: Money
    protective_stop_price: PositiveDecimal | None = None
    reason_codes: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def risk_may_only_reduce_requested_exposure(self):
        if self.approved_quote_notional > self.requested_quote_notional:
            raise ValueError("Risk 不得增加 PortfolioTarget 暴露")
        if tuple(sorted(set(self.reason_codes))) != self.reason_codes:
            raise ValueError("ApprovedAssetTarget reason_codes 必须唯一且排序")
        return self


class ApprovedTarget(FrozenModel):
    approved_target_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    cycle_id: str = Field(min_length=1)
    portfolio_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    as_of: datetime
    valid_until: datetime
    reference_equity: Money
    target_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    account_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    market_snapshot_hashes: tuple[str, ...]
    targets: tuple[ApprovedAssetTarget, ...] = ()

    _utc_as_of = field_validator("as_of")(_require_utc)
    _utc_valid_until = field_validator("valid_until")(_require_utc)

    @model_validator(mode="after")
    def approved_target_must_be_bounded_and_sorted(self):
        if self.as_of >= self.valid_until:
            raise ValueError("ApprovedTarget 必须具有未来有效期")
        symbols = tuple(item.symbol for item in self.targets)
        if tuple(sorted(set(symbols))) != symbols:
            raise ValueError("ApprovedTarget 资产必须唯一且排序")
        if sum(item.approved_quote_notional for item in self.targets) > (
            self.reference_equity
        ):
            raise ValueError("ApprovedTarget 不得超过参考权益")
        if tuple(sorted(set(self.market_snapshot_hashes))) != (
            self.market_snapshot_hashes
        ):
            raise ValueError("market_snapshot_hashes 必须唯一且排序")
        return self


class PortfolioRiskDecision(FrozenModel):
    decision_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    decided_at: datetime
    outcome: RiskOutcome
    rule_results: tuple[RuleResult, ...] = Field(min_length=1)
    approved_target: ApprovedTarget | None = None

    _utc_decided_at = field_validator("decided_at")(_require_utc)

    @model_validator(mode="after")
    def outcome_must_match_approved_target(self):
        if (self.approved_target is not None) != (
            self.outcome == RiskOutcome.APPROVED
        ):
            raise ValueError("Risk outcome 与 ApprovedTarget 不一致")
        if (
            self.approved_target is not None
            and self.approved_target.target_id != self.target_id
        ):
            raise ValueError("RiskDecision 与 ApprovedTarget target_id 不一致")
        return self


class PortfolioRiskEngine:
    """Clamp-only portfolio risk authorization for the new target pipeline."""

    def __init__(self, policy: PortfolioRiskPolicy) -> None:
        self._policy = policy

    def evaluate(
        self,
        *,
        target: PortfolioTarget,
        account: AccountSnapshot,
        markets: tuple[MarketSnapshot, ...],
        protective_stops: tuple[ProtectiveStop, ...],
        as_of: datetime,
    ) -> PortfolioRiskDecision:
        as_of = _require_utc(as_of)
        if len(target.targets) > 1:
            raise ValueError("单 Sleeve MVP 不接受多个 PortfolioTarget 资产")
        market_by_symbol = self._unique_markets(markets)
        stop_by_symbol = self._unique_stops(protective_stops)
        rules = self._global_rules(
            target=target,
            account=account,
            as_of=as_of,
        )
        if target.valid_until <= as_of:
            return self._decision(
                target=target,
                as_of=as_of,
                rules=rules,
                approved=None,
            )

        force_cash = self._policy.kill_switch or account.kill_switch_active or (
            account.daily_pnl < -self._policy.maximum_daily_loss
            or account.drawdown_fraction
            > self._policy.maximum_drawdown_fraction
        )
        approved_assets: list[ApprovedAssetTarget] = []
        market_hashes: list[str] = []
        equity = min(
            target.reference_equity,
            account.equity if account.equity is not None else target.reference_equity,
        )
        position_by_symbol = {item.symbol: item for item in account.positions}
        requested_by_symbol = {item.symbol: item for item in target.targets}
        decision_symbols = tuple(
            sorted(set(requested_by_symbol).union(position_by_symbol))
        )
        for symbol in decision_symbols:
            requested = requested_by_symbol.get(symbol)
            requested_notional = (
                requested.desired_quote_notional
                if requested is not None
                else Decimal("0")
            )
            market = market_by_symbol.get(symbol)
            stop = stop_by_symbol.get(symbol)
            if market is not None:
                market_hashes.append(content_hash(market))
            position = position_by_symbol.get(symbol)
            current_notional = (
                max(Decimal("0"), position.quantity) * market.bid
                if position is not None and market is not None
                else Decimal("0")
            )
            approved_notional, reason_codes, asset_rules = self._clamp_asset(
                symbol=symbol,
                requested_notional=requested_notional,
                current_notional=current_notional,
                market=market,
                stop=stop,
                account=account,
                equity=equity,
                as_of=as_of,
                force_cash=force_cash,
            )
            rules.extend(asset_rules)
            approved_assets.append(
                ApprovedAssetTarget(
                    symbol=symbol,
                    requested_quote_notional=requested_notional,
                    approved_quote_notional=approved_notional,
                    protective_stop_price=(stop.stop_price if stop is not None else None),
                    reason_codes=tuple(sorted(reason_codes)),
                )
            )

        approved = self._approved_target(
            target=target,
            account=account,
            as_of=as_of,
            equity=equity,
            market_hashes=tuple(sorted(market_hashes)),
            assets=tuple(approved_assets),
        )
        return self._decision(
            target=target,
            as_of=as_of,
            rules=rules,
            approved=approved,
        )

    def _clamp_asset(
        self,
        *,
        symbol: str,
        requested_notional: Decimal,
        current_notional: Decimal,
        market: MarketSnapshot | None,
        stop: ProtectiveStop | None,
        account: AccountSnapshot,
        equity: Decimal,
        as_of: datetime,
        force_cash: bool,
    ) -> tuple[Decimal, set[str], list[RuleResult]]:
        if force_cash:
            return Decimal("0"), {"ACCOUNT_RISK_FORCED_CASH"}, []
        if requested_notional <= current_notional:
            return requested_notional, {"RISK_REDUCTION_ALLOWED"}, []

        rules: list[RuleResult] = []
        increase_allowed = True
        checks = (
            (
                "symbol-allowlist",
                symbol in self._policy.symbol_allowlist,
                "SYMBOL_ALLOWED",
                "SYMBOL_NOT_ALLOWED",
            ),
            (
                "account-reconciled",
                account.reconciled,
                "ACCOUNT_RECONCILED",
                "ACCOUNT_NOT_RECONCILED",
            ),
            (
                "account-freshness",
                self._fresh(account.observed_at, as_of, self._policy.maximum_account_age_seconds),
                "ACCOUNT_FRESH",
                "ACCOUNT_STALE_OR_FUTURE",
            ),
            (
                "market-present",
                market is not None,
                "MARKET_PRESENT",
                "MARKET_MISSING",
            ),
        )
        for rule_id, passed, pass_code, fail_code in checks:
            rules.append(self._rule(rule_id, passed, pass_code, fail_code))
            increase_allowed &= passed
        if market is not None:
            spread_bps = (market.ask - market.bid) / market.last * Decimal("10000")
            market_fresh = self._fresh(
                market.observed_at,
                as_of,
                self._policy.maximum_market_age_seconds,
            )
            spread_safe = spread_bps <= self._policy.maximum_spread_bps
            rules.extend(
                (
                    self._rule(
                        "market-freshness",
                        market_fresh,
                        "MARKET_FRESH",
                        "MARKET_STALE_OR_FUTURE",
                    ),
                    self._rule(
                        "spread",
                        spread_safe,
                        "SPREAD_WITHIN_LIMIT",
                        "SPREAD_LIMIT_EXCEEDED",
                    ),
                )
            )
            increase_allowed &= market_fresh and spread_safe
        stop_valid = bool(
            stop is not None
            and market is not None
            and stop.stop_price < market.ask
        )
        rules.append(
            self._rule(
                "protective-stop",
                stop_valid,
                "PROTECTIVE_STOP_VALID",
                "PROTECTIVE_STOP_INVALID",
            )
        )
        increase_allowed &= stop_valid
        if not increase_allowed or market is None or stop is None:
            return current_notional, {"NEW_RISK_CLAMPED_TO_CURRENT"}, rules

        stop_fraction = (market.ask - stop.stop_price) / market.ask
        risk_cap = equity * self._policy.maximum_risk_fraction / stop_fraction
        approved = min(
            requested_notional,
            self._policy.maximum_position_notional,
            equity * self._policy.maximum_total_exposure_fraction,
            risk_cap,
        )
        reasons = (
            {"TARGET_WITHIN_RISK_ENVELOPE"}
            if approved == requested_notional
            else {"TARGET_CLAMPED_TO_RISK_ENVELOPE"}
        )
        return approved, reasons, rules

    def _global_rules(
        self,
        *,
        target: PortfolioTarget,
        account: AccountSnapshot,
        as_of: datetime,
    ) -> list[RuleResult]:
        return [
            self._rule(
                "target-validity",
                target.valid_until > as_of,
                "TARGET_VALID",
                "TARGET_EXPIRED",
            ),
            self._rule(
                "kill-switch",
                not (self._policy.kill_switch or account.kill_switch_active),
                "KILL_SWITCH_CLEAR",
                "KILL_SWITCH_ACTIVE",
            ),
            self._rule(
                "daily-loss",
                account.daily_pnl >= -self._policy.maximum_daily_loss,
                "DAILY_LOSS_WITHIN_LIMIT",
                "DAILY_LOSS_LIMIT_EXCEEDED",
            ),
            self._rule(
                "drawdown",
                account.drawdown_fraction
                <= self._policy.maximum_drawdown_fraction,
                "DRAWDOWN_WITHIN_LIMIT",
                "DRAWDOWN_LIMIT_EXCEEDED",
            ),
        ]

    def _approved_target(
        self,
        *,
        target: PortfolioTarget,
        account: AccountSnapshot,
        as_of: datetime,
        equity: Decimal,
        market_hashes: tuple[str, ...],
        assets: tuple[ApprovedAssetTarget, ...],
    ) -> ApprovedTarget:
        values = {
            "target_id": target.target_id,
            "cycle_id": target.cycle_id,
            "portfolio_id": target.portfolio_id,
            "policy_version": self._policy.version,
            "as_of": as_of,
            "valid_until": target.valid_until,
            "reference_equity": equity,
            "target_hash": content_hash(target),
            "account_snapshot_hash": content_hash(account),
            "market_snapshot_hashes": market_hashes,
            "targets": assets,
        }
        return ApprovedTarget(
            approved_target_id=stable_id(
                "approved_target",
                target.target_id,
                self._policy.version,
                content_hash(values),
            ),
            **values,
        )

    def _decision(
        self,
        *,
        target: PortfolioTarget,
        as_of: datetime,
        rules: list[RuleResult],
        approved: ApprovedTarget | None,
    ) -> PortfolioRiskDecision:
        outcome = (
            RiskOutcome.APPROVED if approved is not None else RiskOutcome.REJECTED
        )
        return PortfolioRiskDecision(
            decision_id=stable_id(
                "portfolio_risk_decision",
                target.target_id,
                self._policy.version,
                as_of.isoformat(),
                content_hash(rules),
            ),
            target_id=target.target_id,
            policy_version=self._policy.version,
            decided_at=as_of,
            outcome=outcome,
            rule_results=tuple(rules),
            approved_target=approved,
        )

    @staticmethod
    def _unique_markets(
        markets: tuple[MarketSnapshot, ...],
    ) -> dict[str, MarketSnapshot]:
        values = {item.symbol: item for item in markets}
        if len(values) != len(markets):
            raise ValueError("MarketSnapshot symbol 必须唯一")
        return values

    @staticmethod
    def _unique_stops(stops: tuple[ProtectiveStop, ...]) -> dict[str, ProtectiveStop]:
        values = {item.symbol: item for item in stops}
        if len(values) != len(stops):
            raise ValueError("ProtectiveStop symbol 必须唯一")
        return values

    @staticmethod
    def _fresh(observed_at: datetime, as_of: datetime, maximum_age: int) -> bool:
        return observed_at <= as_of and (as_of - observed_at).total_seconds() <= maximum_age

    @staticmethod
    def _rule(
        rule_id: str,
        passed: bool,
        pass_code: str,
        fail_code: str,
    ) -> RuleResult:
        return RuleResult(
            rule_id=rule_id,
            rule_version="portfolio-risk-v1",
            state=GuardState.PASS if passed else GuardState.FAIL,
            reason_code=pass_code if passed else fail_code,
        )
