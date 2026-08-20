from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import create_engine

from investment_manager.governance.performance import (
    EvaluationVariant,
    ReplayCase,
    ReplayEvaluator,
    without_information_events,
)
from investment_manager.governance.repository import SqlEvaluationRepository
from investment_manager.legacy.cycle import AnalysisCycle
from investment_manager.market.models import MarketSnapshot
from investment_manager.schema import create_schema


def _case(replay_input) -> ReplayCase:
    outcome_at = replay_input.market.as_of + timedelta(minutes=61)
    raw = replay_input.market.model_dump(mode="json")
    raw.update(
        {
            "cycle_id": "outcome-market-1",
            "as_of": outcome_at,
            "observed_at": outcome_at,
            "bid": "103.19",
            "ask": "103.21",
            "last": "103.20",
        }
    )
    return ReplayCase(
        case_id="uptrend-1",
        cycle_input=replay_input,
        outcome_market=MarketSnapshot.model_validate(raw),
    )


def test_replay_evaluation_uses_same_snapshot_costs_and_never_trade_baseline(
    app_config, replay_input
) -> None:
    case = _case(replay_input)
    report = ReplayEvaluator(minimum_conclusive_sample=30).evaluate(
        (case,),
        {
            "never_trade": None,
            "off_price_baseline": lambda: AnalysisCycle.create(app_config),
            "off_without_information": EvaluationVariant(
                cycle_factory=lambda: AnalysisCycle.create(app_config),
                input_transform=without_information_events,
            ),
        },
        baseline_ids=("never_trade", "off_price_baseline"),
    )

    by_id = {item.variant_id: item for item in report.variants}
    never = by_id["never_trade"]
    off = by_id["off_price_baseline"]
    assert never.net_pnl == 0
    assert off.executed_count == 1
    assert off.closed_trade_count == 1
    assert off.total_fees > 0
    assert off.net_pnl == off.gross_pnl - off.total_fees
    assert report.comparisons[0].incremental_net_pnl == off.net_pnl
    ablated = by_id["off_without_information"]
    assert ablated.net_pnl == off.net_pnl
    ai_increment_style = next(
        item
        for item in report.comparisons
        if item.challenger_id == "off_without_information"
        and item.baseline_id == "off_price_baseline"
    )
    assert ai_increment_style.incremental_net_pnl == 0
    assert not report.statistically_conclusive
    assert report.limitations == ("SAMPLE_TOO_SMALL_FOR_STATISTICAL_CONCLUSION",)


def test_evaluation_reports_loss_and_drawdown_after_real_costs(app_config, replay_input) -> None:
    case = _case(replay_input)
    losing_market = case.outcome_market.model_copy(
        update={"bid": Decimal("99.49"), "ask": Decimal("99.51"), "last": Decimal("99.50")}
    )
    losing = case.model_copy(update={"outcome_market": losing_market})

    report = ReplayEvaluator().evaluate(
        (losing,),
        {
            "never_trade": None,
            "off_price_baseline": lambda: AnalysisCycle.create(app_config),
        },
    )
    off = next(item for item in report.variants if item.variant_id == "off_price_baseline")

    assert off.net_pnl < 0
    assert off.maximum_drawdown == -off.net_pnl
    assert off.win_rate == 0


def test_evaluation_report_is_replayable_persistent_fact(app_config, replay_input) -> None:
    report = ReplayEvaluator().evaluate(
        (_case(replay_input),),
        {
            "never_trade": None,
            "off_price_baseline": lambda: AnalysisCycle.create(app_config),
        },
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    repository = SqlEvaluationRepository(engine)

    repository.record(report)
    repository.record(report)

    assert repository.get(report.report_id) == report
