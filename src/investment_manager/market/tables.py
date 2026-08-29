from sqlalchemy import JSON, BigInteger, Column, DateTime, Index, String, Table

from investment_manager.platform.database import metadata

market_quotes = Table(
    "market_quotes",
    metadata,
    Column("quote_id", String(128), primary_key=True),
    Column("symbol", String(32), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)
Index("ix_market_quotes_symbol_observed", market_quotes.c.symbol, market_quotes.c.observed_at)

market_reference_prices = Table(
    "market_reference_prices",
    metadata,
    Column("reference_price_id", String(128), primary_key=True),
    Column("symbol", String(32), nullable=False),
    Column("exchange_time", DateTime(timezone=True), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)
Index(
    "ix_market_reference_prices_symbol_time",
    market_reference_prices.c.symbol,
    market_reference_prices.c.exchange_time,
    market_reference_prices.c.observed_at,
)

cross_venue_spot_quotes = Table(
    "cross_venue_spot_quotes",
    metadata,
    Column("quote_id", String(128), primary_key=True),
    Column("venue", String(32), nullable=False),
    Column("symbol", String(32), nullable=False),
    Column("exchange_time", DateTime(timezone=True), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)
Index(
    "ix_cross_venue_spot_quotes_venue_symbol_time",
    cross_venue_spot_quotes.c.venue,
    cross_venue_spot_quotes.c.symbol,
    cross_venue_spot_quotes.c.exchange_time,
    cross_venue_spot_quotes.c.observed_at,
)

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

perpetual_market_states = Table(
    "perpetual_market_states",
    metadata,
    Column("state_id", String(128), primary_key=True),
    Column("instrument_id", String(128), nullable=False),
    Column("exchange_time", DateTime(timezone=True), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)
Index(
    "ix_perpetual_market_states_instrument_time",
    perpetual_market_states.c.instrument_id,
    perpetual_market_states.c.exchange_time,
    perpetual_market_states.c.observed_at,
)

perpetual_quotes = Table(
    "perpetual_quotes",
    metadata,
    Column("quote_id", String(128), primary_key=True),
    Column("instrument_id", String(128), nullable=False),
    Column("exchange_time", DateTime(timezone=True), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)
Index(
    "ix_perpetual_quotes_instrument_time",
    perpetual_quotes.c.instrument_id,
    perpetual_quotes.c.exchange_time,
    perpetual_quotes.c.observed_at,
)

perpetual_product_rules = Table(
    "perpetual_product_rules",
    metadata,
    Column("rules_id", String(128), primary_key=True),
    Column("instrument_id", String(128), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("payload", JSON, nullable=False),
)
Index(
    "ix_perpetual_product_rules_instrument_time",
    perpetual_product_rules.c.instrument_id,
    perpetual_product_rules.c.observed_at,
)

funding_settlements = Table(
    "funding_settlements",
    metadata,
    Column("settlement_id", String(128), primary_key=True),
    Column("instrument_id", String(128), nullable=False),
    Column("funding_time", DateTime(timezone=True), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("rate_type", String(16), nullable=False),
    Column("payload", JSON, nullable=False),
)
Index(
    "ix_funding_settlements_instrument_time",
    funding_settlements.c.instrument_id,
    funding_settlements.c.funding_time,
    funding_settlements.c.observed_at,
)

tradfi_trading_schedules = Table(
    "tradfi_trading_schedules",
    metadata,
    Column("schedule_id", String(128), primary_key=True),
    Column("exchange_time", DateTime(timezone=True), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)
Index(
    "ix_tradfi_trading_schedules_time",
    tradfi_trading_schedules.c.exchange_time,
    tradfi_trading_schedules.c.observed_at,
)


market_tables = (
    market_quotes,
    market_reference_prices,
    cross_venue_spot_quotes,
    market_trades,
    market_bars,
    perpetual_market_states,
    perpetual_quotes,
    perpetual_product_rules,
    funding_settlements,
    tradfi_trading_schedules,
)
