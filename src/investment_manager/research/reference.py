from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import pairwise
from math import prod, sqrt
from pathlib import Path

from investment_manager.governance.evaluation.reference_selection import (
    ReferenceEconomicMetrics,
    ReferenceEvidenceLayer,
    ReferenceRiskContribution,
    ReferenceSelectionEvidence,
    ReferenceSelectionPlan,
    ReferenceStressResult,
)
from investment_manager.kernel.identity import content_hash
from investment_manager.market.models import InstrumentId, InstrumentProduct
from investment_manager.portfolio.policy import EconomicExposure
from investment_manager.research.dataset import (
    HistoricalDatasetCatalog,
    HistoricalDatasetManifest,
    HistoricalFundingDatasetCatalog,
    HistoricalFundingDatasetManifest,
)
from investment_manager.research.economic_series import (
    EconomicSeriesFrequency,
    EconomicSeriesKind,
    EconomicSeriesRole,
    HistoricalEconomicSeriesCatalog,
    HistoricalEconomicSeriesDataset,
)
from investment_manager.research.quote_dataset import (
    HistoricalExecutableQuoteCatalog,
    HistoricalExecutableQuoteManifest,
)


@dataclass(frozen=True, slots=True)
class ReferenceEconomicEvaluation:
    development: ReferenceEconomicMetrics
    blind: ReferenceEconomicMetrics
    stress: tuple[ReferenceStressResult, ...]


@dataclass(frozen=True, slots=True)
class _MonthlyEconomicRow:
    month: date
    returns: Mapping[EconomicExposure, float]


def evaluate_reference_economics(
    plan: ReferenceSelectionPlan,
    *,
    economic_catalog: Path,
    exposure_by_implementation: Mapping[str, EconomicExposure],
) -> ReferenceEconomicEvaluation:
    """Evaluate only the frozen economic exposures; product cash flows stay separate."""

    datasets = _fixed_economic_datasets(plan, catalog=economic_catalog)
    rows = _monthly_economic_rows(datasets)
    cpi_levels = _monthly_levels(datasets[None])
    candidate = plan.candidates[0]
    weights: dict[EconomicExposure, float] = {}
    for allocation in candidate.allocations:
        exposure = (
            EconomicExposure.CASH
            if allocation.implementation_key.startswith("CASH:")
            else exposure_by_implementation.get(allocation.implementation_key)
        )
        if exposure is None:
            raise ValueError("Reference 候选实现缺少经济暴露映射")
        if exposure in weights:
            raise ValueError("Reference 候选不得用多个产品重复同一经济暴露")
        weights[exposure] = float(allocation.target_exposure_fraction)
    proxy_exposures = {item for item in datasets if item is not None}
    if set(weights) - {EconomicExposure.CASH} != proxy_exposures:
        raise ValueError("Reference 候选与固定经济代理覆盖不一致")
    band = float(candidate.rebalance_band_fraction)
    development = _economic_metrics(
        rows,
        start=plan.development_start,
        end=plan.development_end,
        weights=weights,
        rebalance_band=band,
        cpi_levels=cpi_levels,
    )
    blind = _economic_metrics(
        rows,
        start=plan.blind_start,
        end=plan.blind_end,
        weights=weights,
        rebalance_band=band,
        cpi_levels=cpi_levels,
    )
    stress = tuple(
        ReferenceStressResult(
            stress_id=window.stress_id,
            loss_fraction=_decimal(
                _simulate_economic_window(
                    rows,
                    start=window.start,
                    end=window.end,
                    weights=weights,
                    rebalance_band=band,
                    minimum_observation_count=1,
                )[1]
            ),
        )
        for window in plan.stress_windows
    )
    return ReferenceEconomicEvaluation(
        development=development,
        blind=blind,
        stress=stress,
    )


def _fixed_economic_datasets(
    plan: ReferenceSelectionPlan,
    *,
    catalog: Path,
) -> dict[EconomicExposure | None, HistoricalEconomicSeriesDataset]:
    store = HistoricalEconomicSeriesCatalog(catalog)
    datasets: dict[EconomicExposure | None, HistoricalEconomicSeriesDataset] = {}
    for requirement in plan.evidence_requirements:
        if requirement.layer not in {
            ReferenceEvidenceLayer.ECONOMIC_PROXY,
            ReferenceEvidenceLayer.OBJECTIVE_DEFLATOR,
        }:
            continue
        if requirement.fixed_evidence_id is None:
            raise ValueError("Reference 经济评价只能读取预登记固定数据")
        dataset = store.load(requirement.fixed_evidence_id)
        key = dataset.manifest.economic_exposure
        if key in datasets:
            raise ValueError("Reference 经济评价存在重复暴露或购买力序列")
        if content_hash(dataset.manifest) != requirement.fixed_content_hash:
            raise ValueError("Reference 经济评价数据哈希与预登记不一致")
        datasets[key] = dataset
    if None not in datasets or len(datasets) < 2:
        raise ValueError("Reference 经济评价缺少购买力或风险代理")
    return datasets


def _monthly_economic_rows(
    datasets: Mapping[EconomicExposure | None, HistoricalEconomicSeriesDataset],
) -> tuple[_MonthlyEconomicRow, ...]:
    returns_by_exposure: dict[EconomicExposure, dict[date, float]] = {}
    for exposure, dataset in datasets.items():
        if exposure is None:
            continue
        returns_by_exposure[exposure] = _monthly_returns(dataset)
    months = sorted(
        set.intersection(*(set(values) for values in returns_by_exposure.values()))
    )
    rows = tuple(
        _MonthlyEconomicRow(
            month=month,
            returns={
                exposure: values[month]
                for exposure, values in returns_by_exposure.items()
            },
        )
        for month in months
    )
    if len(rows) < 120:
        raise ValueError("Reference 经济代理共同月度窗口不足")
    for previous, current in pairwise(rows):
        if current.month != _next_month(previous.month):
            raise ValueError("Reference 经济代理共同月度窗口存在缺口")
    return rows


def _monthly_returns(dataset: HistoricalEconomicSeriesDataset) -> dict[date, float]:
    manifest = dataset.manifest
    if manifest.kind == EconomicSeriesKind.TOTAL_RETURN:
        grouped: dict[date, list[float]] = defaultdict(list)
        for item in dataset.observations:
            grouped[date(item.effective_date.year, item.effective_date.month, 1)].append(
                float(item.value)
            )
        return {
            month: prod(1 + value for value in values) - 1
            for month, values in grouped.items()
        }
    if (
        manifest.kind != EconomicSeriesKind.PRICE_LEVEL
        or manifest.frequency != EconomicSeriesFrequency.MONTHLY
    ):
        raise ValueError("Reference 价格代理必须是月度水平序列")
    levels = {item.effective_date: float(item.value) for item in dataset.observations}
    ordered = sorted(levels)
    returns: dict[date, float] = {}
    for previous, current in pairwise(ordered):
        if current != _next_month(previous):
            raise ValueError("Reference 月度水平序列存在缺口")
        returns[current] = levels[current] / levels[previous] - 1
    return returns


def _economic_metrics(
    rows: tuple[_MonthlyEconomicRow, ...],
    *,
    start: date,
    end: date,
    weights: Mapping[EconomicExposure, float],
    rebalance_band: float,
    cpi_levels: Mapping[date, float],
) -> ReferenceEconomicMetrics:
    selected = tuple(item for item in rows if start <= item.month < end)
    returns, maximum_drawdown, annualized_turnover, final_equity = (
        _simulate_economic_window(
            rows,
            start=start,
            end=end,
            weights=weights,
            rebalance_band=rebalance_band,
        )
    )
    years = len(selected) / 12
    inflation_growth = _inflation_growth(cpi_levels, start=start, end=end)
    nominal = final_equity ** (1 / years) - 1
    real = (final_equity / inflation_growth) ** (1 / years) - 1
    mean = sum(returns) / len(returns)
    volatility = sqrt(
        sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    ) * sqrt(12)
    return ReferenceEconomicMetrics(
        annualized_nominal_return_fraction=_decimal(nominal),
        annualized_real_return_fraction=_decimal(real),
        annualized_volatility_fraction=_decimal(volatility),
        maximum_drawdown_fraction=_decimal(maximum_drawdown),
        annualized_turnover_fraction=_decimal(annualized_turnover),
        risk_contributions=_risk_contributions(selected, weights=weights),
    )


def _simulate_economic_window(
    rows: tuple[_MonthlyEconomicRow, ...],
    *,
    start: date,
    end: date,
    weights: Mapping[EconomicExposure, float],
    rebalance_band: float,
    minimum_observation_count: int = 24,
) -> tuple[tuple[float, ...], float, float, float]:
    selected = tuple(item for item in rows if start <= item.month < end)
    if len(selected) < minimum_observation_count:
        raise ValueError("Reference 经济评价窗口共同月度观测不足")
    holdings = dict(weights)
    equity = sum(holdings.values())
    if abs(equity - 1) > 1e-9:
        raise ValueError("Reference 经济暴露之和必须为 1")
    peak = equity
    maximum_drawdown = 0.0
    turnover = sum(
        value for exposure, value in weights.items() if exposure != EconomicExposure.CASH
    )
    monthly_returns: list[float] = []
    for row in selected:
        before = equity
        for exposure in tuple(holdings):
            if exposure != EconomicExposure.CASH:
                holdings[exposure] *= 1 + row.returns[exposure]
        equity = sum(holdings.values())
        current_weights = {
            exposure: value / equity for exposure, value in holdings.items()
        }
        if max(
            abs(current_weights[exposure] - target)
            for exposure, target in weights.items()
        ) > rebalance_band:
            turnover += sum(
                abs(target * equity - holdings[exposure]) / equity
                for exposure, target in weights.items()
                if exposure != EconomicExposure.CASH
            )
            holdings = {
                exposure: target * equity for exposure, target in weights.items()
            }
        monthly_returns.append(equity / before - 1)
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, 1 - equity / peak)
    return (
        tuple(monthly_returns),
        maximum_drawdown,
        turnover / (len(selected) / 12),
        equity,
    )


def _risk_contributions(
    rows: tuple[_MonthlyEconomicRow, ...],
    *,
    weights: Mapping[EconomicExposure, float],
) -> tuple[ReferenceRiskContribution, ...]:
    exposures = tuple(sorted(set(weights) - {EconomicExposure.CASH}))
    samples = {
        exposure: tuple(item.returns[exposure] for item in rows)
        for exposure in exposures
    }
    means = {
        exposure: sum(values) / len(values) for exposure, values in samples.items()
    }
    covariance: dict[tuple[EconomicExposure, EconomicExposure], float] = {}
    for left in exposures:
        for right in exposures:
            covariance[(left, right)] = sum(
                (a - means[left]) * (b - means[right])
                for a, b in zip(samples[left], samples[right], strict=True)
            ) / (len(rows) - 1)
    variance = sum(
        weights[left] * weights[right] * covariance[(left, right)]
        for left in exposures
        for right in exposures
    )
    if variance <= 0:
        raise ValueError("Reference 经济代理组合方差必须为正")
    return tuple(
        ReferenceRiskContribution(
            exposure=exposure,
            fraction=_decimal(
                weights[exposure]
                * sum(
                    covariance[(exposure, other)] * weights[other]
                    for other in exposures
                )
                / variance
            ),
        )
        for exposure in exposures
    )


def _next_month(value: date) -> date:
    return (
        date(value.year + 1, 1, 1)
        if value.month == 12
        else date(value.year, value.month + 1, 1)
    )


def _monthly_levels(dataset: HistoricalEconomicSeriesDataset) -> dict[date, float]:
    if (
        dataset.manifest.kind != EconomicSeriesKind.PRICE_LEVEL
        or dataset.manifest.frequency != EconomicSeriesFrequency.MONTHLY
    ):
        raise ValueError("Reference 购买力序列必须是月度水平序列")
    return {item.effective_date: float(item.value) for item in dataset.observations}


def _inflation_growth(
    levels: Mapping[date, float],
    *,
    start: date,
    end: date,
) -> float:
    # Returns dated in month M describe the move from the prior month-end into M.
    # A deflator published exactly at the window start is therefore the correct
    # purchasing-power base; the end remains exclusive like the return window.
    starts = tuple(item for item in levels if item <= start)
    ends = tuple(item for item in levels if item < end)
    if not starts or not ends:
        raise ValueError("Reference 购买力序列不能覆盖评价窗口端点")
    start_date = max(starts)
    end_date = max(ends)
    if end_date <= start_date:
        raise ValueError("Reference 购买力评价窗口没有正向时间跨度")
    return levels[end_date] / levels[start_date]


def _decimal(value: float) -> Decimal:
    return Decimal(str(value))


def collect_reference_evidence(
    plan: ReferenceSelectionPlan,
    *,
    instruments: Mapping[str, InstrumentId],
    information_cutoff: date,
    economic_catalog: Path,
    product_catalog: Path,
    funding_catalog: Path,
    quote_catalog: Path,
) -> tuple[ReferenceSelectionEvidence, ...]:
    """Resolve the strongest frozen artifact per requirement; never infer missing layers."""

    evidence: list[ReferenceSelectionEvidence] = []
    for requirement in plan.evidence_requirements:
        found = _evidence_for_requirement(
            requirement.layer,
            requirement.scope,
            fixed_evidence_id=requirement.fixed_evidence_id,
            instruments=instruments,
            information_cutoff=information_cutoff,
            economic_catalog=economic_catalog,
            product_catalog=product_catalog,
            funding_catalog=funding_catalog,
            quote_catalog=quote_catalog,
        )
        if found is not None:
            evidence.append(found)
    return tuple(sorted(evidence, key=lambda item: (item.layer, item.scope, item.evidence_id)))


def _evidence_for_requirement(
    layer: ReferenceEvidenceLayer,
    scope: str,
    *,
    fixed_evidence_id: str | None,
    instruments: Mapping[str, InstrumentId],
    information_cutoff: date,
    economic_catalog: Path,
    product_catalog: Path,
    funding_catalog: Path,
    quote_catalog: Path,
) -> ReferenceSelectionEvidence | None:
    if layer in {
        ReferenceEvidenceLayer.ECONOMIC_PROXY,
        ReferenceEvidenceLayer.OBJECTIVE_DEFLATOR,
    }:
        return _economic_evidence(
            layer=layer,
            scope=scope,
            fixed_evidence_id=fixed_evidence_id,
            information_cutoff=information_cutoff,
            catalog=economic_catalog,
        )
    instrument = instruments.get(scope)
    if instrument is None:
        raise ValueError(f"Reference 证据作用域不属于当前可投资产品: {scope}")
    if layer == ReferenceEvidenceLayer.PRODUCT_BARS:
        return _product_bar_evidence(
            instrument=instrument,
            information_cutoff=information_cutoff,
            catalog=product_catalog,
        )
    if layer == ReferenceEvidenceLayer.EXECUTABLE_QUOTES:
        return _quote_evidence(
            instrument=instrument,
            information_cutoff=information_cutoff,
            catalog=quote_catalog,
        )
    if layer == ReferenceEvidenceLayer.PRODUCT_CASH_FLOWS:
        return _funding_evidence(
            instrument=instrument,
            information_cutoff=information_cutoff,
            catalog=funding_catalog,
        )
    if layer == ReferenceEvidenceLayer.PRODUCT_RULES:
        return None
    raise AssertionError(f"未处理 Reference 证据层: {layer}")


def _economic_evidence(
    *,
    layer: ReferenceEvidenceLayer,
    scope: str,
    fixed_evidence_id: str | None,
    information_cutoff: date,
    catalog: Path,
) -> ReferenceSelectionEvidence | None:
    if fixed_evidence_id is None:
        raise ValueError("经济代理与购买力口径必须预先固定数据集身份")
    target = catalog / fixed_evidence_id
    if not target.exists():
        return None
    dataset = HistoricalEconomicSeriesCatalog(catalog).load(fixed_evidence_id)
    manifest = dataset.manifest
    expected_role = (
        EconomicSeriesRole.EXPOSURE_PROXY
        if layer == ReferenceEvidenceLayer.ECONOMIC_PROXY
        else EconomicSeriesRole.OBJECTIVE_DEFLATOR
    )
    if manifest.role != expected_role:
        raise ValueError("Reference 固定经济数据角色与要求不一致")
    if layer == ReferenceEvidenceLayer.ECONOMIC_PROXY and (
        manifest.economic_exposure is None
        or manifest.economic_exposure.value != scope
    ):
        raise ValueError("Reference 固定经济代理与风险暴露不一致")
    if manifest.last_effective_date > information_cutoff:
        return None
    return ReferenceSelectionEvidence(
        layer=layer,
        scope=scope,
        evidence_id=manifest.dataset_id,
        content_hash=content_hash(manifest),
        first_effective_date=manifest.first_effective_date,
        last_effective_date=manifest.last_effective_date,
        observation_count=manifest.observation_count,
    )


def _product_bar_evidence(
    *,
    instrument: InstrumentId,
    information_cutoff: date,
    catalog: Path,
) -> ReferenceSelectionEvidence | None:
    expected_source = (
        "binance-rest-historical"
        if instrument.product == InstrumentProduct.SPOT
        else "binance-usdm-rest-historical"
    )
    candidates: list[ReferenceSelectionEvidence] = []
    for dataset_id in _dataset_ids(catalog):
        manifest = HistoricalDatasetManifest.model_validate_json(
            (catalog / dataset_id / "manifest.json").read_text(encoding="utf-8")
        )
        if (
            manifest.symbol != instrument.symbol
            or manifest.source != expected_source
            or manifest.interval != "1d"
            or manifest.last_close_time.date() > information_cutoff
        ):
            continue
        candidates.append(
            ReferenceSelectionEvidence(
                layer=ReferenceEvidenceLayer.PRODUCT_BARS,
                scope=instrument.key,
                evidence_id=manifest.dataset_id,
                content_hash=content_hash(manifest),
                first_effective_date=manifest.first_open_time.date(),
                last_effective_date=manifest.last_close_time.date(),
                observation_count=manifest.bar_count,
            )
        )
    strongest = _strongest(candidates)
    if strongest is not None:
        HistoricalDatasetCatalog(catalog).load(strongest.evidence_id)
    return strongest


def _quote_evidence(
    *,
    instrument: InstrumentId,
    information_cutoff: date,
    catalog: Path,
) -> ReferenceSelectionEvidence | None:
    candidates: list[ReferenceSelectionEvidence] = []
    for dataset_id in _dataset_ids(catalog):
        manifest = HistoricalExecutableQuoteManifest.model_validate_json(
            (catalog / dataset_id / "manifest.json").read_text(encoding="utf-8")
        )
        if (
            manifest.instrument != instrument
            or manifest.last_observed_at.date() > information_cutoff
        ):
            continue
        candidates.append(
            ReferenceSelectionEvidence(
                layer=ReferenceEvidenceLayer.EXECUTABLE_QUOTES,
                scope=instrument.key,
                evidence_id=manifest.dataset_id,
                content_hash=content_hash(manifest),
                first_effective_date=manifest.first_observed_at.date(),
                last_effective_date=manifest.last_observed_at.date(),
                observation_count=manifest.quote_count,
            )
        )
    strongest = _strongest(candidates)
    if strongest is not None:
        HistoricalExecutableQuoteCatalog(catalog).load(strongest.evidence_id)
    return strongest


def _funding_evidence(
    *,
    instrument: InstrumentId,
    information_cutoff: date,
    catalog: Path,
) -> ReferenceSelectionEvidence | None:
    if instrument.product == InstrumentProduct.SPOT:
        raise ValueError("Spot 产品不得伪造资金费率现金流")
    candidates: list[ReferenceSelectionEvidence] = []
    for dataset_id in _dataset_ids(catalog):
        manifest = HistoricalFundingDatasetManifest.model_validate_json(
            (catalog / dataset_id / "manifest.json").read_text(encoding="utf-8")
        )
        if (
            manifest.symbol != instrument.symbol
            or manifest.last_available_at.date() > information_cutoff
        ):
            continue
        candidates.append(
            ReferenceSelectionEvidence(
                layer=ReferenceEvidenceLayer.PRODUCT_CASH_FLOWS,
                scope=instrument.key,
                evidence_id=manifest.dataset_id,
                content_hash=content_hash(manifest),
                first_effective_date=manifest.first_available_at.date(),
                last_effective_date=manifest.last_available_at.date(),
                observation_count=manifest.observation_count,
            )
        )
    strongest = _strongest(candidates)
    if strongest is not None:
        HistoricalFundingDatasetCatalog(catalog).load(strongest.evidence_id)
    return strongest


def _dataset_ids(catalog: Path) -> tuple[str, ...]:
    if not catalog.exists():
        return ()
    return tuple(
        path.name
        for path in sorted(catalog.iterdir())
        if path.is_dir() and (path / "manifest.json").is_file()
    )


def _strongest(
    candidates: Iterable[ReferenceSelectionEvidence],
) -> ReferenceSelectionEvidence | None:
    values = tuple(candidates)
    if not values:
        return None
    return max(
        values,
        key=lambda item: (
            (item.last_effective_date - item.first_effective_date).days,
            item.observation_count,
            item.last_effective_date,
            item.evidence_id,
        ),
    )
