from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine

from investment_manager.forecast.context.contract import (
    assessment_available_feature_selectors,
    assessment_current_evidence_ids,
)
from investment_manager.forecast.context.verification import packet_feature_values
from investment_manager.kernel.identity import stable_id
from investment_manager.market.features import FeatureEngine
from investment_manager.market.models import InstrumentId, InstrumentProduct, TradFiMarket
from investment_manager.market.perpetual.models import (
    PerpetualMarketState,
    PerpetualQuote,
    TradingScheduleSnapshot,
    TradingSession,
    TradingSessionType,
)
from investment_manager.schema import create_schema
from investment_manager.state.decision.application import DecisionPacketPreparation
from investment_manager.state.decision.packet import (
    AnalysisMandate,
    MandateExposure,
    ObservationAsset,
    PacketReviewRequest,
    decision_packet_analysis_projection,
)
from investment_manager.state.decision.repository import SqlDecisionPacketAssembler
from investment_manager.state.economic_reference import (
    ObservationReference,
    build_economic_reference_snapshot,
)
from investment_manager.state.evidence_repository import (
    SqlStateEvidenceStore,
    StateEvidenceKind,
)
from investment_manager.state.policy import DecisionPacketPolicy
from investment_manager.state.projection import SqlStateProjector

NOW = datetime(2026, 8, 29, 11, tzinfo=UTC)


def _reference_instrument() -> InstrumentId:
    return InstrumentId(
        product=InstrumentProduct.TRADFI_PERPETUAL,
        symbol="XAUUSDT",
        base_asset="XAU",
        quote_asset="USDT",
        settlement_asset="USDT",
        tradfi_market=TradFiMarket.COMMODITY,
    )


def _reference_inputs(replay_input):
    instrument = _reference_instrument()
    target = replay_input.market.model_copy(
        update={
            "cycle_id": "paxg-xau-reference",
            "symbol": "PAXGUSDT",
            "as_of": NOW,
            "observed_at": NOW,
            "bid": Decimal("4450"),
            "ask": Decimal("4452"),
            "last": Decimal("4451"),
        }
    )
    exchange_time = NOW - timedelta(seconds=1)
    state = PerpetualMarketState(
        state_id=stable_id(
            "perpetual_market_state", instrument.key, exchange_time.isoformat()
        ),
        instrument=instrument,
        exchange_time=exchange_time,
        observed_at=NOW,
        mark_price="4461",
        index_price="4460",
        last_funding_rate="0",
        interest_rate="0",
        next_funding_time=NOW + timedelta(hours=4),
        source="test",
    )
    quote = PerpetualQuote(
        quote_id=stable_id("perpetual_quote", instrument.key, 1),
        instrument=instrument,
        exchange_time=exchange_time,
        observed_at=NOW,
        bid="4460",
        bid_quantity="1",
        ask="4460.2",
        ask_quantity="1",
        update_id=1,
        source="test",
    )
    schedule_exchange_time = NOW - timedelta(seconds=2)
    schedule = TradingScheduleSnapshot(
        schedule_id=stable_id(
            "tradfi_trading_schedule", schedule_exchange_time.isoformat()
        ),
        exchange_time=schedule_exchange_time,
        observed_at=NOW,
        sessions=(
            TradingSession(
                market=TradFiMarket.COMMODITY,
                starts_at=NOW - timedelta(hours=1),
                ends_at=NOW + timedelta(hours=1),
                session_type=TradingSessionType.NO_TRADING,
            ),
        ),
        source="test",
    )
    policy = ObservationReference(
        target_asset="PAXG",
        reference_instrument_key=instrument.key,
    )
    return policy, target, state, quote, schedule


def test_economic_reference_preserves_closed_state_and_point_in_time(replay_input) -> None:
    policy, target, state, quote, schedule = _reference_inputs(replay_input)

    snapshot = build_economic_reference_snapshot(
        policy=policy,
        target=target,
        reference_state=state,
        reference_quote=quote,
        schedule=schedule,
    )

    assert snapshot.session_type == TradingSessionType.NO_TRADING
    assert snapshot.reference_index_price == Decimal("4460")
    assert snapshot.target_reference_deviation_bps == (
        (Decimal("4451") / Decimal("4460") - Decimal("1")) * Decimal("10000")
    )

    future_quote = quote.model_copy(update={"observed_at": NOW + timedelta(seconds=1)})
    with pytest.raises(ValueError, match="as_of 之后"):
        build_economic_reference_snapshot(
            policy=policy,
            target=target,
            reference_state=state,
            reference_quote=future_quote,
            schedule=schedule,
        )


def test_preparation_exposes_closed_reference_and_degrades_missing_schedule(
    app_config,
    replay_input,
) -> None:
    policy, target, state, quote, schedule = _reference_inputs(replay_input)

    class ReferenceStore:
        current_schedule = schedule

        @staticmethod
        def latest_perpetual_state(*, instrument, as_of):
            assert instrument == state.instrument
            assert as_of == NOW
            return state

        @staticmethod
        def latest_perpetual_quote(*, instrument, evaluation_at, visible_at):
            assert instrument == quote.instrument
            assert evaluation_at == visible_at == NOW
            return quote

        def latest_trading_schedule(self, *, as_of):
            assert as_of == NOW
            return self.current_schedule

    store = ReferenceStore()
    preparation = DecisionPacketPreparation(
        market_store=store,
        event_reader=None,
        facts=None,
        projector=None,
        assembler=None,
        features=FeatureEngine(app_config.feature),
        market_interval="5m",
        market_bar_window=64,
        market_source="test",
        maximum_market_age_seconds=180,
        perpetual_instruments=(state.instrument,),
        maximum_perpetual_age_seconds=900,
    )
    mandate = AnalysisMandate(
        version="economic-reference-mandate-v1",
        analysis_scope="primary-portfolio",
        question="维护黄金经济底层与可交易代理之间的点时关系。",
        mandate_exposures=(
            MandateExposure(economic_exposure="INFLATION_SENSITIVE", asset="PAXG"),
        ),
        observation_assets=(
            ObservationAsset(
                asset="PAXG",
                market_symbol="PAXGUSDT",
            ),
        ),
        observation_references=(policy,),
        required_risk_factors=("US_REAL_INTEREST_RATES",),
    )

    references, quality_codes = preparation._economic_reference_context(
        as_of=NOW,
        mandate=mandate,
        markets=(target,),
    )

    assert len(references) == 1
    assert references[0].session_type == TradingSessionType.NO_TRADING
    assert quality_codes == ()

    store.current_schedule = None
    references, quality_codes = preparation._economic_reference_context(
        as_of=NOW,
        mandate=mandate,
        markets=(target,),
    )
    assert references == ()
    assert quality_codes == ("ECONOMIC_REFERENCE.PAXG.SCHEDULE_MISSING",)


def test_economic_reference_round_trips_into_model_visible_packet(
    app_config,
    replay_input,
) -> None:
    policy, target, state, quote, schedule = _reference_inputs(replay_input)
    reference = build_economic_reference_snapshot(
        policy=policy,
        target=target,
        reference_state=state,
        reference_quote=quote,
        schedule=schedule,
    )
    feature = FeatureEngine(app_config.feature).compute(target)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    projector = SqlStateProjector(
        engine,
        projection_version="portfolio-state-economic-reference-v1",
        delta_policy=app_config.decision_state.delta_policy,
    )
    projection = projector.project(
        analysis_scope="primary-portfolio",
        as_of=NOW,
        built_at=NOW,
        facts=(),
        markets=(target,),
        features=(feature,),
        economic_references=(reference,),
    )
    mandate = AnalysisMandate(
        version="economic-reference-mandate-v1",
        analysis_scope="primary-portfolio",
        question="维护黄金经济底层与可交易代理之间的点时关系。",
        mandate_exposures=(
            MandateExposure(economic_exposure="INFLATION_SENSITIVE", asset="PAXG"),
        ),
        observation_assets=(
            ObservationAsset(
                asset="PAXG",
                market_symbol="PAXGUSDT",
            ),
        ),
        observation_references=(policy,),
        required_risk_factors=("US_REAL_INTEREST_RATES",),
    )
    request = PacketReviewRequest.create(
        requested_at=NOW,
        reason="定时更新世界认知",
    )
    packet = SqlDecisionPacketAssembler(
        engine,
        DecisionPacketPolicy(
            version="decision-packet-economic-reference-v1",
            schema_version="decision-packet-v22",
        ),
    ).assemble(
        mandate=mandate,
        state_id=projection.state.state_id,
        delta_ids=(),
        review_requests=(request,),
    )

    assert len(packet.economic_reference_states) == 1
    visible = packet.economic_reference_states[0]
    assert visible.reference_instrument.key == policy.reference_instrument_key
    assert visible.evidence_ref in assessment_current_evidence_ids(packet)
    selector = "economic_reference_state:PAXG.target_reference_deviation_bps"
    assert selector in assessment_available_feature_selectors(packet)
    assert packet_feature_values(packet)[selector] == reference.target_reference_deviation_bps
    table = decision_packet_analysis_projection(packet)["economic_reference_states"]
    assert table["rows"][0][0] == "PAXG"
    assert table["rows"][0][4] == TradingSessionType.NO_TRADING.value

    stored = SqlStateEvidenceStore(engine).get(visible.evidence_ref)
    assert stored == (StateEvidenceKind.ECONOMIC_REFERENCE, reference)
