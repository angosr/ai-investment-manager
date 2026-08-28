from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.market.models import ClosedMarketBar
from investment_manager.research.dataset import (
    HistoricalDataset,
    HistoricalDatasetManifest,
    InstrumentSpec,
    _bars_hash,
)
from investment_manager.research.fixed_maturity_carry import (
    DatedContractEvidence,
    DatedFutureBar,
    FixedMaturityCarryDataset,
    FixedMaturityCarryDatasetManifest,
    FixedMaturityCarryPlan,
    evaluate_fixed_maturity_carry,
    load_fixed_maturity_carry_plan,
)


def _plan(*, minimum_entered: int = 2) -> FixedMaturityCarryPlan:
    first = datetime(2026, 3, 2, tzinfo=UTC)
    second = datetime(2026, 4, 1, tzinfo=UTC)
    return FixedMaturityCarryPlan(
        plan_id="test-fixed-maturity",
        plan_hash="1" * 64,
        observed_cutoff=datetime(2026, 1, 1, tzinfo=UTC),
        settlement_start=first,
        settlement_end=second,
        starting_equity=Decimal("10000"),
        entry_days_before_delivery=30,
        leg_notional_fraction=Decimal("0.40"),
        futures_collateral_fraction=Decimal("0.60"),
        quantity_step=Decimal("0.001"),
        minimum_notional=Decimal("50"),
        spot_fee_bps=Decimal("10"),
        futures_fee_bps=Decimal("5"),
        friction_bps=Decimal("2.5"),
        total_round_trip_bps=Decimal("40"),
        maintenance_margin_fraction=Decimal("0.10"),
        minimum_completed_contracts=2,
        minimum_entered_contracts=minimum_entered,
        minimum_positive_trade_fraction=Decimal("0.75"),
        minimum_positive_regimes=2,
        maximum_drawdown_fraction=Decimal("0.10"),
        minimum_margin_buffer_fraction=Decimal("0.10"),
        regimes=((first, second), (second, second)),
    )


def _spot_dataset() -> HistoricalDataset:
    start = datetime(2026, 1, 31, tzinfo=UTC)
    count = 30 * 24 * 2 + 1
    bars = tuple(
        ClosedMarketBar(
            symbol="BTCUSDT",
            interval="1h",
            open_time=start + timedelta(hours=index),
            close_time=start + timedelta(hours=index + 1) - timedelta(milliseconds=1),
            observed_at=start + timedelta(hours=index + 1) - timedelta(milliseconds=1),
            open=Decimal("100"),
            high=Decimal("100"),
            low=Decimal("100"),
            close=Decimal("100"),
            volume=Decimal("10"),
            source="test",
        )
        for index in range(count)
    )
    instrument = InstrumentSpec(
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        price_increment=Decimal("0.01"),
        quantity_increment=Decimal("0.00001"),
        minimum_quantity=Decimal("0.00001"),
        maximum_quantity=Decimal("1000"),
        minimum_notional=Decimal("5"),
        minimum_price=Decimal("0.01"),
        maximum_price=Decimal("1000000"),
    )
    end = start + timedelta(hours=count)
    digest = _bars_hash(bars)
    manifest = HistoricalDatasetManifest(
        dataset_id=stable_id(
            "historical_dataset",
            "historical-bars-v1",
            "test",
            "BTCUSDT",
            "1h",
            start,
            end,
            digest,
            instrument,
        ),
        symbol="BTCUSDT",
        interval="1h",
        source="test",
        collected_at=end,
        requested_start=start,
        requested_end=end,
        first_open_time=bars[0].open_time,
        last_close_time=bars[-1].close_time,
        bar_count=len(bars),
        bars_hash=digest,
        instrument=instrument,
    )
    return HistoricalDataset(manifest=manifest, bars=bars)


def _contract(delivery: datetime, *, crisis_high: Decimal | None = None):
    start = delivery - timedelta(days=30)
    bars = tuple(
        DatedFutureBar(
            open_time=start + timedelta(hours=index),
            close_time=start + timedelta(hours=index + 1) - timedelta(milliseconds=1),
            open=Decimal("110"),
            high=(crisis_high if index == 1 and crisis_high is not None else Decimal("110")),
            low=Decimal("110"),
            close=Decimal("110"),
            volume=Decimal("10"),
        )
        for index in range(30 * 24)
    )
    return DatedContractEvidence(
        contract_symbol=f"BTCUSDT_{delivery.strftime('%y%m%d')}",
        delivery_time=delivery,
        delivery_price=Decimal("100"),
        bars=bars,
    )


def _dataset(plan: FixedMaturityCarryPlan, contracts):
    contracts = tuple(contracts)
    records_hash = content_hash(contracts)
    payload = {
        "schema_version": "fixed-maturity-carry-dataset-v1",
        "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash,
        "spot_dataset_id": _spot_dataset().manifest.dataset_id,
        "settlement_start": plan.settlement_start,
        "settlement_end": plan.settlement_end,
        "records_hash": records_hash,
    }
    return FixedMaturityCarryDataset(
        manifest=FixedMaturityCarryDatasetManifest(
            dataset_id=stable_id("fixed_maturity_carry_dataset", *payload.values()),
            collected_at=datetime(2026, 5, 1, tzinfo=UTC),
            contract_count=len(contracts),
            **payload,
        ),
        contracts=contracts,
    )


def test_fixed_maturity_carry_compounds_only_costed_convergence() -> None:
    plan = _plan()
    spot = _spot_dataset()
    contracts = (_contract(plan.settlement_start), _contract(plan.settlement_end))
    result = evaluate_fixed_maturity_carry(
        plan=plan,
        spot_dataset=spot,
        dataset=_dataset(plan, contracts),
        plan_commit="a" * 40,
        evaluator_commit="b" * 40,
        evaluated_at=datetime(2026, 5, 1, tzinfo=UTC),
    )

    assert result.status == "PASSED_RETROSPECTIVE"
    assert result.entered_contract_count == 2
    assert result.ending_equity > result.starting_equity
    assert all(item.net_pnl > 0 for item in result.contracts)
    assert result.minimum_futures_margin_buffer_fraction > Decimal("0.10")
    assert all(item.net_return_fraction > 0 for item in result.regimes)


def test_fixed_maturity_carry_rejects_short_leg_liquidation() -> None:
    plan = _plan(minimum_entered=1)
    spot = _spot_dataset()
    contracts = (
        _contract(plan.settlement_start, crisis_high=Decimal("400")),
        _contract(plan.settlement_end),
    )
    result = evaluate_fixed_maturity_carry(
        plan=plan,
        spot_dataset=spot,
        dataset=_dataset(plan, contracts),
        plan_commit="a" * 40,
        evaluator_commit="b" * 40,
        evaluated_at=datetime(2026, 5, 1, tzinfo=UTC),
    )

    assert result.status == "REJECTED_RETROSPECTIVE"
    assert "LIQUIDATION_OCCURRED" in result.reason_codes
    assert result.contracts[0].status == "LIQUIDATED"


def test_fixed_maturity_carry_records_missing_hours_instead_of_aborting() -> None:
    plan = _plan(minimum_entered=1)
    spot = _spot_dataset()
    incomplete = _contract(plan.settlement_start)
    incomplete = incomplete.model_copy(update={"bars": incomplete.bars[1:]})
    contracts = (incomplete, _contract(plan.settlement_end))

    result = evaluate_fixed_maturity_carry(
        plan=plan,
        spot_dataset=spot,
        dataset=_dataset(plan, contracts),
        plan_commit="a" * 40,
        evaluator_commit="b" * 40,
        evaluated_at=datetime(2026, 5, 1, tzinfo=UTC),
    )

    assert result.status == "REJECTED_RETROSPECTIVE"
    assert result.complete_contract_count == 1
    assert result.contracts[0].status == "INCOMPLETE"
    assert "missing_future_hours=1" in result.contracts[0].reason


def test_repository_plan_is_strictly_bound_to_cost_and_account_rules() -> None:
    plan = load_fixed_maturity_carry_plan(Path("config/research/btc-quarterly-cash-carry-v1.yaml"))
    assert plan.starting_equity == Decimal("10000")
    assert plan.total_round_trip_bps == Decimal("40")
    assert plan.minimum_entered_contracts == 8
