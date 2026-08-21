from __future__ import annotations

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
        marker: str | int = (
            update_id if update_id is not None else exchange_time.isoformat()
        )
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
        raw = await self.transport.get(
            "/fapi/v1/premiumIndex",
            {"symbol": instrument.symbol},
        )
        observed_at = require_utc(self.clock())
        if not isinstance(raw, dict) or str(raw.get("symbol")) != instrument.symbol:
            raise ValueError("Binance premiumIndex REST 响应非法")
        exchange_time = _from_milliseconds(int(raw["time"]))
        estimated = Decimal(str(raw["estimatedSettlePrice"]))
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
            source="binance-usdm-premium-index-rest",
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
