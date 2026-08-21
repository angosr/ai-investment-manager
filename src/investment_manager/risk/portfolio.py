from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from pydantic import Field, field_validator, model_validator

from investment_manager.forecast.models import ExposureDirection, ForecastTarget
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import (
    FrozenModel,
    Money,
    UnitInterval,
)
from investment_manager.market.models import (
    ExecutableQuote,
    InstrumentProduct,
)
from investment_manager.portfolio.models import (
    PortfolioAccountSnapshot,
    PortfolioTarget,
    SleeveTarget,
    sleeve_gross_notional,
)
from investment_manager.risk.models import GuardState, RiskOutcome, RuleResult

_BPS = Decimal("10000")


class PortfolioRiskPolicy(FrozenModel):
    version: str = Field(min_length=1)
    instrument_allowlist: tuple[str, ...] = Field(min_length=1)
    maximum_quote_age_seconds: int = Field(gt=0)
    maximum_account_age_seconds: int = Field(gt=0)
    maximum_daily_loss: Money
    maximum_drawdown_fraction: UnitInterval
    maximum_gross_exposure_fraction: UnitInterval
    maximum_net_delta_fraction: UnitInterval
    maximum_instrument_fraction: UnitInterval
    maximum_margin_fraction: UnitInterval
    maximum_stress_loss_fraction: UnitInterval
    maximum_spread_bps: Money
    maximum_unhedged_fraction: UnitInterval
    maximum_unhedged_seconds: int = Field(gt=0)
    kill_switch: bool = False

    @model_validator(mode="after")
    def policy_scope_must_be_unique_and_sorted(self):
        if tuple(sorted(set(self.instrument_allowlist))) != self.instrument_allowlist:
            raise ValueError("Risk instrument_allowlist 必须唯一且排序")
        positive_limits = (
            self.maximum_gross_exposure_fraction,
            self.maximum_net_delta_fraction,
            self.maximum_instrument_fraction,
            self.maximum_margin_fraction,
            self.maximum_stress_loss_fraction,
            self.maximum_unhedged_fraction,
        )
        if any(item <= 0 for item in positive_limits):
            raise ValueError("Risk 暴露、保证金、压力和失配上限必须为正数")
        return self


class SleeveRiskProfile(FrozenModel):
    sleeve_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    basis_stress_bps: Money
    funding_stress_bps: Money
    execution_stress_bps: Money
    derivative_initial_margin_fraction: UnitInterval = Decimal("1")

    @property
    def total_stress_bps(self) -> Decimal:
        return (
            self.basis_stress_bps
            + self.funding_stress_bps
            + self.execution_stress_bps
        )

    @model_validator(mode="after")
    def profile_must_define_nonzero_stress(self):
        if self.total_stress_bps <= 0:
            raise ValueError("SleeveRiskProfile 必须定义正的压力损失")
        if self.derivative_initial_margin_fraction <= 0:
            raise ValueError("衍生品初始保证金比例必须为正数")
        return self


class ApprovedSleeve(FrozenModel):
    sleeve_id: str = Field(min_length=1)
    forecast_family: str = Field(min_length=1)
    forecast_target: ForecastTarget
    requested_gross_notional: Money
    approved_gross_notional: Money
    sleeve_scale: UnitInterval
    risk_profile_version: str = Field(min_length=1)
    maximum_unhedged_notional: Money
    maximum_unhedged_seconds: int = Field(gt=0)
    reason_codes: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def risk_may_only_reduce_whole_sleeve(self):
        if self.approved_gross_notional > self.requested_gross_notional:
            raise ValueError("Risk 不得增加 PortfolioTarget Sleeve 暴露")
        expected_scale = (
            self.approved_gross_notional / self.requested_gross_notional
            if self.requested_gross_notional > 0
            else Decimal("0")
        )
        if self.sleeve_scale != expected_scale:
            raise ValueError("ApprovedSleeve scale 必须等于批准/请求 gross notional")
        if self.maximum_unhedged_notional > self.approved_gross_notional:
            raise ValueError("未对冲名义上限不能超过批准 Sleeve gross notional")
        if tuple(sorted(set(self.reason_codes))) != self.reason_codes:
            raise ValueError("ApprovedSleeve reason_codes 必须唯一且排序")
        return self


class ApprovedPortfolioTarget(FrozenModel):
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
    quote_hashes: tuple[str, ...]
    risk_profile_hashes: tuple[str, ...]
    sleeves: tuple[ApprovedSleeve, ...] = ()

    _utc_as_of = field_validator("as_of")(require_utc)
    _utc_valid_until = field_validator("valid_until")(require_utc)

    @model_validator(mode="after")
    def approved_target_must_be_bounded_and_sorted(self):
        if self.as_of >= self.valid_until:
            raise ValueError("ApprovedPortfolioTarget 必须具有未来有效期")
        sleeve_ids = tuple(item.sleeve_id for item in self.sleeves)
        if tuple(sorted(set(sleeve_ids))) != sleeve_ids:
            raise ValueError("ApprovedPortfolioTarget Sleeves 必须唯一且排序")
        if sum(item.approved_gross_notional for item in self.sleeves) > (
            self.reference_equity
        ):
            raise ValueError("ApprovedPortfolioTarget 不得引入隐含杠杆")
        for values, label in (
            (self.quote_hashes, "quote_hashes"),
            (self.risk_profile_hashes, "risk_profile_hashes"),
        ):
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"{label} 必须唯一且排序")
        return self


class PortfolioRiskDecision(FrozenModel):
    decision_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    decided_at: datetime
    outcome: RiskOutcome
    rule_results: tuple[RuleResult, ...] = Field(min_length=1)
    approved_target: ApprovedPortfolioTarget | None = None

    _utc_decided_at = field_validator("decided_at")(require_utc)

    @model_validator(mode="after")
    def outcome_must_match_approved_target(self):
        if (self.approved_target is not None) != (
            self.outcome == RiskOutcome.APPROVED
        ):
            raise ValueError("Risk outcome 与 ApprovedPortfolioTarget 不一致")
        if (
            self.approved_target is not None
            and self.approved_target.target_id != self.target_id
        ):
            raise ValueError("RiskDecision 与 ApprovedPortfolioTarget target_id 不一致")
        return self


class PortfolioRiskEngine:
    """Atomically clamp whole Sleeves; never approve individual opportunity legs."""

    def __init__(self, policy: PortfolioRiskPolicy) -> None:
        self._policy = policy

    def evaluate(
        self,
        *,
        target: PortfolioTarget,
        account: PortfolioAccountSnapshot,
        quotes: tuple[ExecutableQuote, ...],
        risk_profiles: tuple[SleeveRiskProfile, ...],
        as_of: datetime,
    ) -> PortfolioRiskDecision:
        as_of = require_utc(as_of)
        quote_by_instrument = self._unique_quotes(quotes)
        profile_by_sleeve = self._unique_profiles(risk_profiles)
        target_by_sleeve = {item.sleeve_id: item for item in target.sleeves}
        account_by_sleeve = {item.sleeve_id: item for item in account.sleeves}
        missing_exits = set(account_by_sleeve) - set(target_by_sleeve)
        if missing_exits:
            raise ValueError("PortfolioTarget 必须显式包含全部当前 Sleeve 的零目标")
        required_quote_keys = {
            leg.instrument.key
            for sleeve in target.sleeves
            for leg in sleeve.forecast_target.legs
        }
        if set(quote_by_instrument) != required_quote_keys:
            raise ValueError("Risk quotes 必须精确覆盖 PortfolioTarget Instruments")
        if set(profile_by_sleeve) != set(target_by_sleeve):
            raise ValueError("Risk profiles 必须精确覆盖 PortfolioTarget Sleeves")
        rules = self._global_rules(target=target, account=account, as_of=as_of)
        if target.valid_until <= as_of:
            return self._decision(target=target, as_of=as_of, rules=rules, approved=None)

        equity = min(target.reference_equity, account.equity)
        force_cash = self._policy.kill_switch or account.kill_switch_active or (
            account.daily_pnl < -self._policy.maximum_daily_loss
            or account.drawdown_fraction > self._policy.maximum_drawdown_fraction
        )
        requested_after_gates: dict[str, Decimal] = {}
        reasons: dict[str, set[str]] = {}
        for sleeve in target.sleeves:
            profile = profile_by_sleeve.get(sleeve.sleeve_id)
            if profile is None:
                raise ValueError("每个 PortfolioTarget Sleeve 必须具有风险 Profile")
            current = sleeve_gross_notional(
                account_by_sleeve.get(sleeve.sleeve_id),
                quote_by_instrument=quote_by_instrument,
            )
            requested = sleeve.desired_gross_notional
            if force_cash:
                requested_after_gates[sleeve.sleeve_id] = Decimal("0")
                reasons[sleeve.sleeve_id] = {"ACCOUNT_RISK_FORCED_CASH"}
                continue
            if requested <= current:
                requested_after_gates[sleeve.sleeve_id] = requested
                reasons[sleeve.sleeve_id] = {"RISK_REDUCTION_ALLOWED"}
                continue
            allowed, sleeve_rules = self._new_risk_allowed(
                sleeve=sleeve,
                account=account,
                quote_by_instrument=quote_by_instrument,
                as_of=as_of,
            )
            rules.extend(sleeve_rules)
            requested_after_gates[sleeve.sleeve_id] = requested if allowed else current
            reasons[sleeve.sleeve_id] = {
                "NEW_RISK_ELIGIBLE" if allowed else "NEW_RISK_CLAMPED_TO_CURRENT"
            }

        global_scale = self._global_scale(
            target_by_sleeve=target_by_sleeve,
            requested_by_sleeve=requested_after_gates,
            profile_by_sleeve=profile_by_sleeve,
            equity=equity,
        )
        approved_sleeves: list[ApprovedSleeve] = []
        for sleeve in target.sleeves:
            requested = sleeve.desired_gross_notional
            gated = requested_after_gates[sleeve.sleeve_id]
            approved = min(requested, gated * global_scale)
            reason_codes = reasons[sleeve.sleeve_id]
            reason_codes.add(
                "TARGET_WITHIN_RISK_ENVELOPE"
                if approved == requested
                else "TARGET_CLAMPED_TO_RISK_ENVELOPE"
            )
            approved_sleeves.append(
                ApprovedSleeve(
                    sleeve_id=sleeve.sleeve_id,
                    forecast_family=sleeve.forecast_family,
                    forecast_target=sleeve.forecast_target,
                    requested_gross_notional=requested,
                    approved_gross_notional=approved,
                    sleeve_scale=(
                        approved / requested if requested > 0 else Decimal("0")
                    ),
                    risk_profile_version=profile_by_sleeve[sleeve.sleeve_id].version,
                    maximum_unhedged_notional=min(
                        approved,
                        equity * self._policy.maximum_unhedged_fraction,
                    ),
                    maximum_unhedged_seconds=self._policy.maximum_unhedged_seconds,
                    reason_codes=tuple(sorted(reason_codes)),
                )
            )

        approved_target = self._approved_target(
            target=target,
            account=account,
            as_of=as_of,
            equity=equity,
            quotes=quotes,
            profiles=risk_profiles,
            sleeves=tuple(approved_sleeves),
        )
        return self._decision(
            target=target,
            as_of=as_of,
            rules=rules,
            approved=approved_target,
        )

    def _new_risk_allowed(
        self,
        *,
        sleeve: SleeveTarget,
        account: PortfolioAccountSnapshot,
        quote_by_instrument: dict[str, ExecutableQuote],
        as_of: datetime,
    ) -> tuple[bool, list[RuleResult]]:
        checks: list[tuple[str, bool, str, str]] = [
            (
                "account-reconciled",
                account.reconciled and not account.pending_execution_group_ids,
                "ACCOUNT_RECONCILED",
                "ACCOUNT_UNRECONCILED_OR_EXECUTION_PENDING",
            ),
            (
                "account-freshness",
                self._fresh(
                    account.observed_at,
                    as_of,
                    self._policy.maximum_account_age_seconds,
                ),
                "ACCOUNT_FRESH",
                "ACCOUNT_STALE_OR_FUTURE",
            ),
        ]
        for leg in sleeve.forecast_target.legs:
            quote = quote_by_instrument.get(leg.instrument.key)
            prefix = leg.instrument.key
            checks.extend(
                (
                    (
                        f"instrument-allowlist:{prefix}",
                        leg.instrument.key in self._policy.instrument_allowlist,
                        "INSTRUMENT_ALLOWED",
                        "INSTRUMENT_NOT_ALLOWED",
                    ),
                    (
                        f"quote-present:{prefix}",
                        quote is not None,
                        "QUOTE_PRESENT",
                        "QUOTE_MISSING",
                    ),
                )
            )
            if quote is not None:
                spread_bps = (quote.ask - quote.bid) / quote.bid * _BPS
                checks.extend(
                    (
                        (
                            f"quote-freshness:{prefix}",
                            self._fresh(
                                quote.observed_at,
                                as_of,
                                self._policy.maximum_quote_age_seconds,
                            ),
                            "QUOTE_FRESH",
                            "QUOTE_STALE_OR_FUTURE",
                        ),
                        (
                            f"spread:{prefix}",
                            spread_bps <= self._policy.maximum_spread_bps,
                            "SPREAD_WITHIN_LIMIT",
                            "SPREAD_LIMIT_EXCEEDED",
                        ),
                    )
                )
        rules = [
            self._rule(rule_id, passed, pass_code, fail_code)
            for rule_id, passed, pass_code, fail_code in checks
        ]
        return all(item[1] for item in checks), rules

    def _global_scale(
        self,
        *,
        target_by_sleeve: dict[str, SleeveTarget],
        requested_by_sleeve: dict[str, Decimal],
        profile_by_sleeve: dict[str, SleeveRiskProfile],
        equity: Decimal,
    ) -> Decimal:
        if equity <= 0:
            return Decimal("0")
        gross = sum(requested_by_sleeve.values(), Decimal("0"))
        net_by_asset: defaultdict[str, Decimal] = defaultdict(Decimal)
        instrument_gross: defaultdict[str, Decimal] = defaultdict(Decimal)
        margin = Decimal("0")
        stress_loss = Decimal("0")
        for sleeve_id, requested in requested_by_sleeve.items():
            sleeve = target_by_sleeve[sleeve_id]
            profile = profile_by_sleeve[sleeve_id]
            stress_loss += requested * profile.total_stress_bps / _BPS
            for leg in sleeve.forecast_target.legs:
                leg_notional = requested * leg.gross_weight
                sign = (
                    Decimal("1")
                    if leg.direction == ExposureDirection.LONG
                    else Decimal("-1")
                )
                net_by_asset[leg.instrument.base_asset] += sign * leg_notional
                instrument_gross[leg.instrument.key] += leg_notional
                margin += leg_notional * (
                    Decimal("1")
                    if leg.instrument.product == InstrumentProduct.SPOT
                    else profile.derivative_initial_margin_fraction
                )
        limits = (
            self._limit_scale(
                gross,
                equity * self._policy.maximum_gross_exposure_fraction,
            ),
            self._limit_scale(
                sum((abs(item) for item in net_by_asset.values()), Decimal("0")),
                equity * self._policy.maximum_net_delta_fraction,
            ),
            self._limit_scale(
                max(instrument_gross.values(), default=Decimal("0")),
                equity * self._policy.maximum_instrument_fraction,
            ),
            self._limit_scale(
                margin,
                equity * self._policy.maximum_margin_fraction,
            ),
            self._limit_scale(
                stress_loss,
                equity * self._policy.maximum_stress_loss_fraction,
            ),
        )
        return min(limits)

    @staticmethod
    def _limit_scale(observed: Decimal, limit: Decimal) -> Decimal:
        if observed <= 0:
            return Decimal("1")
        return min(Decimal("1"), max(Decimal("0"), limit / observed))

    def _global_rules(
        self,
        *,
        target: PortfolioTarget,
        account: PortfolioAccountSnapshot,
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
                account.drawdown_fraction <= self._policy.maximum_drawdown_fraction,
                "DRAWDOWN_WITHIN_LIMIT",
                "DRAWDOWN_LIMIT_EXCEEDED",
            ),
        ]

    def _approved_target(
        self,
        *,
        target: PortfolioTarget,
        account: PortfolioAccountSnapshot,
        as_of: datetime,
        equity: Decimal,
        quotes: tuple[ExecutableQuote, ...],
        profiles: tuple[SleeveRiskProfile, ...],
        sleeves: tuple[ApprovedSleeve, ...],
    ) -> ApprovedPortfolioTarget:
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
            "quote_hashes": tuple(sorted(content_hash(item) for item in quotes)),
            "risk_profile_hashes": tuple(
                sorted(content_hash(item) for item in profiles)
            ),
            "sleeves": sleeves,
        }
        return ApprovedPortfolioTarget(
            approved_target_id=stable_id(
                "approved_portfolio_target",
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
        approved: ApprovedPortfolioTarget | None,
    ) -> PortfolioRiskDecision:
        outcome = RiskOutcome.APPROVED if approved is not None else RiskOutcome.REJECTED
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
    def _unique_quotes(
        quotes: tuple[ExecutableQuote, ...],
    ) -> dict[str, ExecutableQuote]:
        keys = tuple(item.instrument.key for item in quotes)
        if tuple(sorted(set(keys))) != keys:
            raise ValueError("ExecutableQuote 必须按 Instrument 唯一且排序")
        return {item.instrument.key: item for item in quotes}

    @staticmethod
    def _unique_profiles(
        profiles: tuple[SleeveRiskProfile, ...],
    ) -> dict[str, SleeveRiskProfile]:
        keys = tuple(item.sleeve_id for item in profiles)
        if tuple(sorted(set(keys))) != keys:
            raise ValueError("SleeveRiskProfile 必须按 sleeve_id 唯一且排序")
        return {item.sleeve_id: item for item in profiles}

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
            rule_version="portfolio-risk-v2",
            state=GuardState.PASS if passed else GuardState.FAIL,
            reason_code=pass_code if passed else fail_code,
        )
