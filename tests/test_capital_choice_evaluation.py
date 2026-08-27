from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from investment_manager.execution.models import Side
from investment_manager.forecast.models import ExposureDirection
from investment_manager.portfolio.evaluation import (
    CAPITAL_CHOICE_EVALUATION_VERSION,
    TRADING_COST_EVALUATION_VERSION,
    CapitalChoiceCase,
    ExecutionFillCase,
    evaluate_capital_choice,
    evaluate_trading_cost,
    is_full_forecast_capital_choice,
)
from investment_manager.portfolio.models import CapitalCycleOutcome, CapitalCycleRecord

NOW = datetime(2026, 8, 26, 19, tzinfo=UTC)


def _fill(
    fill_id: str,
    cycle_id: str,
    symbol: str,
    side: Side,
    at: datetime,
    quantity: str,
    price: str,
    fee: str,
) -> ExecutionFillCase:
    return ExecutionFillCase(
        fill_id=fill_id,
        cycle_id=cycle_id,
        sleeve_id=f"sleeve-{symbol}",
        instrument_key=f"BINANCE:USD_M_PERPETUAL:{symbol}",
        side=side,
        group_started_at=at,
        filled_at=at,
        quantity=Decimal(quantity),
        price=Decimal(price),
        contract_multiplier=Decimal("1"),
        fee=Decimal(fee),
    )


def _case(
    projection_id: str,
    exposure: str,
    instrument: str,
    direction: ExposureDirection,
    *,
    selected: bool = False,
    predicted: str = "-5",
    cost: str = "10",
    decision_gross: str | None = None,
    projection_gross: str | None = None,
    realized: str = "0",
) -> CapitalChoiceCase:
    predicted_value = Decimal(predicted)
    cost_value = Decimal(cost)
    decision_gross_value = (
        Decimal(decision_gross) if decision_gross is not None else predicted_value + cost_value
    )
    return CapitalChoiceCase(
        decision_id="target-1",
        decision_at=NOW,
        evaluation_at=NOW + timedelta(hours=4),
        economic_exposure_id=exposure,
        projection_id=projection_id,
        instrument_key=instrument,
        direction=direction,
        selected=selected,
        predicted_net_bps=predicted_value,
        decision_gross_bps=decision_gross_value,
        projection_gross_bps=(
            Decimal(projection_gross) if projection_gross is not None else decision_gross_value
        ),
        decision_cost_bps=cost_value,
        realized_product_gross_bps=Decimal(realized),
    )


def test_trading_cost_evidence_isolates_recent_added_risk_with_lifo_matching() -> None:
    opened = datetime(2026, 8, 27, 10, 56, 36, 93696, tzinfo=UTC)
    closed = datetime(2026, 8, 27, 11, 0, 58, 476036, tzinfo=UTC)
    evidence = evaluate_trading_cost(
        (
            _fill(
                "btc-old",
                "material-forecast",
                "BTCUSDT",
                Side.BUY,
                opened - timedelta(minutes=26),
                "0.006",
                "79543.40",
                "0.2386302",
            ),
            _fill(
                "btc-release-rebound",
                "stale-cadence-rebound",
                "BTCUSDT",
                Side.BUY,
                opened,
                "0.056",
                "79527.40",
                "2.2267672",
            ),
            _fill(
                "paxg-release-rebound",
                "stale-cadence-rebound",
                "PAXGUSDT",
                Side.SELL,
                opened,
                "0.638",
                "4581.5400",
                "1.46151126",
            ),
            _fill(
                "btc-fresh-cash",
                "fresh-cadence-cash",
                "BTCUSDT",
                Side.SELL,
                closed,
                "0.062",
                "79545.60",
                "2.4659136",
            ),
            _fill(
                "paxg-fresh-cash",
                "fresh-cadence-cash",
                "PAXGUSDT",
                Side.BUY,
                closed,
                "0.638",
                "4580.9500",
                "1.46132305",
            ),
        )
    )

    assert evidence.evaluation_version == TRADING_COST_EVALUATION_VERSION
    assert evidence.fill_count == 5
    assert evidence.round_trip_count == 3
    assert evidence.open_lot_count == 0
    recent_btc = next(
        item for item in evidence.round_trips if item.entry_fill_id == "btc-release-rebound"
    )
    assert recent_btc.entry_cycle_id == "stale-cadence-rebound"
    assert recent_btc.exit_cycle_id == "fresh-cadence-cash"
    assert recent_btc.quantity == Decimal("0.056")
    assert recent_btc.holding_seconds == Decimal("262.38234")
    assert recent_btc.realized_gross_pnl == Decimal("1.01920")
    assert recent_btc.realized_net_pnl < 0
    paxg = next(
        item for item in evidence.round_trips if item.entry_fill_id == "paxg-release-rebound"
    )
    assert paxg.direction == ExposureDirection.SHORT
    assert paxg.realized_gross_pnl == Decimal("0.3764200")
    assert paxg.realized_net_pnl < 0
    assert evidence.cost_reversal_round_trip_count == 3
    assert evidence.realized_gross_pnl > 0
    assert evidence.realized_net_pnl < 0
    assert evidence.closed_fee_to_realized_gross_pnl == (
        evidence.closed_fee_cost / evidence.realized_gross_pnl
    )
    assert evidence.closed_fee_cost == sum(
        (item.fee_cost for item in evidence.round_trips), Decimal("0")
    )
    reconciled = evaluate_trading_cost(
        (
            _fill(
                "one-entry",
                "entry",
                "BTCUSDT",
                Side.BUY,
                opened,
                "0.01",
                "80000",
                "0.4",
            ),
            _fill(
                "one-exit",
                "exit",
                "BTCUSDT",
                Side.SELL,
                closed,
                "0.01",
                "80100",
                "0.4005",
            ),
        ),
        expected_price_pnl=Decimal("1"),
        expected_fee_cost=Decimal("0.8005"),
    )
    assert reconciled.accounting_reconciled


def test_trading_cost_evidence_keeps_unclosed_fees_out_of_closed_result() -> None:
    evidence = evaluate_trading_cost(
        (
            _fill(
                "still-open",
                "new-forecast",
                "BTCUSDT",
                Side.BUY,
                NOW,
                "0.01",
                "80000",
                "0.4",
            ),
        )
    )

    assert evidence.round_trip_count == 0
    assert evidence.open_lot_count == 1
    assert evidence.closed_fee_cost == 0
    assert evidence.open_fee_cost == Decimal("0.4")
    assert evidence.closed_fee_to_realized_gross_pnl is None
    assert evidence.closed_fee_to_positive_gross_pnl is None
    assert evidence.accounting_reconciled is None


def test_capital_choice_identifies_missed_exposure_after_decision_time_costs() -> None:
    evidence = evaluate_capital_choice(
        (
            _case(
                "btc-perp-long",
                "CRYPTO_NETWORK:BTC:USDT",
                "BINANCE:USD_M_PERPETUAL:BTCUSDT",
                ExposureDirection.LONG,
                predicted="-9",
                realized="25",
            ),
            _case(
                "btc-perp-short",
                "CRYPTO_NETWORK:BTC:USDT",
                "BINANCE:USD_M_PERPETUAL:BTCUSDT",
                ExposureDirection.SHORT,
                predicted="-19",
                realized="-25",
            ),
            _case(
                "btc-spot-long",
                "CRYPTO_NETWORK:BTC:USDT",
                "BINANCE:SPOT:BTCUSDT",
                ExposureDirection.LONG,
                predicted="-20",
                cost="20",
                realized="24",
            ),
        ),
        capital_behavior_id="capital-v1",
    )

    assert evidence.evaluation_version == CAPITAL_CHOICE_EVALUATION_VERSION
    assert evidence.candidate_count == 3
    assert evidence.missed_profitable_exposure_count == 1
    exposure = evidence.exposures[0]
    assert exposure.selected is None
    assert exposure.best_realized.projection_id == "btc-perp-long"
    assert exposure.best_realized.realized_net_bps == Decimal("15")
    assert exposure.opportunity_gap_bps == Decimal("15")
    assert exposure.missed_profitable_exposure
    assert not exposure.selected_unprofitable_exposure


def test_capital_choice_separates_selected_loss_from_product_choice_gap() -> None:
    evidence = evaluate_capital_choice(
        (
            _case(
                "paxg-long",
                "INFLATION_SENSITIVE:PAXG:USDT",
                "BINANCE:USD_M_PERPETUAL:PAXGUSDT",
                ExposureDirection.LONG,
                selected=True,
                predicted="8",
                realized="-12",
            ),
            _case(
                "paxg-short",
                "INFLATION_SENSITIVE:PAXG:USDT",
                "BINANCE:USD_M_PERPETUAL:PAXGUSDT",
                ExposureDirection.SHORT,
                predicted="-18",
                realized="12",
            ),
        ),
        capital_behavior_id="capital-v1",
    )

    exposure = evidence.exposures[0]
    assert exposure.selected is not None
    assert exposure.selected.realized_net_bps == Decimal("-22")
    assert exposure.best_realized.projection_id == "paxg-short"
    assert exposure.best_realized.realized_net_bps == Decimal("2")
    assert exposure.opportunity_gap_bps == Decimal("24")
    assert not exposure.missed_profitable_exposure
    assert exposure.selected_unprofitable_exposure
    assert evidence.selected_unprofitable_exposure_count == 1


def test_capital_choice_reanchors_product_outcome_to_the_actual_decision_time() -> None:
    evidence = evaluate_capital_choice(
        (
            _case(
                "btc-long",
                "CRYPTO_NETWORK:BTC:USDT",
                "BINANCE:USD_M_PERPETUAL:BTCUSDT",
                ExposureDirection.LONG,
                predicted="-5",
                cost="10",
                decision_gross="5",
                projection_gross="20",
                realized="30",
            ),
        ),
        capital_behavior_id="capital-v1",
    )

    # Product outcome is +30 bp from the original anchor, but +15 bp had
    # already happened before this decision.  Only the remaining +15 bp is
    # attributable to this choice, leaving +5 bp after its frozen future cost.
    outcome = evidence.exposures[0].best_realized
    assert outcome.realized_net_bps == Decimal("5")
    assert evidence.exposures[0].opportunity_gap_bps == Decimal("5")


def test_capital_choice_rejects_inconsistent_predicted_net_economics() -> None:
    with pytest.raises(ValueError, match="预测净收益"):
        CapitalChoiceCase(
            decision_id="target-1",
            decision_at=NOW,
            evaluation_at=NOW + timedelta(hours=4),
            economic_exposure_id="CRYPTO_NETWORK:BTC:USDT",
            projection_id="btc-long",
            instrument_key="BINANCE:USD_M_PERPETUAL:BTCUSDT",
            direction=ExposureDirection.LONG,
            selected=False,
            predicted_net_bps=Decimal("1"),
            decision_gross_bps=Decimal("5"),
            projection_gross_bps=Decimal("5"),
            decision_cost_bps=Decimal("10"),
            realized_product_gross_bps=Decimal("20"),
        )


def test_only_fresh_forecast_decisions_support_cross_exposure_choice_evaluation() -> None:
    def receipt(
        *,
        outcome: CapitalCycleOutcome = CapitalCycleOutcome.TARGET_DECIDED,
        trigger_types: tuple[str, ...],
    ) -> CapitalCycleRecord:
        return CapitalCycleRecord.create(
            portfolio_id="primary",
            pipeline_id="capital-v1",
            cause_id=f"cause-{trigger_types[0]}",
            trigger_batch_id="batch-1",
            symbol="BTCUSDT",
            trigger_types=trigger_types,
            triggered_at=NOW,
            evaluated_at=NOW,
            decision_cycle_id="cycle-1",
            account_snapshot_id="account-1",
            forecast_ids=("forecast-1",),
            target_id=(
                "target-1"
                if outcome
                in {
                    CapitalCycleOutcome.TARGET_DECIDED,
                    CapitalCycleOutcome.FORECAST_ALREADY_DECIDED,
                }
                else None
            ),
            outcome=outcome,
            reason_codes=("TEST",),
        )

    assert is_full_forecast_capital_choice(
        receipt(trigger_types=("FORECAST_CADENCE",)),
        capital_behavior_id="capital-v1",
    )
    assert is_full_forecast_capital_choice(
        receipt(trigger_types=("FORECAST_EVENT_DUE",)),
        capital_behavior_id="capital-v1",
    )
    assert not is_full_forecast_capital_choice(
        receipt(trigger_types=("WORLD_MODEL_UPDATED",)),
        capital_behavior_id="capital-v1",
    )
    assert not is_full_forecast_capital_choice(
        receipt(
            outcome=CapitalCycleOutcome.FORECAST_ALREADY_DECIDED,
            trigger_types=("FORECAST_CADENCE",),
        ),
        capital_behavior_id="capital-v1",
    )
    assert not is_full_forecast_capital_choice(
        receipt(trigger_types=("FORECAST_CADENCE",)),
        capital_behavior_id="capital-v2",
    )


def test_capital_choice_rejects_duplicate_or_multiple_selected_products() -> None:
    case = _case(
        "btc-long",
        "CRYPTO_NETWORK:BTC:USDT",
        "BINANCE:USD_M_PERPETUAL:BTCUSDT",
        ExposureDirection.LONG,
        selected=True,
    )
    with pytest.raises(ValueError, match="projection 不得重复"):
        evaluate_capital_choice((case, case), capital_behavior_id="capital-v1")

    with pytest.raises(ValueError, match="不得选择多个产品"):
        evaluate_capital_choice(
            (
                case,
                _case(
                    "btc-short",
                    "CRYPTO_NETWORK:BTC:USDT",
                    "BINANCE:USD_M_PERPETUAL:BTCUSDT",
                    ExposureDirection.SHORT,
                    selected=True,
                ),
            ),
            capital_behavior_id="capital-v1",
        )
