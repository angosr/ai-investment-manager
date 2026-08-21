from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.kernel.time import require_utc
from investment_manager.market.models import (
    ClosedMarketBar,
    InstrumentId,
    MarketQuote,
    MarketSnapshot,
    MarketTrade,
)
from investment_manager.market.perpetual.models import (
    FundingSettlement,
    PerpetualMarketState,
    PerpetualQuote,
)
from investment_manager.market.tables import (
    funding_settlements,
    market_bars,
    market_quotes,
    market_tables,
    market_trades,
    perpetual_market_states,
    perpetual_quotes,
)
from investment_manager.platform.database import metadata

logger = logging.getLogger(__name__)


class MarketDataStore(Protocol):
    def put_quote(self, quote: MarketQuote) -> bool: ...

    def put_trade(self, trade: MarketTrade) -> bool: ...

    def put_bar(self, bar: ClosedMarketBar) -> bool: ...

    def put_perpetual_state(self, state: PerpetualMarketState) -> bool: ...

    def put_perpetual_quote(self, quote: PerpetualQuote) -> bool: ...

    def put_funding_settlement(self, settlement: FundingSettlement) -> bool: ...

    def latest_perpetual_state(
        self, *, instrument: InstrumentId, as_of: datetime
    ) -> PerpetualMarketState | None: ...

    def latest_perpetual_quote(
        self,
        *,
        instrument: InstrumentId,
        evaluation_at: datetime,
        visible_at: datetime,
    ) -> PerpetualQuote | None: ...

    def funding_settlements(
        self,
        *,
        instrument: InstrumentId,
        start: datetime,
        end: datetime,
        visible_at: datetime,
    ) -> tuple[FundingSettlement, ...]: ...

    def latest_trade(self, *, symbol: str, as_of: datetime) -> MarketTrade: ...

    def snapshot(
        self,
        *,
        cycle_id: str,
        symbol: str,
        interval: str,
        as_of: datetime,
        bar_window: int,
        source: str,
    ) -> MarketSnapshot: ...


def _bar_market_facts(bar: ClosedMarketBar) -> dict[str, Any]:
    return bar.model_dump(exclude={"observed_at", "source"}, mode="json")


def _bar_revision_only_changes_volume(
    existing: ClosedMarketBar,
    incoming: ClosedMarketBar,
) -> bool:
    """Recognize a provider's late volume finalization without rewriting history."""

    old = existing.model_dump(
        exclude={"observed_at", "source", "volume"},
        mode="json",
    )
    new = incoming.model_dump(
        exclude={"observed_at", "source", "volume"},
        mode="json",
    )
    return old == new and existing.volume != incoming.volume


def _quote_market_facts(quote: MarketQuote) -> dict[str, Any]:
    return quote.model_dump(exclude={"observed_at", "source"}, mode="json")


def _trade_market_facts(trade: MarketTrade) -> dict[str, Any]:
    return trade.model_dump(exclude={"observed_at", "source"}, mode="json")


def _perpetual_market_facts(state: PerpetualMarketState) -> dict[str, Any]:
    return state.model_dump(exclude={"observed_at", "source"}, mode="json")


def _perpetual_quote_facts(quote: PerpetualQuote) -> dict[str, Any]:
    return quote.model_dump(exclude={"observed_at", "source"}, mode="json")


def _funding_settlement_facts(settlement: FundingSettlement) -> dict[str, Any]:
    return settlement.model_dump(exclude={"observed_at", "source"}, mode="json")


def trade_at_or_before(
    engine: Engine,
    *,
    symbol: str,
    evaluation_at: datetime,
    visible_at: datetime,
) -> MarketTrade | None:
    """Read the latest trade that was knowable at a point-in-time horizon."""

    with engine.connect() as connection:
        payload = connection.execute(
            select(market_trades.c.payload)
            .where(
                market_trades.c.symbol == symbol,
                market_trades.c.event_time <= require_utc(evaluation_at),
                market_trades.c.observed_at <= require_utc(visible_at),
            )
            .order_by(
                market_trades.c.event_time.desc(),
                market_trades.c.aggregate_trade_id.desc(),
            )
            .limit(1)
        ).scalar_one_or_none()
    return MarketTrade.model_validate(payload) if payload is not None else None


@dataclass(slots=True)
class InMemoryMarketDataStore:
    _quotes: dict[str, MarketQuote] = field(default_factory=dict)
    _trades: dict[tuple[str, int], MarketTrade] = field(default_factory=dict)
    _bars: dict[tuple[str, str, datetime], ClosedMarketBar] = field(default_factory=dict)
    _perpetual_states: dict[str, PerpetualMarketState] = field(default_factory=dict)
    _perpetual_quotes: dict[str, PerpetualQuote] = field(default_factory=dict)
    _funding_settlements: dict[str, FundingSettlement] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def put_quote(self, quote: MarketQuote) -> bool:
        with self._lock:
            existing = self._quotes.get(quote.quote_id)
            if existing is not None:
                if _quote_market_facts(existing) != _quote_market_facts(quote):
                    raise ValueError("quote_id 冲突且事实不一致")
                return False
            self._quotes[quote.quote_id] = quote
            return True

    def put_trade(self, trade: MarketTrade) -> bool:
        key = (trade.symbol, trade.aggregate_trade_id)
        with self._lock:
            existing = self._trades.get(key)
            if existing is not None:
                if _trade_market_facts(existing) != _trade_market_facts(trade):
                    raise ValueError("aggregate_trade_id 冲突且事实不一致")
                return False
            self._trades[key] = trade
            return True

    def put_bar(self, bar: ClosedMarketBar) -> bool:
        key = (bar.symbol, bar.interval, bar.open_time)
        with self._lock:
            existing = self._bars.get(key)
            if existing is not None:
                if _bar_market_facts(existing) != _bar_market_facts(bar):
                    if _bar_revision_only_changes_volume(existing, bar):
                        logger.warning(
                            "late closed-bar volume revision ignored; preserving first-seen fact",
                            extra={
                                "symbol": bar.symbol,
                                "interval": bar.interval,
                                "open_time": bar.open_time.isoformat(),
                            },
                        )
                        return False
                    raise ValueError("已收盘 K 线唯一键冲突且事实不一致")
                return False
            self._bars[key] = bar
            return True

    def put_perpetual_state(self, state: PerpetualMarketState) -> bool:
        with self._lock:
            existing = self._perpetual_states.get(state.state_id)
            if existing is not None:
                if _perpetual_market_facts(existing) != _perpetual_market_facts(state):
                    raise ValueError("state_id 冲突且永续市场事实不一致")
                return False
            self._perpetual_states[state.state_id] = state
            return True

    def put_perpetual_quote(self, quote: PerpetualQuote) -> bool:
        with self._lock:
            existing = self._perpetual_quotes.get(quote.quote_id)
            if existing is not None:
                if _perpetual_quote_facts(existing) != _perpetual_quote_facts(quote):
                    raise ValueError("quote_id 冲突且永续报价事实不一致")
                return False
            self._perpetual_quotes[quote.quote_id] = quote
            return True

    def put_funding_settlement(self, settlement: FundingSettlement) -> bool:
        with self._lock:
            existing = self._funding_settlements.get(settlement.settlement_id)
            if existing is not None:
                if _funding_settlement_facts(existing) != _funding_settlement_facts(settlement):
                    raise ValueError("settlement_id 冲突且 Funding 事实不一致")
                return False
            self._funding_settlements[settlement.settlement_id] = settlement
            return True

    def latest_perpetual_state(
        self, *, instrument: InstrumentId, as_of: datetime
    ) -> PerpetualMarketState | None:
        visible_at = require_utc(as_of)
        with self._lock:
            visible = tuple(
                item
                for item in self._perpetual_states.values()
                if item.instrument == instrument
                and item.exchange_time <= visible_at
                and item.observed_at <= visible_at
            )
        return max(
            visible,
            key=lambda item: (item.exchange_time, item.observed_at, item.state_id),
            default=None,
        )

    def latest_perpetual_quote(
        self,
        *,
        instrument: InstrumentId,
        evaluation_at: datetime,
        visible_at: datetime,
    ) -> PerpetualQuote | None:
        evaluation_at = require_utc(evaluation_at)
        visible_at = require_utc(visible_at)
        if evaluation_at > visible_at:
            raise ValueError("永续报价评价时间不能晚于可见时间")
        with self._lock:
            visible = tuple(
                item
                for item in self._perpetual_quotes.values()
                if item.instrument == instrument
                and item.exchange_time <= evaluation_at
                and item.observed_at <= visible_at
            )
        return max(
            visible,
            key=lambda item: (item.exchange_time, item.observed_at, item.quote_id),
            default=None,
        )

    def funding_settlements(
        self,
        *,
        instrument: InstrumentId,
        start: datetime,
        end: datetime,
        visible_at: datetime,
    ) -> tuple[FundingSettlement, ...]:
        start = require_utc(start)
        end = require_utc(end)
        visible_at = require_utc(visible_at)
        if not start < end <= visible_at:
            raise ValueError("Funding 查询时间边界非法")
        with self._lock:
            return tuple(
                sorted(
                    (
                        item
                        for item in self._funding_settlements.values()
                        if item.instrument == instrument
                        and start <= item.funding_time < end
                        and item.observed_at <= visible_at
                    ),
                    key=lambda item: (
                        item.funding_time,
                        item.rate_type.value,
                        item.settlement_id,
                    ),
                )
            )

    def latest_trade(self, *, symbol: str, as_of: datetime) -> MarketTrade:
        as_of = require_utc(as_of)
        with self._lock:
            visible = [
                item
                for item in self._trades.values()
                if item.symbol == symbol and item.observed_at <= as_of and item.event_time <= as_of
            ]
        if not visible:
            raise ValueError(f"{symbol} 缺少可见成交，无法计算组合权益")
        return max(
            visible,
            key=lambda item: (item.event_time, item.observed_at, item.aggregate_trade_id),
        )

    def snapshot(
        self,
        *,
        cycle_id: str,
        symbol: str,
        interval: str,
        as_of: datetime,
        bar_window: int,
        source: str,
    ) -> MarketSnapshot:
        as_of = require_utc(as_of)
        with self._lock:
            quotes = [
                item
                for item in self._quotes.values()
                if item.symbol == symbol and item.observed_at <= as_of
            ]
            trades = [
                item
                for item in self._trades.values()
                if item.symbol == symbol and item.observed_at <= as_of and item.event_time <= as_of
            ]
            bars = [
                item
                for item in self._bars.values()
                if item.symbol == symbol and item.interval == interval and item.observed_at <= as_of
            ]
        if not quotes or not trades:
            raise ValueError("行情快照缺少可见的报价或成交")
        quote = max(quotes, key=lambda item: (item.observed_at, item.quote_id))
        trade = max(
            trades,
            key=lambda item: (item.event_time, item.observed_at, item.aggregate_trade_id),
        )
        selected_bars = sorted(bars, key=lambda item: item.open_time)[-bar_window:]
        if len(selected_bars) < 2:
            raise ValueError("行情快照至少需要两根已收盘 K 线")
        return MarketSnapshot(
            cycle_id=cycle_id,
            symbol=symbol,
            as_of=as_of,
            observed_at=min(quote.observed_at, trade.observed_at),
            bid=quote.bid,
            ask=quote.ask,
            last=trade.price,
            bars=tuple(item.to_market_bar() for item in selected_bars),
            source=source,
        )


def create_market_schema(engine: Engine) -> None:
    metadata.create_all(engine, tables=market_tables)


class SqlMarketDataStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def put_quote(self, quote: MarketQuote) -> bool:
        payload = quote.model_dump(mode="json")
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(market_quotes).values(
                        quote_id=quote.quote_id,
                        symbol=quote.symbol,
                        observed_at=quote.observed_at,
                        payload=payload,
                    )
                )
            return True
        except IntegrityError:
            with self._engine.connect() as connection:
                existing = connection.execute(
                    select(market_quotes.c.payload).where(
                        market_quotes.c.quote_id == quote.quote_id
                    )
                ).scalar_one()
            if _quote_market_facts(MarketQuote.model_validate(existing)) != _quote_market_facts(
                quote
            ):
                raise ValueError("quote_id 冲突且事实不一致") from None
            return False

    def put_trade(self, trade: MarketTrade) -> bool:
        payload = trade.model_dump(mode="json")
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(market_trades).values(
                        symbol=trade.symbol,
                        aggregate_trade_id=trade.aggregate_trade_id,
                        event_time=trade.event_time,
                        observed_at=trade.observed_at,
                        payload=payload,
                    )
                )
            return True
        except IntegrityError:
            with self._engine.connect() as connection:
                existing = connection.execute(
                    select(market_trades.c.payload).where(
                        market_trades.c.symbol == trade.symbol,
                        market_trades.c.aggregate_trade_id == trade.aggregate_trade_id,
                    )
                ).scalar_one()
            if _trade_market_facts(MarketTrade.model_validate(existing)) != _trade_market_facts(
                trade
            ):
                raise ValueError("aggregate_trade_id 冲突且事实不一致") from None
            return False

    def put_bar(self, bar: ClosedMarketBar) -> bool:
        payload = bar.model_dump(mode="json")
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(market_bars).values(
                        symbol=bar.symbol,
                        interval=bar.interval,
                        open_time=bar.open_time,
                        observed_at=bar.observed_at,
                        payload=payload,
                    )
                )
            return True
        except IntegrityError:
            with self._engine.connect() as connection:
                existing = connection.execute(
                    select(market_bars.c.payload).where(
                        market_bars.c.symbol == bar.symbol,
                        market_bars.c.interval == bar.interval,
                        market_bars.c.open_time == bar.open_time,
                    )
                ).scalar_one()
            existing_bar = ClosedMarketBar.model_validate(existing)
            existing_facts = _bar_market_facts(existing_bar)
            if existing_facts != _bar_market_facts(bar):
                if _bar_revision_only_changes_volume(existing_bar, bar):
                    logger.warning(
                        "late closed-bar volume revision ignored; preserving first-seen fact",
                        extra={
                            "symbol": bar.symbol,
                            "interval": bar.interval,
                            "open_time": bar.open_time.isoformat(),
                        },
                    )
                    return False
                raise ValueError("已收盘 K 线唯一键冲突且事实不一致") from None
            return False

    def put_perpetual_state(self, state: PerpetualMarketState) -> bool:
        payload = state.model_dump(mode="json")
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(perpetual_market_states).values(
                        state_id=state.state_id,
                        instrument_id=state.instrument.key,
                        exchange_time=state.exchange_time,
                        observed_at=state.observed_at,
                        payload=payload,
                    )
                )
            return True
        except IntegrityError:
            with self._engine.connect() as connection:
                existing = connection.execute(
                    select(perpetual_market_states.c.payload).where(
                        perpetual_market_states.c.state_id == state.state_id
                    )
                ).scalar_one()
            if _perpetual_market_facts(
                PerpetualMarketState.model_validate(existing)
            ) != _perpetual_market_facts(state):
                raise ValueError("state_id 冲突且永续市场事实不一致") from None
            return False

    def put_perpetual_quote(self, quote: PerpetualQuote) -> bool:
        payload = quote.model_dump(mode="json")
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(perpetual_quotes).values(
                        quote_id=quote.quote_id,
                        instrument_id=quote.instrument.key,
                        exchange_time=quote.exchange_time,
                        observed_at=quote.observed_at,
                        payload=payload,
                    )
                )
            return True
        except IntegrityError:
            with self._engine.connect() as connection:
                existing = connection.execute(
                    select(perpetual_quotes.c.payload).where(
                        perpetual_quotes.c.quote_id == quote.quote_id
                    )
                ).scalar_one()
            if _perpetual_quote_facts(
                PerpetualQuote.model_validate(existing)
            ) != _perpetual_quote_facts(quote):
                raise ValueError("quote_id 冲突且永续报价事实不一致") from None
            return False

    def put_funding_settlement(self, settlement: FundingSettlement) -> bool:
        payload = settlement.model_dump(mode="json")
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(funding_settlements).values(
                        settlement_id=settlement.settlement_id,
                        instrument_id=settlement.instrument.key,
                        funding_time=settlement.funding_time,
                        observed_at=settlement.observed_at,
                        rate_type=settlement.rate_type.value,
                        payload=payload,
                    )
                )
            return True
        except IntegrityError:
            with self._engine.connect() as connection:
                existing = connection.execute(
                    select(funding_settlements.c.payload).where(
                        funding_settlements.c.settlement_id == settlement.settlement_id
                    )
                ).scalar_one()
            if _funding_settlement_facts(
                FundingSettlement.model_validate(existing)
            ) != _funding_settlement_facts(settlement):
                raise ValueError("settlement_id 冲突且 Funding 事实不一致") from None
            return False

    def latest_perpetual_state(
        self, *, instrument: InstrumentId, as_of: datetime
    ) -> PerpetualMarketState | None:
        as_of = require_utc(as_of)
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(perpetual_market_states.c.payload)
                .where(
                    perpetual_market_states.c.instrument_id == instrument.key,
                    perpetual_market_states.c.exchange_time <= as_of,
                    perpetual_market_states.c.observed_at <= as_of,
                )
                .order_by(
                    perpetual_market_states.c.exchange_time.desc(),
                    perpetual_market_states.c.observed_at.desc(),
                    perpetual_market_states.c.state_id.desc(),
                )
                .limit(1)
            ).scalar_one_or_none()
        return PerpetualMarketState.model_validate(payload) if payload is not None else None

    def latest_perpetual_quote(
        self,
        *,
        instrument: InstrumentId,
        evaluation_at: datetime,
        visible_at: datetime,
    ) -> PerpetualQuote | None:
        evaluation_at = require_utc(evaluation_at)
        visible_at = require_utc(visible_at)
        if evaluation_at > visible_at:
            raise ValueError("永续报价评价时间不能晚于可见时间")
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(perpetual_quotes.c.payload)
                .where(
                    perpetual_quotes.c.instrument_id == instrument.key,
                    perpetual_quotes.c.exchange_time <= evaluation_at,
                    perpetual_quotes.c.observed_at <= visible_at,
                )
                .order_by(
                    perpetual_quotes.c.exchange_time.desc(),
                    perpetual_quotes.c.observed_at.desc(),
                    perpetual_quotes.c.quote_id.desc(),
                )
                .limit(1)
            ).scalar_one_or_none()
        return PerpetualQuote.model_validate(payload) if payload is not None else None

    def funding_settlements(
        self,
        *,
        instrument: InstrumentId,
        start: datetime,
        end: datetime,
        visible_at: datetime,
    ) -> tuple[FundingSettlement, ...]:
        start = require_utc(start)
        end = require_utc(end)
        visible_at = require_utc(visible_at)
        if not start < end <= visible_at:
            raise ValueError("Funding 查询时间边界非法")
        with self._engine.connect() as connection:
            payloads = connection.execute(
                select(funding_settlements.c.payload)
                .where(
                    funding_settlements.c.instrument_id == instrument.key,
                    funding_settlements.c.funding_time >= start,
                    funding_settlements.c.funding_time < end,
                    funding_settlements.c.observed_at <= visible_at,
                )
                .order_by(
                    funding_settlements.c.funding_time,
                    funding_settlements.c.rate_type,
                    funding_settlements.c.settlement_id,
                )
            ).scalars()
            return tuple(FundingSettlement.model_validate(item) for item in payloads)

    def latest_trade(self, *, symbol: str, as_of: datetime) -> MarketTrade:
        as_of = require_utc(as_of)
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(market_trades.c.payload)
                .where(
                    market_trades.c.symbol == symbol,
                    market_trades.c.observed_at <= as_of,
                    market_trades.c.event_time <= as_of,
                )
                .order_by(
                    market_trades.c.event_time.desc(),
                    market_trades.c.aggregate_trade_id.desc(),
                )
                .limit(1)
            ).scalar_one_or_none()
        if payload is None:
            raise ValueError(f"{symbol} 缺少可见成交，无法计算组合权益")
        return MarketTrade.model_validate(payload)

    def snapshot(
        self,
        *,
        cycle_id: str,
        symbol: str,
        interval: str,
        as_of: datetime,
        bar_window: int,
        source: str,
    ) -> MarketSnapshot:
        as_of = require_utc(as_of)
        with self._engine.connect() as connection:
            quote_payload = connection.execute(
                select(market_quotes.c.payload)
                .where(
                    market_quotes.c.symbol == symbol,
                    market_quotes.c.observed_at <= as_of,
                )
                .order_by(market_quotes.c.observed_at.desc(), market_quotes.c.quote_id.desc())
                .limit(1)
            ).scalar_one_or_none()
            trade_payload = connection.execute(
                select(market_trades.c.payload)
                .where(
                    market_trades.c.symbol == symbol,
                    market_trades.c.observed_at <= as_of,
                    market_trades.c.event_time <= as_of,
                )
                .order_by(
                    market_trades.c.event_time.desc(),
                    market_trades.c.aggregate_trade_id.desc(),
                )
                .limit(1)
            ).scalar_one_or_none()
            bar_payloads = list(
                connection.execute(
                    select(market_bars.c.payload)
                    .where(
                        market_bars.c.symbol == symbol,
                        market_bars.c.interval == interval,
                        market_bars.c.observed_at <= as_of,
                    )
                    .order_by(market_bars.c.open_time.desc())
                    .limit(bar_window)
                ).scalars()
            )
        if quote_payload is None or trade_payload is None:
            raise ValueError("行情快照缺少可见的报价或成交")
        if len(bar_payloads) < 2:
            raise ValueError("行情快照至少需要两根已收盘 K 线")
        quote = MarketQuote.model_validate(quote_payload)
        trade = MarketTrade.model_validate(trade_payload)
        bars = [ClosedMarketBar.model_validate(payload) for payload in reversed(bar_payloads)]
        return MarketSnapshot(
            cycle_id=cycle_id,
            symbol=symbol,
            as_of=as_of,
            observed_at=min(quote.observed_at, trade.observed_at),
            bid=quote.bid,
            ask=quote.ask,
            last=trade.price,
            bars=tuple(item.to_market_bar() for item in bars),
            source=source,
        )
