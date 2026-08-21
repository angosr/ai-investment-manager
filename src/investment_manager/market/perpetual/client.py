from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from investment_manager.kernel.identity import stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.market.models import InstrumentId
from investment_manager.market.perpetual.models import (
    FundingRateType,
    FundingSettlement,
    PerpetualMarketState,
    PerpetualQuote,
)


def _from_milliseconds(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=UTC)


class JsonHttpTransport(Protocol):
    async def get(self, path: str, params: dict[str, Any]) -> Any: ...


@dataclass(slots=True)
class BinanceUsdmRestClient:
    transport: JsonHttpTransport
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    async def fetch_quote(self, instrument: InstrumentId) -> PerpetualQuote:
        raw = await self.transport.get(
            "/fapi/v1/ticker/bookTicker",
            {"symbol": instrument.symbol},
        )
        observed_at = require_utc(self.clock())
        if not isinstance(raw, dict) or str(raw.get("symbol")) != instrument.symbol:
            raise ValueError("Binance USD-M bookTicker REST 响应非法")
        exchange_time = _from_milliseconds(int(raw["time"]))
        update_id = int(raw["lastUpdateId"]) if "lastUpdateId" in raw else None
        marker: str | int = update_id if update_id is not None else exchange_time.isoformat()
        return PerpetualQuote(
            quote_id=stable_id("perpetual_quote", instrument.key, marker),
            instrument=instrument,
            exchange_time=exchange_time,
            observed_at=observed_at,
            bid=Decimal(str(raw["bidPrice"])),
            bid_quantity=Decimal(str(raw["bidQty"])),
            ask=Decimal(str(raw["askPrice"])),
            ask_quantity=Decimal(str(raw["askQty"])),
            update_id=update_id,
            source="binance-usdm-book-ticker-rest",
        )

    async def fetch_market_state(self, instrument: InstrumentId) -> PerpetualMarketState:
        raw, open_interest, account_ratio, taker_ratio = await asyncio.gather(
            self.transport.get(
                "/fapi/v1/premiumIndex",
                {"symbol": instrument.symbol},
            ),
            self.transport.get(
                "/futures/data/openInterestHist",
                {"symbol": instrument.symbol, "period": "5m", "limit": 13},
            ),
            self.transport.get(
                "/futures/data/globalLongShortAccountRatio",
                {"symbol": instrument.symbol, "period": "5m", "limit": 1},
            ),
            self.transport.get(
                "/futures/data/takerlongshortRatio",
                {"symbol": instrument.symbol, "period": "5m", "limit": 12},
            ),
        )
        observed_at = require_utc(self.clock())
        if not isinstance(raw, dict) or str(raw.get("symbol")) != instrument.symbol:
            raise ValueError("Binance premiumIndex REST 响应非法")
        exchange_time = _from_milliseconds(int(raw["time"]))
        estimated = Decimal(str(raw["estimatedSettlePrice"]))
        positioning = _positioning_summary(
            instrument=instrument,
            open_interest=open_interest,
            account_ratio=account_ratio,
            taker_ratio=taker_ratio,
            observed_at=observed_at,
        )
        return PerpetualMarketState(
            state_id=stable_id(
                "perpetual_market_state",
                instrument.key,
                exchange_time.isoformat(),
            ),
            instrument=instrument,
            exchange_time=exchange_time,
            observed_at=observed_at,
            mark_price=Decimal(str(raw["markPrice"])),
            index_price=Decimal(str(raw["indexPrice"])),
            estimated_settle_price=estimated if estimated > 0 else None,
            last_funding_rate=Decimal(str(raw["lastFundingRate"])),
            interest_rate=Decimal(str(raw["interestRate"])),
            next_funding_time=_from_milliseconds(int(raw["nextFundingTime"])),
            **positioning,
            source="binance-usdm-public-market-rest",
        )

    async def fetch_funding_settlements(
        self,
        instrument: InstrumentId,
        *,
        start: datetime,
        end: datetime,
    ) -> tuple[FundingSettlement, ...]:
        start = require_utc(start)
        end = require_utc(end)
        if start >= end:
            raise ValueError("Funding REST 查询时间边界非法")
        raw = await self.transport.get(
            "/fapi/v1/fundingRate",
            {
                "symbol": instrument.symbol,
                "startTime": int(start.timestamp() * 1000),
                "endTime": int(end.timestamp() * 1000),
                "limit": 1000,
            },
        )
        observed_at = require_utc(self.clock())
        if not isinstance(raw, list):
            raise ValueError("Binance fundingRate REST 响应非法")
        values: list[FundingSettlement] = []
        for item in raw:
            if not isinstance(item, dict) or str(item.get("symbol")) != instrument.symbol:
                raise ValueError("Binance fundingRate REST 条目非法")
            funding_time = _from_milliseconds(int(item["fundingTime"]))
            if not start <= funding_time < end:
                continue
            rate_type = FundingRateType(str(item["rateType"]).upper())
            values.append(
                FundingSettlement(
                    settlement_id=stable_id(
                        "funding_settlement",
                        instrument.key,
                        funding_time.isoformat(),
                        rate_type.value,
                    ),
                    instrument=instrument,
                    funding_time=funding_time,
                    observed_at=observed_at,
                    funding_rate=Decimal(str(item["fundingRate"])),
                    mark_price=Decimal(str(item["markPrice"])),
                    rate_type=rate_type,
                    source="binance-usdm-funding-rate-rest",
                )
            )
        ordering = tuple((item.funding_time, item.rate_type.value) for item in values)
        if tuple(sorted(set(ordering))) != ordering:
            raise ValueError("Binance fundingRate REST 结算必须唯一且升序")
        return tuple(values)


def _positioning_summary(
    *,
    instrument: InstrumentId,
    open_interest: Any,
    account_ratio: Any,
    taker_ratio: Any,
    observed_at: datetime,
) -> dict[str, Decimal | datetime | int]:
    oi_rows = _ordered_positioning_rows(
        open_interest,
        name="openInterestHist",
        symbol=instrument.symbol,
    )
    account_rows = _ordered_positioning_rows(
        account_ratio,
        name="globalLongShortAccountRatio",
        symbol=instrument.symbol,
    )
    taker_rows = _ordered_positioning_rows(
        taker_ratio,
        name="takerlongshortRatio",
        symbol=None,
    )
    if len(oi_rows) < 2 or not account_rows or not taker_rows:
        raise ValueError("Binance USD-M 仓位窗口样本不足")
    first_oi = Decimal(str(oi_rows[0]["sumOpenInterest"]))
    latest_oi = Decimal(str(oi_rows[-1]["sumOpenInterest"]))
    latest_oi_value = Decimal(str(oi_rows[-1]["sumOpenInterestValue"]))
    if first_oi <= 0 or latest_oi < 0 or latest_oi_value < 0:
        raise ValueError("Binance USD-M Open Interest 非法")
    latest_account = account_rows[-1]
    long_fraction = Decimal(str(latest_account["longAccount"]))
    short_fraction = Decimal(str(latest_account["shortAccount"]))
    long_short_ratio = Decimal(str(latest_account["longShortRatio"]))
    buy_volume = sum(
        (Decimal(str(item["buyVol"])) for item in taker_rows),
        Decimal("0"),
    )
    sell_volume = sum(
        (Decimal(str(item["sellVol"])) for item in taker_rows),
        Decimal("0"),
    )
    if sell_volume <= 0 or buy_volume < 0:
        raise ValueError("Binance USD-M 主动买卖量非法")
    latest_timestamp = max(
        int(oi_rows[-1]["timestamp"]),
        int(account_rows[-1]["timestamp"]),
        int(taker_rows[-1]["timestamp"]),
    )
    positioning_observed_at = _from_milliseconds(latest_timestamp)
    if positioning_observed_at > observed_at:
        raise ValueError("Binance USD-M 仓位数据晚于系统观察时间")
    return {
        "positioning_observed_at": positioning_observed_at,
        "positioning_window_minutes": 60,
        "open_interest": latest_oi,
        "open_interest_value": latest_oi_value,
        "open_interest_change_fraction": latest_oi / first_oi - Decimal("1"),
        "global_long_short_account_ratio": long_short_ratio,
        "global_long_account_fraction": long_fraction,
        "global_short_account_fraction": short_fraction,
        "taker_buy_sell_ratio": buy_volume / sell_volume,
        "taker_buy_volume": buy_volume,
        "taker_sell_volume": sell_volume,
    }


def _ordered_positioning_rows(
    raw: Any,
    *,
    name: str,
    symbol: str | None,
) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise ValueError(f"Binance {name} REST 响应非法")
    if symbol is not None and any(str(item.get("symbol")) != symbol for item in raw):
        raise ValueError(f"Binance {name} REST symbol 不一致")
    try:
        rows = sorted(raw, key=lambda item: int(item["timestamp"]))
        timestamps = tuple(int(item["timestamp"]) for item in rows)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Binance {name} REST 缺少时间字段") from exc
    if len(set(timestamps)) != len(timestamps):
        raise ValueError(f"Binance {name} REST 时间点重复")
    return rows
