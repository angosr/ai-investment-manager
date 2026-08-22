from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

import pytest
from sqlalchemy import create_engine, insert

from investment_manager.decision_cycle.capital import assemble_capital_cycle
from investment_manager.governance.evaluation.capital import (
    CapitalShadowEvaluationSpec,
    equity_values_reconcile,
)
from investment_manager.governance.evaluation.capital_ledger import SqlCapitalLedgerProjector
from investment_manager.governance.models import ReleaseArtifact, load_release_manifest
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.types import floor_to_step
from investment_manager.market.models import InstrumentProduct, MarketQuote
from investment_manager.market.perpetual.models import (
    FundingRateType,
    FundingSettlement,
    PerpetualMarketState,
    PerpetualQuote,
)
from investment_manager.market.repository import SqlMarketDataStore
from investment_manager.portfolio.models import (
    CapitalCycleOutcome,
    CapitalCycleRecord,
    PortfolioAccountSnapshot,
    PortfolioPerformanceInterval,
)
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


def _account_intervals(
    start: datetime,
    *,
    delay_seconds: int,
    starting_equity: Decimal,
) -> tuple[PortfolioPerformanceInterval, ...]:
    snapshots = tuple(
        PortfolioAccountSnapshot(
            snapshot_id=f"boundary-account-{sequence}",
            cycle_id=f"boundary-cycle-{sequence}",
            portfolio_id="primary-portfolio",
            revision=sequence,
            as_of=_month(start, sequence) + timedelta(seconds=delay_seconds),
            observed_at=_month(start, sequence) + timedelta(seconds=delay_seconds),
            settlement_asset="USDT",
            cash_balance=starting_equity + Decimal(sequence),
            equity=starting_equity + Decimal(sequence),
            equity_high_water=max(Decimal("10000"), starting_equity + Decimal(sequence)),
        )
        for sequence in range(13)
    )
    return tuple(
        PortfolioPerformanceInterval.between(current, following)
        for current, following in pairwise(snapshots)
    )


def _legacy_v3_spec(spec: CapitalShadowEvaluationSpec) -> CapitalShadowEvaluationSpec:
    payload = spec.model_dump(mode="python")
    payload["version"] = "capital-shadow-evaluation-spec-v3"
    payload.pop("equity_boundary_rule")
    payload["behavior_contract"] = tuple(
        item
        for item in payload["behavior_contract"]
        if item
        not in {
            "BOUNDED_AUTHORITATIVE_ACCOUNT_BOUNDARIES",
            "OBSERVATION_RETURN_STARTS_FROM_BOUNDARY_ACCOUNT_EQUITY",
        }
    )
    payload["thresholds"].pop("maximum_account_boundary_delay_seconds")
    return CapitalShadowEvaluationSpec.model_validate(payload)


def _evaluation_spec(config) -> CapitalShadowEvaluationSpec:
    permission = config.capital.mock_candidate_authorizations[0]
    return CapitalShadowEvaluationSpec.freeze(
        plan_id=permission.evaluation_plan_id,
        config=config,
        manifest=_release(config),
        observation_start=datetime(2026, 9, 1, tzinfo=UTC),
        observation_end=datetime(2027, 9, 1, tzinfo=UTC),
    )


def _put_flat_quotes(
    market: SqlMarketDataStore,
    config,
    *,
    at: datetime,
    sequence: int,
) -> None:
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
            quote_id=f"flat-spot-{sequence}",
            symbol=spot.symbol,
            observed_at=at,
            bid=Decimal("99995"),
            bid_quantity=Decimal("5"),
            ask=Decimal("100005"),
            ask_quantity=Decimal("5"),
            source="capital-ledger-test",
        )
    )
    market.put_perpetual_quote(
        PerpetualQuote(
            quote_id=stable_id("perpetual_quote", perpetual.key, sequence),
            instrument=perpetual,
            exchange_time=at,
            observed_at=at,
            bid=Decimal("100495"),
            bid_quantity=Decimal("5"),
            ask=Decimal("100505"),
            ask_quantity=Decimal("5"),
            update_id=sequence,
            source="capital-ledger-test",
        )
    )


def test_v4_monthly_returns_use_bounded_authoritative_account_revisions() -> None:
    config = load_config("config/investment-manager.shadow.yaml")
    spec = _evaluation_spec(config)
    start = spec.observation_start
    intervals = _account_intervals(
        start,
        delay_seconds=5,
        starting_equity=Decimal("9876.54"),
    )

    monthly, starting, ending = SqlCapitalLedgerProjector._monthly_returns(
        spec,
        intervals,
    )

    assert len(monthly) == 12
    assert starting == Decimal("9876.54")
    assert ending == Decimal("9888.54")
    assert spec.starting_equity == Decimal("10000")


def test_v4_monthly_returns_reject_account_revision_beyond_frozen_delay() -> None:
    config = load_config("config/investment-manager.shadow.yaml")
    spec = _evaluation_spec(config)
    maximum_delay = spec.thresholds.maximum_account_boundary_delay_seconds
    assert maximum_delay is not None
    intervals = _account_intervals(
        spec.observation_start,
        delay_seconds=maximum_delay + 1,
        starting_equity=Decimal("10000"),
    )

    with pytest.raises(ValueError, match="有界时间内缺少权威账户估值"):
        SqlCapitalLedgerProjector._monthly_returns(spec, intervals)


def test_v3_monthly_returns_preserve_exact_configured_start_semantics() -> None:
    config = load_config("config/investment-manager.shadow.yaml")
    spec = _legacy_v3_spec(_evaluation_spec(config))
    exact = _account_intervals(
        spec.observation_start,
        delay_seconds=0,
        starting_equity=Decimal("10000"),
    )

    _monthly, starting, _ending = SqlCapitalLedgerProjector._monthly_returns(spec, exact)
    assert starting == spec.starting_equity

    delayed = _account_intervals(
        spec.observation_start,
        delay_seconds=1,
        starting_equity=Decimal("10000"),
    )
    with pytest.raises(ValueError, match="缺少精确账户估值"):
        SqlCapitalLedgerProjector._monthly_returns(spec, delayed)

    changed_start = _account_intervals(
        spec.observation_start,
        delay_seconds=0,
        starting_equity=Decimal("9999"),
    )
    with pytest.raises(ValueError, match="起始权益与预登记合同不一致"):
        SqlCapitalLedgerProjector._monthly_returns(spec, changed_start)


def test_counterfactual_normalizes_from_observation_boundary_equity() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    create_schema(engine)
    config = load_config("config/investment-manager.shadow.yaml")
    spec = _evaluation_spec(config)
    market = SqlMarketDataStore(engine)
    first = spec.observation_start + timedelta(seconds=5)
    _put_flat_quotes(market, config, at=first, sequence=1)
    _put_flat_quotes(market, config, at=spec.observation_end, sequence=2)
    record = CapitalCycleRecord.create(
        portfolio_id=spec.portfolio_id,
        pipeline_id=config.pipeline.version,
        cause_id="boundary-equity-counterfactual",
        trigger_batch_id="boundary-equity-counterfactual",
        symbol="BTCUSDT",
        trigger_types=("SCHEDULED",),
        triggered_at=first,
        evaluated_at=first,
        decision_cycle_id="boundary-equity-cycle",
        account_snapshot_id="boundary-equity-account",
        forecast_ids=(),
        target_id=None,
        outcome=CapitalCycleOutcome.NO_OPPORTUNITY,
        reason_codes=("CASH_SELECTED_NO_ELIGIBLE_FORECAST",),
    )
    starting_equity = Decimal("9876.54")

    annualized, _source_ids = SqlCapitalLedgerProjector(
        engine,
        config,
    )._calendar_counterfactual(
        spec,
        (record,),
        starting_equity=starting_equity,
    )

    spot_spec = next(
        item
        for item in config.capital.execution_specs
        if item.instrument.product == InstrumentProduct.SPOT
    )
    perpetual_spec = next(
        item
        for item in config.capital.execution_specs
        if item.instrument.product == InstrumentProduct.USD_M_PERPETUAL
    )
    evidence = config.carry_forecast.evidence
    assert evidence is not None
    quantity = floor_to_step(
        starting_equity
        * evidence.evaluated_gross_exposure_fraction
        / Decimal("2")
        / Decimal("100495"),
        max(spot_spec.quantity_step, perpetual_spec.quantity_step),
    )
    entry_cost = quantity * (
        Decimal("10")
        + (Decimal("100005") * spot_spec.fee_bps + Decimal("100495") * perpetual_spec.fee_bps)
        / Decimal("10000")
    )
    exit_cost = quantity * (
        Decimal("10")
        + (Decimal("99995") * spot_spec.fee_bps + Decimal("100505") * perpetual_spec.fee_bps)
        / Decimal("10000")
    )
    assert equity_values_reconcile(
        annualized,
        -(entry_cost + exit_cost) / starting_equity,
    )


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
