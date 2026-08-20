from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Index,
    String,
    Table,
    insert,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from investment_manager.kernel.time import require_utc
from investment_manager.market.models import (
    ClosedMarketBar,
    MarketQuote,
    MarketSnapshot,
    MarketTrade,
)
from investment_manager.platform.database import metadata

logger = logging.getLogger(__name__)


class MarketDataStore(Protocol):
    def put_quote(self, quote: MarketQuote) -> bool: ...

    def put_trade(self, trade: MarketTrade) -> bool: ...

    def put_bar(self, bar: ClosedMarketBar) -> bool: ...

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


market_quotes = Table(
    "market_quotes",
    metadata,
    Column("quote_id", String(128), primary_key=True),
    Column("symbol", String(32), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)
Index("ix_market_quotes_symbol_observed", market_quotes.c.symbol, market_quotes.c.observed_at)

market_trades = Table(
    "market_trades",
    metadata,
    Column("symbol", String(32), primary_key=True),
    Column("aggregate_trade_id", BigInteger, primary_key=True),
    Column("event_time", DateTime(timezone=True), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)
Index("ix_market_trades_symbol_event", market_trades.c.symbol, market_trades.c.event_time)

market_bars = Table(
    "market_bars",
    metadata,
    Column("symbol", String(32), primary_key=True),
    Column("interval", String(16), primary_key=True),
    Column("open_time", DateTime(timezone=True), primary_key=True),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)
Index(
    "ix_market_bars_symbol_interval_open",
    market_bars.c.symbol,
    market_bars.c.interval,
    market_bars.c.open_time,
)


market_tables = (market_quotes, market_trades, market_bars)


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
