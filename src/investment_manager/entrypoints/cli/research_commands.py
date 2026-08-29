from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated

import typer

from investment_manager.entrypoints.cli.research_root import app
from investment_manager.entrypoints.cli.support import (
    observed_market_instruments as _observed_market_instruments,
)
from investment_manager.entrypoints.cli.support import (
    parse_research_symbol as _parse_research_symbol,
)
from investment_manager.entrypoints.cli.support import (
    parse_utc_option as _parse_utc_option,
)
from investment_manager.entrypoints.cli.support import (
    runtime_engine as _runtime_engine,
)
from investment_manager.governance.models import (
    committed_file_revision,
    current_clean_code_version,
)
from investment_manager.kernel.identity import content_hash
from investment_manager.settings import load_config


@app.command("record-reference-rejection")
def record_reference_rejection_command(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    plan: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    information_cutoff: Annotated[str, typer.Option(help="YYYY-MM-DD 信息截止日")],
    project_root: Annotated[Path, typer.Option(exists=True, file_okay=False)] = Path(
        "."
    ),
    economic_catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/economic-series"
    ),
    product_catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/datasets"
    ),
    funding_catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/funding-datasets"
    ),
    quote_catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/quote-datasets"
    ),
    result_catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        "evidence/reference-selections"
    ),
) -> None:
    """记录 Reference 经济或产品证据失败的不可变拒绝；不能授予资格。"""

    from investment_manager.governance.evaluation.reference_selection import (
        ReferencePlanRegistration,
        ReferenceSelectionCatalog,
        build_reference_rejection,
        load_reference_selection_plan,
    )
    from investment_manager.research.reference import (
        collect_reference_evidence,
        evaluate_reference_economics,
        persist_reference_evidence_manifests,
    )

    try:
        cutoff = date.fromisoformat(information_cutoff)
    except ValueError as exc:
        raise typer.BadParameter("information-cutoff 必须是 YYYY-MM-DD") from exc
    evaluated_at = datetime.now(UTC)
    if cutoff > evaluated_at.date():
        raise typer.BadParameter("information-cutoff 不能晚于当前 UTC 日期")
    loaded = load_config(config)
    root = project_root.resolve()
    registered = load_reference_selection_plan(plan)
    registration_commit, registration_time = committed_file_revision(
        plan,
        repository_root=root,
    )
    plan_registration = ReferencePlanRegistration(
        repository_path=plan.resolve().relative_to(root).as_posix(),
        commit=registration_commit,
        committed_at=registration_time,
        plan_hash=content_hash(registered),
    )
    evaluator_code_version = current_clean_code_version(repository_root=root)
    instruments = {
        item.instrument.key: item.instrument for item in loaded.capital.execution_specs
    }
    evidence = collect_reference_evidence(
        registered,
        instruments=instruments,
        information_cutoff=cutoff,
        economic_catalog=economic_catalog,
        product_catalog=product_catalog,
        funding_catalog=funding_catalog,
        quote_catalog=quote_catalog,
    )
    economics = evaluate_reference_economics(
        registered,
        economic_catalog=economic_catalog,
        exposure_by_implementation={
            item.instrument_key: item.economic_exposure
            for item in loaded.capital.investable_universe.instruments
        },
    )
    catalog = ReferenceSelectionCatalog(result_catalog)
    artifact = catalog.rejection(
        plan_hash=content_hash(registered),
        plan_registration=plan_registration,
        evaluator_code_version=evaluator_code_version,
        information_cutoff=cutoff,
        evidence=evidence,
        economic_development_metrics=economics.development,
        economic_blind_metrics=economics.blind,
        economic_stress_results=economics.stress,
    )
    if artifact is None:
        artifact = build_reference_rejection(
            plan=registered,
            plan_registration=plan_registration,
            evaluator_code_version=evaluator_code_version,
            evidence=evidence,
            evaluated_at=evaluated_at,
            information_cutoff=cutoff,
            economic_development_metrics=economics.development,
            economic_blind_metrics=economics.blind,
            economic_stress_results=economics.stress,
        )
    target = catalog.store(artifact)
    manifest_paths = persist_reference_evidence_manifests(
        evidence,
        economic_catalog=economic_catalog,
        product_catalog=product_catalog,
        funding_catalog=funding_catalog,
        quote_catalog=quote_catalog,
        target_root=result_catalog / artifact.artifact_id / "evidence-manifests",
    )
    typer.echo(
        json.dumps(
            {
                "artifact_id": artifact.artifact_id,
                "status": artifact.status,
                "evidence_count": len(artifact.evidence),
                "reason_codes": artifact.results[0].reason_codes,
                "path": str(target),
                "evidence_manifests": [str(path) for path in manifest_paths],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("freeze-executable-quotes")
def freeze_executable_quotes_command(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[str, typer.Option(envvar="INVESTMENT_MANAGER_DATABASE_URL")],
    instrument_key: Annotated[str, typer.Option()],
    start: Annotated[str, typer.Option(help="带时区的 ISO-8601 起点（含）")],
    end: Annotated[str, typer.Option(help="带时区的 ISO-8601 终点（不含）")],
    sampling_interval_seconds: Annotated[
        int,
        typer.Option(min=1, help="每个 UTC 时间桶只冻结首个可执行报价"),
    ] = 300,
    catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/quote-datasets"
    ),
) -> None:
    """从追加行情事实冻结压缩后的双边可执行报价；不新增采集路径。"""

    from investment_manager.research.quote_dataset import (
        HistoricalExecutableQuoteCatalog,
        freeze_executable_quotes,
    )

    loaded = load_config(config)
    specs = tuple(
        item
        for item in _observed_market_instruments(loaded)
        if item.key == instrument_key
    )
    if len(specs) != 1:
        raise typer.BadParameter(
            "instrument-key 必须唯一属于当前 Market 观测产品"
        )
    engine = _runtime_engine(database_url)
    try:
        dataset = freeze_executable_quotes(
            engine,
            instrument=specs[0],
            start=_parse_utc_option(start, name="start"),
            end=_parse_utc_option(end, name="end"),
            sampling_interval_seconds=sampling_interval_seconds,
        )
        target = HistoricalExecutableQuoteCatalog(catalog).store(dataset)
    finally:
        engine.dispose()
    manifest = dataset.manifest
    typer.echo(
        json.dumps(
            {
                "dataset_id": manifest.dataset_id,
                "instrument_key": manifest.instrument.key,
                "source_row_count": manifest.source_row_count,
                "quote_count": manifest.quote_count,
                "first_observed_at": manifest.first_observed_at.isoformat(),
                "last_observed_at": manifest.last_observed_at.isoformat(),
                "sampling_interval_seconds": manifest.sampling_interval_seconds,
                "quotes_hash": manifest.quotes_hash,
                "path": str(target),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("freeze-reference-product-mapping")
def freeze_reference_product_mapping_command(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    database_url: Annotated[str, typer.Option(envvar="INVESTMENT_MANAGER_DATABASE_URL")],
    instrument_key: Annotated[str, typer.Option()],
    start: Annotated[str, typer.Option(help="带时区的 ISO-8601 起点（含）")],
    end: Annotated[str, typer.Option(help="带时区的 ISO-8601 终点（不含）")],
    reference_calculation_type: Annotated[str, typer.Option()],
    reference_external_calculation_id: Annotated[int, typer.Option(min=1)],
    sampling_interval_seconds: Annotated[int, typer.Option(min=1)] = 60,
    maximum_reference_age_ms: Annotated[int, typer.Option(min=1)] = 3_000,
    catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/reference-product-mappings"
    ),
) -> None:
    """冻结候选 Spot 报价与官方同步 ReferencePrice 的点时映射证据。"""

    from investment_manager.market.models import InstrumentProduct
    from investment_manager.research.reference_product_mapping import (
        freeze_reference_product_mapping,
        store_reference_product_mapping,
    )

    loaded = load_config(config)
    instruments = tuple(
        item
        for item in _observed_market_instruments(loaded)
        if item.key == instrument_key
    )
    if len(instruments) != 1 or instruments[0].product != InstrumentProduct.SPOT:
        raise typer.BadParameter("instrument-key 必须唯一属于 Market Spot 观测产品")
    instrument = instruments[0]
    if instrument.symbol not in loaded.market_data.reference_price_symbols:
        raise typer.BadParameter("候选未启用官方 ReferencePrice 观测")
    engine = _runtime_engine(database_url)
    try:
        artifact = freeze_reference_product_mapping(
            engine,
            instrument=instrument,
            start=_parse_utc_option(start, name="start"),
            end=_parse_utc_option(end, name="end"),
            sampling_interval_seconds=sampling_interval_seconds,
            maximum_reference_age_ms=maximum_reference_age_ms,
            reference_contract=(
                "https://developers.binance.com/docs/binance-spot-api-docs/"
                "rest-api/market-data-endpoints#reference-price"
            ),
            reference_calculation_type=reference_calculation_type,
            reference_external_calculation_id=reference_external_calculation_id,
            captured_at=datetime.now(UTC),
        )
        target = store_reference_product_mapping(artifact, root=catalog)
    finally:
        engine.dispose()
    typer.echo(
        json.dumps(
            {
                "artifact_id": artifact.artifact_id,
                "instrument_key": artifact.instrument.key,
                "sampled_quote_count": artifact.sampled_quote_count,
                "matched_quote_count": artifact.matched_quote_count,
                "matched_fraction": str(artifact.matched_fraction),
                "mean_absolute_mid_premium_bps": str(
                    artifact.mean_absolute_mid_premium_bps
                ),
                "maximum_absolute_mid_premium_bps": str(
                    artifact.maximum_absolute_mid_premium_bps
                ),
                "mean_spread_bps": str(artifact.mean_spread_bps),
                "minimum_bid_top_notional": str(
                    artifact.minimum_bid_top_notional
                ),
                "minimum_ask_top_notional": str(
                    artifact.minimum_ask_top_notional
                ),
                "path": str(target),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("fetch-economic-series")
def fetch_economic_series_command(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    series: Annotated[
        str,
        typer.Option(
            help=(
                "US_EQUITY_TOTAL_RETURN、US_TBILL_TOTAL_RETURN、"
                "GOLD_USD_PRICE 或 US_CPI_DEFLATOR"
            )
        ),
    ],
    catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/economic-series"
    ),
) -> None:
    """冻结一手长期经济代理；不把代理历史冒充可交易产品历史。"""

    from investment_manager.research.economic_series import (
        HistoricalEconomicSeriesCatalog,
        fetch_fama_french_us_market_returns,
        fetch_fama_french_us_one_month_tbill_returns,
        fetch_fred_us_cpi,
        fetch_world_bank_gold_prices,
    )

    loaded = load_config(config)
    fetchers = {
        "US_EQUITY_TOTAL_RETURN": fetch_fama_french_us_market_returns,
        "US_TBILL_TOTAL_RETURN": fetch_fama_french_us_one_month_tbill_returns,
        "GOLD_USD_PRICE": fetch_world_bank_gold_prices,
        "US_CPI_DEFLATOR": fetch_fred_us_cpi,
    }
    normalized = series.strip().upper()
    fetcher = fetchers.get(normalized)
    if fetcher is None:
        raise typer.BadParameter(
            "series 只允许 US_EQUITY_TOTAL_RETURN、US_TBILL_TOTAL_RETURN、"
            "GOLD_USD_PRICE 或 US_CPI_DEFLATOR"
        )
    dataset = asyncio.run(
        fetcher(timeout_seconds=loaded.market_data.rest_timeout_seconds)
    )
    target = HistoricalEconomicSeriesCatalog(catalog).store(dataset)
    manifest = dataset.manifest
    typer.echo(
        json.dumps(
            {
                "dataset_id": manifest.dataset_id,
                "series_id": manifest.series_id,
                "economic_exposure": manifest.economic_exposure,
                "vintage_policy": manifest.vintage_policy,
                "observation_count": manifest.observation_count,
                "first_effective_date": manifest.first_effective_date.isoformat(),
                "last_effective_date": manifest.last_effective_date.isoformat(),
                "source_sha256": manifest.source_sha256,
                "observations_hash": manifest.observations_hash,
                "path": str(target),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("fetch-binance-history")
def fetch_binance_history_command(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    symbol: Annotated[str, typer.Option()],
    start: Annotated[str, typer.Option(help="带时区的 ISO-8601 起点（含）")],
    end: Annotated[str, typer.Option(help="带时区的 ISO-8601 终点（不含）")],
    interval: Annotated[
        str | None,
        typer.Option(help="研究 K 线周期；省略时沿用 MarketDataPolicy"),
    ] = None,
    catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(".runtime/datasets"),
) -> None:
    """抓取并内容寻址保存 Binance 官方已收盘 K 线；不生成 Markdown 报告。"""

    from investment_manager.research.dataset import (
        HistoricalDatasetCatalog,
        fetch_binance_history,
    )

    loaded = load_config(config)
    canonical_symbol = _parse_research_symbol(symbol)
    dataset = asyncio.run(
        fetch_binance_history(
            base_url=loaded.market_data.rest_base_url,
            symbol=canonical_symbol,
            interval=interval or loaded.market_data.interval,
            start=_parse_utc_option(start, name="start"),
            end=_parse_utc_option(end, name="end"),
            timeout_seconds=loaded.market_data.rest_timeout_seconds,
        )
    )
    target = HistoricalDatasetCatalog(catalog).store(dataset)
    typer.echo(
        json.dumps(
            {
                "dataset_id": dataset.manifest.dataset_id,
                "symbol": dataset.manifest.symbol,
                "interval": dataset.manifest.interval,
                "bar_count": dataset.manifest.bar_count,
                "first_open_time": dataset.manifest.first_open_time.isoformat(),
                "last_close_time": dataset.manifest.last_close_time.isoformat(),
                "bars_hash": dataset.manifest.bars_hash,
                "path": str(target),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("fetch-binance-usdm-history")
def fetch_binance_usdm_history_command(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    symbol: Annotated[str, typer.Option()],
    start: Annotated[str, typer.Option(help="带时区的 ISO-8601 起点（含）")],
    end: Annotated[str, typer.Option(help="带时区的 ISO-8601 终点（不含）")],
    interval: Annotated[str, typer.Option(help="USD-M 合约交易价 K 线周期")],
    catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(".runtime/datasets"),
) -> None:
    """冻结 Binance USD-M 合约交易价 K 线；不把成交价冒充 bid/ask。"""

    from investment_manager.research.dataset import (
        HistoricalDatasetCatalog,
        fetch_binance_usdm_history,
    )

    loaded = load_config(config)
    canonical_symbol = _parse_research_symbol(symbol)
    dataset = asyncio.run(
        fetch_binance_usdm_history(
            base_url="https://fapi.binance.com",
            symbol=canonical_symbol,
            interval=interval,
            start=_parse_utc_option(start, name="start"),
            end=_parse_utc_option(end, name="end"),
            timeout_seconds=loaded.market_data.rest_timeout_seconds,
        )
    )
    target = HistoricalDatasetCatalog(catalog).store(dataset)
    typer.echo(
        json.dumps(
            {
                "dataset_id": dataset.manifest.dataset_id,
                "source": dataset.manifest.source,
                "symbol": dataset.manifest.symbol,
                "interval": dataset.manifest.interval,
                "bar_count": dataset.manifest.bar_count,
                "first_open_time": dataset.manifest.first_open_time.isoformat(),
                "last_close_time": dataset.manifest.last_close_time.isoformat(),
                "bars_hash": dataset.manifest.bars_hash,
                "path": str(target),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("fetch-binance-funding-history")
def fetch_binance_funding_history_command(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    symbol: Annotated[str, typer.Option()],
    start: Annotated[str, typer.Option(help="带时区的 ISO-8601 起点（含）")],
    end: Annotated[str, typer.Option(help="带时区的 ISO-8601 终点（不含）")],
    catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/funding-datasets"
    ),
) -> None:
    """冻结 Binance 官方校验的 USD-M 资金费率；不调用模型或生成报告。"""

    from investment_manager.research.dataset import (
        HistoricalFundingDatasetCatalog,
        fetch_binance_funding_history,
    )

    loaded = load_config(config)
    canonical_symbol = _parse_research_symbol(symbol)
    dataset = asyncio.run(
        fetch_binance_funding_history(
            base_url="https://data.binance.vision",
            verification_base_url="https://fapi.binance.com",
            symbol=canonical_symbol,
            start=_parse_utc_option(start, name="start"),
            end=_parse_utc_option(end, name="end"),
            timeout_seconds=loaded.market_data.rest_timeout_seconds,
        )
    )
    target = HistoricalFundingDatasetCatalog(catalog).store(dataset)
    typer.echo(
        json.dumps(
            {
                "dataset_id": dataset.manifest.dataset_id,
                "symbol": dataset.manifest.symbol,
                "observation_count": dataset.manifest.observation_count,
                "first_available_at": dataset.manifest.first_available_at.isoformat(),
                "last_available_at": dataset.manifest.last_available_at.isoformat(),
                "observations_hash": dataset.manifest.observations_hash,
                "source_artifact_count": len(dataset.manifest.source_artifacts),
                "path": str(target),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("fetch-binance-carry-history")
def fetch_binance_carry_history_command(
    config: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    spot_dataset_id: Annotated[str, typer.Option()],
    funding_dataset_id: Annotated[str | None, typer.Option()] = None,
    spot_catalog: Annotated[Path, typer.Option(exists=True, file_okay=False)] = Path(
        ".runtime/datasets"
    ),
    funding_catalog: Annotated[
        Path, typer.Option(exists=True, file_okay=False)
    ] = Path(".runtime/funding-datasets"),
    carry_catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/carry-datasets"
    ),
) -> None:
    """冻结 carry 的 USD-M 价格；需要时同时校验逐次 funding。"""

    from investment_manager.research.carry import (
        HistoricalCarryDatasetCatalog,
        fetch_binance_carry_history,
    )
    from investment_manager.research.dataset import (
        HistoricalDatasetCatalog,
        HistoricalFundingDatasetCatalog,
    )

    loaded = load_config(config)
    spot_dataset = HistoricalDatasetCatalog(spot_catalog).load(spot_dataset_id)
    funding_dataset = (
        None
        if funding_dataset_id is None
        else HistoricalFundingDatasetCatalog(funding_catalog).load(funding_dataset_id)
    )
    try:
        dataset = asyncio.run(
            fetch_binance_carry_history(
                base_url="https://fapi.binance.com",
                spot_dataset=spot_dataset,
                funding_dataset=funding_dataset,
                timeout_seconds=loaded.market_data.rest_timeout_seconds,
            )
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="spot-dataset-id") from exc
    target = HistoricalCarryDatasetCatalog(carry_catalog).store(dataset)
    typer.echo(
        json.dumps(
            {
                "carry_dataset_id": dataset.manifest.dataset_id,
                "symbol": dataset.manifest.symbol,
                "interval": dataset.manifest.interval,
                "bar_count": dataset.manifest.bar_count,
                "settlement_count": dataset.manifest.settlement_count,
                "bars_hash": dataset.manifest.bars_hash,
                "settlements_hash": dataset.manifest.settlements_hash,
                "rule_snapshot_as_of": dataset.manifest.collected_at.isoformat(),
                "path": str(target),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("freeze-event-history")
def freeze_event_history_command(
    database_url: Annotated[
        str,
        typer.Option(envvar="INVESTMENT_MANAGER_DATABASE_URL", help="点时事件事实库"),
    ],
    start: Annotated[str, typer.Option(help="按 observed_at 过滤的含时区起点（含）")],
    end: Annotated[str, typer.Option(help="按 observed_at 过滤的含时区终点（不含）")],
    catalog: Annotated[Path, typer.Option(file_okay=False)] = Path(
        ".runtime/event-datasets"
    ),
) -> None:
    """冻结真实到达时间的标准事件；不为事后新闻猜测 observed_at。"""

    from sqlalchemy import select

    from investment_manager.information.models import IntelligenceEvent
    from investment_manager.information.tables import normalized_events
    from investment_manager.research.dataset import (
        HistoricalEventDatasetCatalog,
        freeze_historical_events,
    )

    window_start = _parse_utc_option(start, name="start")
    window_end = _parse_utc_option(end, name="end")
    if window_start >= window_end:
        raise typer.BadParameter("start 必须早于 end")
    frozen_at = datetime.now(UTC)
    if window_end > frozen_at:
        raise typer.BadParameter("end 不能晚于当前冻结时间", param_hint="end")
    engine = _runtime_engine(database_url)
    with engine.connect() as connection:
        rows = tuple(
            connection.execute(
                select(normalized_events.c.payload)
                .where(
                    normalized_events.c.observed_at >= window_start,
                    normalized_events.c.observed_at < window_end,
                )
                .order_by(
                    normalized_events.c.observed_at,
                    normalized_events.c.evidence_id,
                )
            ).scalars()
        )
    dataset = freeze_historical_events(
        events=(IntelligenceEvent.model_validate(item) for item in rows),
        source="investment-manager-normalized-events",
        requested_start=window_start,
        requested_end=window_end,
        collected_at=frozen_at,
    )
    target = HistoricalEventDatasetCatalog(catalog).store(dataset)
    typer.echo(
        json.dumps(
            {
                "event_dataset_id": dataset.manifest.dataset_id,
                "event_count": dataset.manifest.event_count,
                "requested_start": dataset.manifest.requested_start.isoformat(),
                "requested_end": dataset.manifest.requested_end.isoformat(),
                "events_hash": dataset.manifest.events_hash,
                "path": str(target),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
