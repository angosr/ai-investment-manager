"""RiskEngine 不变量测试：用真实回放周期产出的对象验证，不手搓易碎 fixture。"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine

from investment_manager.execution.models import (
    SUPPORTED_OPEN_SIDES,
    Side,
)
from investment_manager.legacy.cycle import AnalysisCycle
from investment_manager.legacy.exchange import MockExchange
from investment_manager.legacy.models import CycleOutcome
from investment_manager.legacy.repository import SqlFactLedger
from investment_manager.legacy.risk import RiskEngine
from investment_manager.legacy.shadow import SqlShadowStateReader
from investment_manager.risk.budget import SqlRiskBudgetStore
from investment_manager.risk.models import (
    GuardState,
    RiskOutcome,
)
from investment_manager.schema import create_offline_schema


def _fresh_cycle(app_config):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_offline_schema(engine)
    ledger = SqlFactLedger(engine)
    cycle = AnalysisCycle.with_adapters(
        app_config,
        ledger=ledger,
        exchange=MockExchange(app_config.execution),
        risk_budget=SqlRiskBudgetStore(engine),
    )
    return engine, ledger, cycle


def _replay_facts(app_config, replay_input):
    _engine, ledger, cycle = _fresh_cycle(app_config)
    return ledger.get(cycle.run(replay_input).cycle_id)


def test_short_entry_is_rejected_before_reserving_budget(app_config, replay_input) -> None:
    facts = _replay_facts(app_config, replay_input)
    assert facts.intent is not None and facts.intent.side == Side.BUY  # 回放前提：做多

    # 把同一意图翻成开空：现货 MVP 不支持，必须在占用预算前拒绝
    short_intent = facts.intent.model_copy(update={"side": Side.SELL})
    decision = RiskEngine(app_config.risk).evaluate(
        intent=short_intent,
        market=facts.panel.market,
        account=facts.panel.account,
    )

    assert decision.outcome == RiskOutcome.REJECTED
    assert decision.reservation is None  # 关键：没有占用组合风险预算 → 无泄漏
    assert decision.quantity is None
    long_only = next(rule for rule in decision.rule_results if rule.rule_id == "long-only")
    assert long_only.state == GuardState.FAIL
    assert long_only.reason_code == "SHORT_ENTRY_NOT_SUPPORTED"
    assert long_only.limit == ",".join(side.value for side in SUPPORTED_OPEN_SIDES)


def test_long_entry_still_passes_long_only_rule(app_config, replay_input) -> None:
    facts = _replay_facts(app_config, replay_input)
    decision = RiskEngine(app_config.risk).evaluate(
        intent=facts.intent,
        market=facts.panel.market,
        account=facts.panel.account,
    )
    long_only = next(rule for rule in decision.rule_results if rule.rule_id == "long-only")
    assert long_only.state == GuardState.PASS


def test_last_entry_order_at_is_per_symbol_and_only_counts_orders(app_config, replay_input) -> None:
    engine, _ledger, cycle = _fresh_cycle(app_config)
    result = cycle.run(replay_input)  # EXECUTED → 该品种有一笔 ENTRY 订单
    reader = SqlShadowStateReader(engine, maximum_reconciliation_age_seconds=180)
    later = replay_input.market.as_of + timedelta(minutes=1)

    assert reader.last_entry_order_at(symbol="BTCUSDT", as_of=later) == replay_input.market.as_of
    assert result.outcome == CycleOutcome.EXECUTED
    # 未下单的品种没有冷却起点
    assert reader.last_entry_order_at(symbol="ETHUSDT", as_of=later) is None


def test_symbol_cooldown_blocks_reorder_within_window(app_config, replay_input) -> None:
    # 把"该品种刚下过单"作为显式输入喂给同一周期：冷却门禁应拦住，不再下单
    _engine, _ledger, cycle = _fresh_cycle(app_config)
    cooled = replay_input.model_copy(
        update={"frequency_last_entry_order_at": replay_input.market.as_of}
    )
    result = cycle.run(cooled)
    assert result.outcome == CycleOutcome.NO_TRADE
    assert result.reason_code == "SYMBOL_COOLDOWN_ACTIVE"


def test_persisted_account_kill_switch_rejects_new_risk(app_config, replay_input) -> None:
    facts = _replay_facts(app_config, replay_input)
    decision = RiskEngine(app_config.risk).evaluate(
        intent=facts.intent,
        market=facts.panel.market,
        account=facts.panel.account.model_copy(update={"kill_switch_active": True}),
    )

    kill_switch = next(rule for rule in decision.rule_results if rule.rule_id == "kill-switch")
    assert decision.outcome == RiskOutcome.REJECTED
    assert decision.reservation is None
    assert kill_switch.state == GuardState.FAIL
    assert kill_switch.reason_code == "KILL_SWITCH_ACTIVE"
