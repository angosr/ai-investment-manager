from __future__ import annotations

import asyncio
import io
import json
import zipfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from investment_manager.portfolio.policy import EconomicExposure
from investment_manager.research.economic_series import (
    FAMA_FRENCH_DAILY_URL,
    FRED_CPI_URL,
    WORLD_BANK_COMMODITY_PAGE_URL,
    EconomicSeriesKind,
    EconomicSeriesRole,
    EconomicSeriesVintagePolicy,
    HistoricalEconomicSeriesCatalog,
    fetch_fama_french_us_market_returns,
    fetch_fama_french_us_one_month_tbill_returns,
    fetch_fred_us_cpi,
    fetch_world_bank_gold_prices,
)


def test_fama_french_history_freezes_total_market_return_without_pretending_vintages(
    tmp_path: Path,
) -> None:
    payload = _fama_zip()

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == FAMA_FRENCH_DAILY_URL
        return httpx.Response(200, content=payload)

    dataset = asyncio.run(
        fetch_fama_french_us_market_returns(
            timeout_seconds=5,
            clock=lambda: datetime(2011, 1, 1, tzinfo=UTC),
            transport=httpx.MockTransport(handler),
        )
    )

    assert dataset.manifest.economic_exposure == EconomicExposure.US_EQUITY
    assert dataset.manifest.role == EconomicSeriesRole.EXPOSURE_PROXY
    assert dataset.manifest.kind == EconomicSeriesKind.TOTAL_RETURN
    assert dataset.manifest.vintage_policy == (
        EconomicSeriesVintagePolicy.CURRENT_VINTAGE_AT_COLLECTION
    )
    assert dataset.observations[0].value == Decimal("0.001")
    catalog = HistoricalEconomicSeriesCatalog(tmp_path)
    target = catalog.store(dataset)
    assert catalog.load(dataset.manifest.dataset_id) == dataset

    observations = json.loads((target / "observations.json").read_text(encoding="utf-8"))
    observations[0][1] = "0.9"
    (target / "observations.json").write_text(
        json.dumps(observations),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="哈希"):
        catalog.load(dataset.manifest.dataset_id)


def test_fama_french_history_exposes_tbill_return_as_cash_not_equity() -> None:
    payload = _fama_zip()

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == FAMA_FRENCH_DAILY_URL
        return httpx.Response(200, content=payload)

    dataset = asyncio.run(
        fetch_fama_french_us_one_month_tbill_returns(
            timeout_seconds=5,
            clock=lambda: datetime(2011, 1, 1, tzinfo=UTC),
            transport=httpx.MockTransport(handler),
        )
    )

    assert dataset.manifest.economic_exposure == EconomicExposure.CASH
    assert dataset.manifest.kind == EconomicSeriesKind.TOTAL_RETURN
    assert dataset.observations[0].value == Decimal("0.0001")


def test_world_bank_page_resolves_one_current_gold_workbook() -> None:
    workbook_url = (
        "https://thedocs.worldbank.org/en/doc/version/related/"
        "CMO-Historical-Data-Monthly.xlsx"
    )
    workbook = _world_bank_gold_xlsx()
    page = f'<a href="{workbook_url}">Monthly prices</a>'.encode()

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == WORLD_BANK_COMMODITY_PAGE_URL:
            return httpx.Response(200, content=page)
        assert str(request.url) == workbook_url
        return httpx.Response(200, content=workbook)

    dataset = asyncio.run(
        fetch_world_bank_gold_prices(
            timeout_seconds=5,
            clock=lambda: datetime(2011, 1, 1, tzinfo=UTC),
            transport=httpx.MockTransport(handler),
        )
    )

    assert dataset.manifest.economic_exposure == EconomicExposure.INFLATION_SENSITIVE
    assert dataset.manifest.kind == EconomicSeriesKind.PRICE_LEVEL
    assert dataset.manifest.source_url == workbook_url
    assert dataset.manifest.observation_count == 120
    assert dataset.observations[0].effective_date == date(2000, 1, 1)
    assert dataset.observations[-1].effective_date == date(2009, 12, 1)
    assert dataset.observations[-1].value == Decimal("154.75")


def test_world_bank_source_discovery_fails_closed_on_ambiguous_workbooks() -> None:
    links = "".join(
        f'<a href="https://thedocs.worldbank.org/{name}/CMO-Historical-Data-Monthly.xlsx">x</a>'
        for name in ("one", "two")
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == WORLD_BANK_COMMODITY_PAGE_URL
        return httpx.Response(200, content=links)

    with pytest.raises(ValueError, match="未唯一指向"):
        asyncio.run(
            fetch_world_bank_gold_prices(
                timeout_seconds=5,
                clock=lambda: datetime(2011, 1, 1, tzinfo=UTC),
                transport=httpx.MockTransport(handler),
            )
        )


def test_fred_cpi_is_an_objective_deflator_not_an_investable_exposure() -> None:
    rows = ["observation_date,CPIAUCSL"]
    for index in range(120):
        year = 2000 + index // 12
        month = index % 12 + 1
        rows.append(f"{year:04d}-{month:02d}-01,{100 + Decimal(index) / 10}")
    payload = "\n".join(rows).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == FRED_CPI_URL
        return httpx.Response(200, content=payload)

    dataset = asyncio.run(
        fetch_fred_us_cpi(
            timeout_seconds=5,
            clock=lambda: datetime(2011, 1, 1, tzinfo=UTC),
            transport=httpx.MockTransport(handler),
        )
    )

    assert dataset.manifest.role == EconomicSeriesRole.OBJECTIVE_DEFLATOR
    assert dataset.manifest.economic_exposure is None
    assert dataset.manifest.kind == EconomicSeriesKind.PRICE_LEVEL
    assert dataset.observations[-1].value == Decimal("111.9")


def _fama_zip() -> bytes:
    start = date(2010, 1, 1)
    lines = [
        "This file was created from a fixed research database.",
        "",
        ",Mkt-RF,SMB,HML,RF",
    ]
    for index in range(253):
        effective = start + timedelta(days=index)
        lines.append(f"{effective:%Y%m%d},0.09,0,0,0.01")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "F-F_Research_Data_Factors_daily.csv",
            "\n".join(lines),
        )
    return buffer.getvalue()


def _world_bank_gold_xlsx() -> bytes:
    periods = tuple(
        f"{year:04d}M{month:02d}"
        for year in range(2000, 2010)
        for month in range(1, 13)
    )
    shared = ("Monthly Prices", "Gold", "($/troy oz)", *periods)
    shared_xml = "".join(
        f"<si><t>{value}</t></si>" for value in shared
    )
    data_rows = "".join(
        (
            f'<row r="{index + 7}">'
            f'<c r="A{index + 7}" t="s"><v>{index + 3}</v></c>'
            f'<c r="BR{index + 7}"><v>{125 + Decimal(index) / 4}</v></c>'
            "</row>"
        )
        for index in range(len(periods))
    )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData>"
        '<row r="5"><c r="BR5" t="s"><v>1</v></c></row>'
        '<row r="6"><c r="BR6" t="s"><v>2</v></c></row>'
        f"{data_rows}"
        "</sheetData></worksheet>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Monthly Prices" sheetId="1" r:id="rId1"/></sheets>'
            "</workbook>",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
            "</Relationships>",
        )
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
        archive.writestr(
            "xl/sharedStrings.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"{shared_xml}</sst>",
        )
    return buffer.getvalue()
