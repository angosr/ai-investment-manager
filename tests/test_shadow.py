from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine

from quant_core.config import AppConfig
from quant_core.cycle import AnalysisCycle
from quant_core.execution import MockExchange
from quant_core.ingestion import InMemoryEventStore
from quant_core.market_data import (
    ClosedMarketBar,
    InMemoryMarketDataStore,
    MarketQuote,
    MarketTrade,
)
from quant_core.persistence import SqlFactLedger, SqlRiskBudgetStore, create_schema
from quant_core.shadow import SqlShadowStateReader
from quant_core.trigger import (
    AnalysisTriggerType,
    build_initial_trigger_plan,
    build_trigger_batch,
    build_trigger_event,
)
from quant_core.trigger_runtime import TriggerAnalysisRequestBuilder

NOW = datetime(2026, 8, 18, 12, 10, 30, tzinfo=UTC)


def _shadow_config(app_config) -> AppConfig:
    raw = app_config.model_dump(mode="python")
    raw["deployment"] = {
        "version": "deployment-shadow-test-v1",
        "stage": "SHADOW",
        "shadow_market_data_enabled": True,
        "testnet_order_submission_enabled": False,
        "live_order_submission_enabled": False,
        "credential_profile": None,
        "manual_approval_ref": None,
    }
    raw["market_data"]["symbols"] = ("BTCUSDT",)
    return AppConfig.model_validate(raw)


def _market_store() -> InMemoryMarketDataStore:
    store = InMemoryMarketDataStore()
    store.put_quote(
        MarketQuote(
            quote_id="q1",
            symbol="BTCUSDT",
            observed_at=NOW - timedelta(seconds=1),
            bid="100",
            bid_quantity="1",
            ask="100.1",
            ask_quantity="1",
            update_id=1,
            source="test",
        )
    )
    store.put_trade(
        MarketTrade(
            trade_id="t1",
            symbol="BTCUSDT",
            aggregate_trade_id=1,
            event_time=NOW - timedelta(seconds=2),
            observed_at=NOW - timedelta(seconds=1),
            price="100.05",
            quantity="0.1",
            buyer_is_maker=False,
            source="test",
        )
    )
    for minutes in range(40, 0, -5):
        opened = NOW.replace(second=0) - timedelta(minutes=minutes)
        store.put_bar(
            ClosedMarketBar(
                symbol="BTCUSDT",
                interval="5m",
                open_time=opened,
                close_time=opened + timedelta(minutes=5) - timedelta(milliseconds=1),
                observed_at=NOW - timedelta(seconds=1),
                open="99",
                high="101",
                low="98",
                close="100",
                volume="10",
                source="test",
            )
        )
    return store


class EmptyShadowState:
    def account_for_cycle(self, *, cycle_id, as_of, initial_quote_balance):
        from quant_core.domain import AccountSnapshot

        return AccountSnapshot(
            cycle_id=cycle_id,
            as_of=as_of,
            observed_at=as_of,
            quote_balance=initial_quote_balance,
            reconciled=True,
        )

    def last_cycle_at(self, *, symbol, as_of):
        return None

    def entry_orders_today(self, *, as_of):
        return 0


def test_trigger_request_builder_freezes_one_batch_without_owning_schedule(app_config) -> None:
    config = _shadow_config(app_config)
    plan = build_initial_trigger_plan(
        symbol="BTCUSDT",
        pipeline_id=config.pipeline.version,
        manifest_id="manifest-v1",
        updated_at=NOW,
        heartbeat_seconds=900,
    )
    trigger = build_trigger_event(
        trigger_type=AnalysisTriggerType.MARKET_SHOCK,
        symbol="BTCUSDT",
        pipeline_id=config.pipeline.version,
        occurred_at=NOW,
        observed_at=NOW,
        priority=90,
        dedup_key="shock-1",
    )
    batch = build_trigger_batch(
        plan=plan,
        triggers=(trigger,),
        created_at=NOW,
        deadline=NOW + timedelta(minutes=5),
    )
    request = TriggerAnalysisRequestBuilder(
        config=config,
        market_store=_market_store(),
        event_store=InMemoryEventStore(),
        state=EmptyShadowState(),
    ).build(batch)

    assert request.cycle_input.market.cycle_id == request.cycle_input.account.cycle_id
    assert request.cycle_input.account.quote_balance == app_config.shadow.initial_quote_balance
    assert request.trigger.reason.value == "EVENT_BATCH"


def test_sql_shadow_account_is_projected_from_latest_business_fact(
    app_config, replay_input
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    cycle = AnalysisCycle.with_adapters(
        app_config,
        ledger=SqlFactLedger(engine),
        exchange=MockExchange(app_config.execution),
        risk_budget=SqlRiskBudgetStore(engine),
    )
    result = cycle.run(replay_input)
    assert result.account_after is not None
    reader = SqlShadowStateReader(engine)
    next_as_of = replay_input.market.as_of + timedelta(minutes=1)
    account = reader.account_for_cycle(
        cycle_id="next-cycle",
        as_of=next_as_of,
        initial_quote_balance=app_config.shadow.initial_quote_balance,
    )
    assert account.cycle_id == "next-cycle"
    assert account.quote_balance == result.account_after.quote_balance
    assert reader.entry_orders_today(as_of=next_as_of) == 1
    assert reader.last_cycle_at(symbol="BTCUSDT", as_of=next_as_of) == replay_input.market.as_of
