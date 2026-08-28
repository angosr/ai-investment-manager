from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from investment_manager.governance.evaluation.reference_selection import (
    ReferenceEvidenceLayer,
    ReferenceEvidenceRequirement,
    ReferenceQualificationPolicy,
    ReferenceStressWindow,
    build_reference_candidate,
    build_reference_selection_plan,
)
from investment_manager.kernel.identity import content_hash
from investment_manager.portfolio.policy import (
    EconomicExposure,
    ReferenceAllocationPolicy,
)
from investment_manager.research.economic_series import (
    EconomicSeriesFrequency,
    EconomicSeriesKind,
    EconomicSeriesObservation,
    EconomicSeriesRole,
    HistoricalEconomicSeriesManifest,
)
from investment_manager.research.reference import evaluate_reference_economics


def test_reference_economics_rebalances_and_deflates(monkeypatch) -> None:
    gold = _dataset(
        dataset_id="gold",
        exposure=EconomicExposure.INFLATION_SENSITIVE,
        monthly_growth=Decimal("1.004"),
    )
    cpi = _dataset(
        dataset_id="cpi",
        exposure=None,
        monthly_growth=Decimal("1.002"),
    )
    datasets = {"gold": gold, "cpi": cpi}
    monkeypatch.setattr(
        "investment_manager.research.reference.HistoricalEconomicSeriesCatalog.load",
        lambda _self, dataset_id: datasets[dataset_id],
    )
    candidate = build_reference_candidate(
        allocations=(
            ReferenceAllocationPolicy(
                implementation_key="BINANCE:SPOT:PAXGUSDT",
                target_exposure_fraction=Decimal("0.50"),
            ),
            ReferenceAllocationPolicy(
                implementation_key="CASH:USDT",
                target_exposure_fraction=Decimal("0.50"),
            ),
        ),
        rebalance_band_fraction=Decimal("0.05"),
    )
    plan = build_reference_selection_plan(
        registered_at=datetime(2026, 1, 1, tzinfo=UTC),
        mandate_version="mandate-v1",
        universe_version="universe-v1",
        risk_policy_version="risk-v1",
        cost_model_version="cost-v1",
        development_start=date(2000, 1, 1),
        development_end=date(2010, 1, 1),
        blind_start=date(2010, 1, 1),
        blind_end=date(2020, 1, 1),
        evidence_requirements=(
            ReferenceEvidenceRequirement(
                layer=ReferenceEvidenceLayer.ECONOMIC_PROXY,
                scope="INFLATION_SENSITIVE",
                minimum_observation_count=120,
                minimum_span_days=3650,
                fixed_evidence_id="gold",
                fixed_content_hash=content_hash(gold.manifest),
            ),
            ReferenceEvidenceRequirement(
                layer=ReferenceEvidenceLayer.OBJECTIVE_DEFLATOR,
                scope="US_CPI_REAL_PURCHASING_POWER",
                minimum_observation_count=120,
                minimum_span_days=3650,
                fixed_evidence_id="cpi",
                fixed_content_hash=content_hash(cpi.manifest),
            ),
        ),
        stress_windows=(
            ReferenceStressWindow(
                stress_id="test-stress",
                start=date(2005, 1, 1),
                end=date(2005, 4, 1),
            ),
        ),
        qualification=ReferenceQualificationPolicy(
            minimum_annualized_real_return_fraction=Decimal("0"),
            maximum_drawdown_fraction=Decimal("0.10"),
            maximum_worst_stress_loss_fraction=Decimal("0.10"),
            maximum_annualized_turnover_fraction=Decimal("0.50"),
            maximum_annualized_cost_fraction=Decimal("0.01"),
            maximum_single_risk_contribution_fraction=Decimal("1"),
        ),
        candidates=(candidate,),
    )

    result = evaluate_reference_economics(
        plan,
        economic_catalog=Path("."),
        exposure_by_implementation={
            "BINANCE:SPOT:PAXGUSDT": EconomicExposure.INFLATION_SENSITIVE
        },
    )

    assert result.development.annualized_real_return_fraction > 0
    assert result.blind.annualized_real_return_fraction > 0
    assert result.development.risk_contributions[0].fraction == 1
    assert result.stress[0].loss_fraction == 0


def test_reference_economics_compounds_registered_cash_proxy(monkeypatch) -> None:
    gold = _dataset(
        dataset_id="gold",
        exposure=EconomicExposure.INFLATION_SENSITIVE,
        monthly_growth=Decimal("1"),
    )
    cash = _dataset(
        dataset_id="cash",
        exposure=EconomicExposure.CASH,
        monthly_growth=Decimal("1.003"),
    )
    cpi = _dataset(
        dataset_id="cpi",
        exposure=None,
        monthly_growth=Decimal("1.002"),
    )
    datasets = {"cash": cash, "gold": gold, "cpi": cpi}
    monkeypatch.setattr(
        "investment_manager.research.reference.HistoricalEconomicSeriesCatalog.load",
        lambda _self, dataset_id: datasets[dataset_id],
    )
    candidate = build_reference_candidate(
        allocations=(
            ReferenceAllocationPolicy(
                implementation_key="BINANCE:SPOT:PAXGUSDT",
                target_exposure_fraction=Decimal("0.30"),
            ),
            ReferenceAllocationPolicy(
                implementation_key="CASH:USDT",
                target_exposure_fraction=Decimal("0.70"),
            ),
        ),
        rebalance_band_fraction=Decimal("0.05"),
    )
    requirements = tuple(
        ReferenceEvidenceRequirement(
            layer=(
                ReferenceEvidenceLayer.OBJECTIVE_DEFLATOR
                if exposure is None
                else ReferenceEvidenceLayer.ECONOMIC_PROXY
            ),
            scope=dataset_id.upper(),
            minimum_observation_count=120,
            minimum_span_days=3650,
            fixed_evidence_id=dataset_id,
            fixed_content_hash=content_hash(dataset.manifest),
        )
        for dataset_id, exposure, dataset in (
            ("cash", EconomicExposure.CASH, cash),
            ("gold", EconomicExposure.INFLATION_SENSITIVE, gold),
            ("cpi", None, cpi),
        )
    )
    plan = build_reference_selection_plan(
        registered_at=datetime(2026, 1, 1, tzinfo=UTC),
        mandate_version="mandate-v1",
        universe_version="universe-v1",
        risk_policy_version="risk-v1",
        cost_model_version="cost-v1",
        development_start=date(2000, 1, 1),
        development_end=date(2010, 1, 1),
        blind_start=date(2010, 1, 1),
        blind_end=date(2020, 1, 1),
        evidence_requirements=requirements,
        stress_windows=(
            ReferenceStressWindow(
                stress_id="test-stress",
                start=date(2005, 1, 1),
                end=date(2005, 4, 1),
            ),
        ),
        qualification=ReferenceQualificationPolicy(
            minimum_annualized_real_return_fraction=Decimal("0"),
            maximum_drawdown_fraction=Decimal("0.10"),
            maximum_worst_stress_loss_fraction=Decimal("0.10"),
            maximum_annualized_turnover_fraction=Decimal("0.50"),
            maximum_annualized_cost_fraction=Decimal("0.01"),
            maximum_single_risk_contribution_fraction=Decimal("1"),
        ),
        candidates=(candidate,),
    )

    result = evaluate_reference_economics(
        plan,
        economic_catalog=Path("."),
        exposure_by_implementation={
            "BINANCE:SPOT:PAXGUSDT": EconomicExposure.INFLATION_SENSITIVE
        },
    )

    assert result.development.annualized_nominal_return_fraction > Decimal("0.02")
    assert result.development.annualized_real_return_fraction > 0
    assert {item.exposure for item in result.development.risk_contributions} == {
        EconomicExposure.CASH,
        EconomicExposure.INFLATION_SENSITIVE,
    }


def _dataset(
    *,
    dataset_id: str,
    exposure: EconomicExposure | None,
    monthly_growth: Decimal,
) -> SimpleNamespace:
    observations = []
    level = Decimal("100")
    year, month = 1999, 12
    for _ in range(26 * 12):
        month += 1
        if month == 13:
            year += 1
            month = 1
        level *= monthly_growth
        observations.append(
            EconomicSeriesObservation(
                effective_date=date(year, month, 1),
                value=level,
            )
        )
    role = (
        EconomicSeriesRole.OBJECTIVE_DEFLATOR
        if exposure is None
        else EconomicSeriesRole.EXPOSURE_PROXY
    )
    manifest = HistoricalEconomicSeriesManifest.model_construct(
        dataset_id=dataset_id,
        series_id=dataset_id,
        role=role,
        economic_exposure=exposure,
        kind=EconomicSeriesKind.PRICE_LEVEL,
        frequency=EconomicSeriesFrequency.MONTHLY,
        unit="INDEX",
        source_name="test",
        source_url="https://example.test/data",
        documentation_url="https://example.test/docs",
        source_sha256="a" * 64,
        collected_at=datetime(2026, 1, 1, tzinfo=UTC),
        first_effective_date=observations[0].effective_date,
        last_effective_date=observations[-1].effective_date,
        observation_count=len(observations),
        observations_hash="b" * 64,
    )
    return SimpleNamespace(
        manifest=manifest,
        observations=tuple(observations),
    )
