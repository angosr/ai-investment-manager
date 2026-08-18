from __future__ import annotations

from datetime import timedelta
from decimal import ROUND_DOWN

from quant_core.config import RiskPolicy
from quant_core.domain import (
    AccountSnapshot,
    GuardState,
    MarketSnapshot,
    RiskDecision,
    RiskOutcome,
    RiskReservation,
    RuleResult,
    Side,
    TradeIntent,
)
from quant_core.ids import content_hash, stable_id


class RiskEngine:
    def __init__(self, policy: RiskPolicy) -> None:
        self._policy = policy

    def evaluate(
        self,
        *,
        intent: TradeIntent,
        market: MarketSnapshot,
        account: AccountSnapshot,
    ) -> RiskDecision:
        rules = [
            self._boolean_rule(
                "kill-switch",
                not self._policy.kill_switch,
                "KILL_SWITCH_CLEAR",
                "KILL_SWITCH_ACTIVE",
            ),
            self._boolean_rule(
                "symbol-allowlist",
                intent.symbol in self._policy.symbol_allowlist,
                "SYMBOL_ALLOWED",
                "SYMBOL_NOT_ALLOWED",
                observed=intent.symbol,
                limit=",".join(self._policy.symbol_allowlist),
            ),
            self._age_rule(
                "market-freshness",
                market.as_of,
                market.observed_at,
                self._policy.maximum_market_age_seconds,
            ),
            self._age_rule(
                "account-freshness",
                market.as_of,
                account.observed_at,
                self._policy.maximum_account_age_seconds,
            ),
            self._boolean_rule(
                "account-reconciled",
                account.reconciled,
                "ACCOUNT_RECONCILED",
                "ACCOUNT_NOT_RECONCILED",
            ),
            self._boolean_rule(
                "daily-loss",
                account.daily_pnl >= -self._policy.maximum_daily_loss,
                "DAILY_LOSS_WITHIN_LIMIT",
                "DAILY_LOSS_LIMIT_EXCEEDED",
                observed=str(account.daily_pnl),
                limit=str(-self._policy.maximum_daily_loss),
            ),
            self._boolean_rule(
                "drawdown",
                account.drawdown_fraction <= self._policy.maximum_drawdown_fraction,
                "DRAWDOWN_WITHIN_LIMIT",
                "DRAWDOWN_LIMIT_EXCEEDED",
                observed=str(account.drawdown_fraction),
                limit=str(self._policy.maximum_drawdown_fraction),
            ),
        ]

        entry_price = market.ask if intent.side == Side.BUY else market.bid
        stop_valid = (
            intent.stop_price < entry_price
            if intent.side == Side.BUY
            else intent.stop_price > entry_price
        )
        rules.append(
            self._boolean_rule(
                "executable-stop",
                stop_valid,
                "STOP_EXECUTABLE",
                "STOP_NOT_EXECUTABLE",
                observed=str(intent.stop_price),
                limit=str(entry_price),
            )
        )
        if intent.valid_until <= market.as_of:
            rules.append(
                RuleResult(
                    rule_id="intent-validity",
                    rule_version="v1",
                    state=GuardState.FAIL,
                    reason_code="INTENT_EXPIRED",
                )
            )
        else:
            rules.append(
                RuleResult(
                    rule_id="intent-validity",
                    rule_version="v1",
                    state=GuardState.PASS,
                    reason_code="INTENT_VALID",
                )
            )

        intent_digest = content_hash(intent)
        decision_id = stable_id("risk", intent.cycle_id, intent.intent_id, self._policy.version)
        common = {
            "decision_id": decision_id,
            "cycle_id": intent.cycle_id,
            "intent_id": intent.intent_id,
            "policy_version": self._policy.version,
            "intent_hash": intent_digest,
            "account_snapshot_hash": content_hash(account),
            "market_snapshot_hash": content_hash(market),
        }
        if any(rule.state != GuardState.PASS for rule in rules):
            return RiskDecision(
                **common,
                outcome=RiskOutcome.REJECTED,
                rule_results=tuple(rules),
            )

        stop_distance = abs(entry_price - intent.stop_price)
        if stop_distance <= 0:
            rules.append(
                RuleResult(
                    rule_id="position-size",
                    rule_version="v1",
                    state=GuardState.UNKNOWN,
                    reason_code="STOP_DISTANCE_UNKNOWN",
                )
            )
            return RiskDecision(
                **common,
                outcome=RiskOutcome.REJECTED,
                rule_results=tuple(rules),
            )

        risk_budget = account.quote_balance * self._policy.maximum_risk_fraction
        risk_quantity = risk_budget / stop_distance
        notional_quantity = self._policy.maximum_position_notional / entry_price
        quantity = min(risk_quantity, notional_quantity)
        quantity = (quantity / self._policy.quantity_step).to_integral_value(
            rounding=ROUND_DOWN
        ) * self._policy.quantity_step
        notional = quantity * entry_price
        if quantity <= 0 or notional < self._policy.minimum_order_notional:
            rules.append(
                RuleResult(
                    rule_id="position-size",
                    rule_version="v1",
                    state=GuardState.FAIL,
                    reason_code="ORDER_BELOW_MINIMUM",
                    observed=str(notional),
                    limit=str(self._policy.minimum_order_notional),
                )
            )
            return RiskDecision(
                **common,
                outcome=RiskOutcome.REJECTED,
                rule_results=tuple(rules),
            )

        actual_risk = quantity * stop_distance
        rules.append(
            RuleResult(
                rule_id="position-size",
                rule_version="v1",
                state=GuardState.PASS,
                reason_code="POSITION_SIZED",
                observed=str(actual_risk),
                limit=str(risk_budget),
            )
        )
        reservation = RiskReservation(
            reservation_id=stable_id("reserve", intent.cycle_id, intent.intent_id),
            cycle_id=intent.cycle_id,
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            risk_amount=actual_risk,
            quantity=quantity,
            expires_at=market.as_of + timedelta(seconds=self._policy.reservation_ttl_seconds),
        )
        return RiskDecision(
            **common,
            outcome=RiskOutcome.APPROVED,
            rule_results=tuple(rules),
            quantity=quantity,
            reservation=reservation,
        )

    @staticmethod
    def _boolean_rule(
        rule_id: str,
        passed: bool,
        pass_code: str,
        fail_code: str,
        *,
        observed: str | None = None,
        limit: str | None = None,
    ) -> RuleResult:
        return RuleResult(
            rule_id=rule_id,
            rule_version="v1",
            state=GuardState.PASS if passed else GuardState.FAIL,
            reason_code=pass_code if passed else fail_code,
            observed=observed,
            limit=limit,
        )

    @staticmethod
    def _age_rule(
        rule_id: str,
        as_of,
        observed_at,
        maximum_age_seconds: int,
    ) -> RuleResult:
        if observed_at > as_of:
            return RuleResult(
                rule_id=rule_id,
                rule_version="v1",
                state=GuardState.UNKNOWN,
                reason_code="OBSERVATION_FROM_FUTURE",
            )
        age = int((as_of - observed_at).total_seconds())
        return RuleResult(
            rule_id=rule_id,
            rule_version="v1",
            state=GuardState.PASS if age <= maximum_age_seconds else GuardState.FAIL,
            reason_code="DATA_FRESH" if age <= maximum_age_seconds else "DATA_STALE",
            observed=str(age),
            limit=str(maximum_age_seconds),
        )
