from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine

from investment_manager.forecast.contract_repository import SqlForecastContractStore
from investment_manager.forecast.contracts import (
    ForecastBenchmarkProbability,
    ForecastContract,
    ForecastDecisionSlot,
    ForecastOutcomeBucket,
    ForecastPriceAnchor,
)
from investment_manager.forecast.models import (
    ExposureDirection,
    ForecastLeg,
    ForecastTarget,
)
from investment_manager.forecast.repository import SqlForecastStore
from investment_manager.forecast.results import ForecastOutcomeStatus
from investment_manager.forecast.settlement import ForecastOutcomeSettler
from investment_manager.kernel.identity import stable_id
from investment_manager.market.models import (
    InstrumentId,
    InstrumentProduct,
    MarketQuote,
)
from investment_manager.market.perpetual.models import (
    FundingRateType,
    FundingSettlement,
    PerpetualQuote,
)
from investment_manager.market.repository import InMemoryMarketDataStore
from investment_manager.schema import create_schema

NOW = datetime(2026, 8, 21, 3, tzinfo=UTC)


def _instruments() -> tuple[InstrumentId, InstrumentId]:
    spot = InstrumentId.binance_spot(
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
    )
    perpetual = InstrumentId(
        product=InstrumentProduct.USD_M_PERPETUAL,
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        settlement_asset="USDT",
    )
    return spot, perpetual


def _contract_and_slot() -> tuple[ForecastContract, ForecastDecisionSlot]:
    spot, perpetual = _instruments()
    buckets = (
        ForecastOutcomeBucket(
            bucket_id="LOSS",
            upper_bps=Decimal("-25"),
            representative_bps=Decimal("-100"),
        ),
        ForecastOutcomeBucket(
            bucket_id="FLAT",
            lower_bps=Decimal("-25"),
            upper_bps=Decimal("25"),
            representative_bps=Decimal("0"),
        ),
        ForecastOutcomeBucket(
            bucket_id="GAIN",
            lower_bps=Decimal("25"),
            representative_bps=Decimal("100"),
        ),
    )
    contract = ForecastContract.create(
        contract_version="carry-1h-v1",
        outcome_family_id="BTC_CARRY_1H",
        target=ForecastTarget.create(
            (
                ForecastLeg(
                    instrument=spot,
                    direction=ExposureDirection.LONG,
                    gross_weight=Decimal("0.5"),
                ),
                ForecastLeg(
                    instrument=perpetual,
                    direction=ExposureDirection.SHORT,
                    gross_weight=Decimal("0.5"),
                ),
            )
        ),
        outcome_buckets=buckets,
        horizon_minutes=60,
        decision_slot_rule="hourly-v1",
        evaluation_trigger="hourly-v1",
        information_cutoff_rule="slot-as-of-v1",
        completion_deadline_seconds=30,
        minimum_remaining_horizon_minutes=30,
        entry_anchor_rule="first-executable-v1",
        cost_semantics_version="complete-cost-v1",
        validity_minutes=30,
        validity_conditions=("TARGET_MATERIAL_DELTA",),
        settlement_rule="cutoff-executable-v1",
        forecast_benchmark=tuple(
            ForecastBenchmarkProbability(bucket_id=item.bucket_id, probability=probability)
            for item, probability in zip(
                buckets,
                (Decimal("0.25"), Decimal("0.5"), Decimal("0.25")),
                strict=True,
            )
        ),
        decision_benchmark="cash-v1",
    )
    slot = ForecastDecisionSlot.create(
        contract,
        slot_as_of=NOW,
        cutoff_prices=(
            ForecastPriceAnchor(
                instrument_id=spot.key,
                price=Decimal("100"),
                observed_at=NOW,
                available_at=NOW,
                quote_ref="spot-cutoff",
            ),
            ForecastPriceAnchor(
                instrument_id=perpetual.key,
                price=Decimal("101"),
                observed_at=NOW,
                available_at=NOW,
                quote_ref="perpetual-cutoff",
            ),
        ),
    )
    return contract, slot


def _spot_quote(*, quote_id: str, at: datetime, bid: str, ask: str) -> MarketQuote:
    return MarketQuote(
        quote_id=quote_id,
        symbol="BTCUSDT",
        observed_at=at,
        bid=bid,
        bid_quantity="1",
        ask=ask,
        ask_quantity="1",
        source="test",
    )


def _perpetual_quote(*, at: datetime, update_id: int, bid: str, ask: str) -> PerpetualQuote:
    instrument = _instruments()[1]
    return PerpetualQuote(
        quote_id=stable_id("perpetual_quote", instrument.key, update_id),
        instrument=instrument,
        exchange_time=at,
        observed_at=at,
        bid=bid,
        bid_quantity="1",
        ask=ask,
        ask_quantity="1",
        update_id=update_id,
        source="test",
    )


def _stores():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    contract_store = SqlForecastContractStore(engine)
    forecast_store = SqlForecastStore(engine)
    contract, slot = _contract_and_slot()
    contract_store.record_contract(contract)
    contract_store.record_slot(slot)
    return forecast_store, contract, slot


def test_shared_slot_outcome_settles_executable_payoff_even_without_trade() -> None:
    store, contract, slot = _stores()
    market = InMemoryMarketDataStore()
    evaluation_at = slot.evaluation_at
    market.put_quote(
        _spot_quote(quote_id="spot-exit", at=evaluation_at, bid="102", ask="102.1")
    )
    market.put_perpetual_quote(
        _perpetual_quote(at=evaluation_at, update_id=2, bid="100.4", ask="100.5")
    )
    perpetual = _instruments()[1]
    funding_time = NOW + timedelta(minutes=30)
    market.put_funding_settlement(
        FundingSettlement(
            settlement_id=stable_id(
                "funding_settlement",
                perpetual.key,
                funding_time.isoformat(),
                FundingRateType.REGULAR.value,
            ),
            instrument=perpetual,
            funding_time=funding_time,
            observed_at=funding_time + timedelta(seconds=1),
            funding_rate=Decimal("0.0001"),
            mark_price=Decimal("100.8"),
            rate_type=FundingRateType.REGULAR,
            source="test",
        )
    )
    settler = ForecastOutcomeSettler(
        market=market,
        store=store,
        evaluation_version="forecast-target-outcome-v2",
        maximum_spot_age_seconds=60,
        maximum_perpetual_age_seconds=900,
        maximum_funding_gap_hours=12,
        settlement_grace_minutes=5,
    )

    result = settler.settle(as_of=evaluation_at + timedelta(seconds=2))
    outcome = store.outcomes(
        contract_id=contract.contract_id,
        evaluation_version="forecast-target-outcome-v2",
    )[0]

    assert result.settled == 1
    assert outcome.status == ForecastOutcomeStatus.SETTLED
    assert outcome.legs[0].exit_price == Decimal("102")
    assert outcome.legs[1].exit_price == Decimal("100.5")
    assert outcome.legs[1].funding_return_bps > 0
    assert outcome.realized_bucket_id in {"LOSS", "FLAT", "GAIN"}


def test_slot_waits_for_market_facts_then_records_outcome_unavailable() -> None:
    store, contract, slot = _stores()
    settler = ForecastOutcomeSettler(
        market=InMemoryMarketDataStore(),
        store=store,
        evaluation_version="forecast-target-outcome-v2",
        maximum_spot_age_seconds=60,
        maximum_perpetual_age_seconds=900,
        maximum_funding_gap_hours=12,
        settlement_grace_minutes=5,
    )

    pending = settler.settle(as_of=slot.evaluation_at + timedelta(minutes=4))
    unavailable = settler.settle(as_of=slot.evaluation_at + timedelta(minutes=5))
    outcome = store.outcomes(
        contract_id=contract.contract_id,
        evaluation_version="forecast-target-outcome-v2",
    )[0]

    assert pending.pending == 1
    assert unavailable.outcome_unavailable == 1
    assert outcome.status == ForecastOutcomeStatus.OUTCOME_UNAVAILABLE
