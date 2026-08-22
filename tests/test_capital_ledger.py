from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, insert

from investment_manager.decision_cycle.capital import assemble_capital_cycle
from investment_manager.governance.evaluation.capital import CapitalShadowEvaluationSpec
from investment_manager.governance.evaluation.capital_ledger import SqlCapitalLedgerProjector
from investment_manager.governance.models import ReleaseArtifact, load_release_manifest
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.market.models import InstrumentProduct, MarketQuote
from investment_manager.market.perpetual.models import (
    FundingRateType,
    FundingSettlement,
    PerpetualMarketState,
    PerpetualQuote,
)
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.scheduling.tables import analysis_trigger_batches
from investment_manager.schema import create_schema
from investment_manager.settings import load_config


def _month(value: datetime, offset: int = 1) -> datetime:
    month_index = value.year * 12 + value.month - 1 + offset
    return value.replace(
        year=month_index // 12,
        month=month_index % 12 + 1,
    )


def _release(config):
    historical = load_release_manifest("config/release-manifest.yaml")
    evidence = config.carry_forecast.evidence
    assert evidence is not None
    return historical.model_copy(
        update={
            "manifest_id": "release-capital-ledger-test",
            "code_version": "b" * 40,
            "configuration_hash": content_hash(config),
            "component_versions": tuple(
                (name, getattr(config, name).version)
                for name, _version in historical.component_versions
            ),
            "artifacts": (
                ReleaseArtifact(
                    artifact_id=evidence.source_evaluation_id,
                    sha256=evidence.source_artifact_sha256,
                ),
            ),
        }
    )


def _put_market(market: SqlMarketDataStore, config, *, at: datetime, sequence: int) -> None:
    spot_mid = Decimal("100000") + Decimal(sequence * 100)
    spot = next(
        item.instrument
        for item in config.capital.execution_specs
        if item.instrument.product == InstrumentProduct.SPOT
    )
    perpetual = next(
        item.instrument
        for item in config.capital.execution_specs
        if item.instrument.product == InstrumentProduct.USD_M_PERPETUAL
    )
    market.put_quote(
        MarketQuote(
            quote_id=f"capital-ledger-spot-{sequence}",
            symbol=spot.symbol,
            observed_at=at,
            bid=spot_mid - Decimal("5"),
            bid_quantity=Decimal("5"),
            ask=spot_mid + Decimal("5"),
            ask_quantity=Decimal("5"),
            source="capital-ledger-test",
        )
    )
    perpetual_mid = spot_mid + Decimal("500")
    market.put_perpetual_quote(
        PerpetualQuote(
            quote_id=stable_id("perpetual_quote", perpetual.key, sequence),
            instrument=perpetual,
            exchange_time=at,
            observed_at=at,
            bid=perpetual_mid - Decimal("5"),
            bid_quantity=Decimal("5"),
            ask=perpetual_mid + Decimal("5"),
            ask_quantity=Decimal("5"),
            update_id=sequence,
            source="capital-ledger-test",
        )
    )
    market.put_perpetual_state(
        PerpetualMarketState(
            state_id=stable_id(
                "perpetual_market_state",
                perpetual.key,
                at.isoformat(),
            ),
            instrument=perpetual,
            exchange_time=at,
            observed_at=at,
            mark_price=perpetual_mid,
            index_price=spot_mid,
            last_funding_rate=Decimal("0.0004"),
            interest_rate=Decimal("0.0001"),
            next_funding_time=at + timedelta(hours=8),
            source="capital-ledger-test",
        )
    )
    for hours, rate in ((24, "0.0005"), (16, "0.0004"), (8, "0.0003")):
        funding_time = at - timedelta(hours=hours)
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
                funding_rate=Decimal(rate),
                mark_price=perpetual_mid,
                rate_type=FundingRateType.REGULAR,
                source="capital-ledger-test",
            )
        )


def _put_batch(engine, config, *, at: datetime, sequence: int) -> str:
    batch_id = f"capital-ledger-batch-{sequence}"
    with engine.begin() as connection:
        connection.execute(
            insert(analysis_trigger_batches).values(
                batch_id=batch_id,
                symbol="BTCUSDT",
                pipeline_id=config.pipeline.version,
                plan_revision=1,
                first_occurred_at=at,
                first_observed_at=at,
                batched_at=at,
                analysis_submitted_at=at,
                payload={"batch_id": batch_id, "triggers": []},
            )
        )
    return batch_id


def test_capital_ledger_projects_exact_months_and_point_in_time_counterfactual() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = datetime(2027, 9, 1, tzinfo=UTC)
    permission = config.capital.mock_candidate_authorizations[0].model_copy(
        update={"valid_until": datetime(2027, 10, 1, tzinfo=UTC)}
    )
    config = config.model_copy(
        update={
            "capital": config.capital.model_copy(
                update={"mock_candidate_authorizations": (permission,)}
            )
        }
    )
    spec = CapitalShadowEvaluationSpec.freeze(
        plan_id=permission.evaluation_plan_id,
        config=config,
        manifest=_release(config),
        observation_start=start,
        observation_end=end,
    )
    market = SqlMarketDataStore(engine)
    service = assemble_capital_cycle(config, engine)
    for sequence in range(13):
        at = _month(start, sequence)
        _put_market(market, config, at=at, sequence=sequence)
        batch_id = _put_batch(engine, config, at=at, sequence=sequence)
        service.produce(
            as_of=at,
            cause_id=batch_id,
            trigger_batch_id=batch_id,
            symbol="BTCUSDT",
            trigger_types=("HEARTBEAT",),
        )

    projection = SqlCapitalLedgerProjector(engine, config).project(
        spec=spec,
        projected_at=datetime(2027, 9, 8, tzinfo=UTC),
    )

    assert len(projection.monthly_net_return_fractions) == 12
    assert projection.forecast_available_months == 12
    assert projection.decision_complete_months == 12
    assert projection.late_entry_count == 0
    assert projection.duplicate_execution_group_count == 0
    assert projection.unresolved_execution_group_count == 0
    assert projection.maximum_unhedged_seconds == 0
    assert projection.starting_equity == Decimal("10000")
    assert projection.ending_equity - projection.starting_equity == projection.net_pnl
    assert projection.source_counterfactual_annualized_return_fraction is not None
    assert spec.source_evaluation_id in projection.source_ids
    assert spec.source_result_hash in projection.source_ids
