"""Database tables owned by Portfolio economic state and targets."""

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Table,
    UniqueConstraint,
)

from investment_manager.platform.database import metadata

portfolio_account_snapshots = Table(
    "portfolio_account_snapshots",
    metadata,
    Column("snapshot_id", String(128), primary_key=True),
    Column("cycle_id", String(128), nullable=False),
    Column("portfolio_id", String(64), nullable=False),
    Column("revision", Integer, nullable=False, default=0, server_default="0"),
    Column("as_of", DateTime(timezone=True), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("snapshot_hash", String(64), nullable=False, unique=True),
    Column("payload", JSON, nullable=False),
    UniqueConstraint(
        "cycle_id",
        "portfolio_id",
        name="uq_portfolio_account_cycle",
    ),
    UniqueConstraint(
        "portfolio_id",
        "revision",
        name="uq_portfolio_account_revision",
    ),
)
Index(
    "ix_portfolio_account_as_of",
    portfolio_account_snapshots.c.portfolio_id,
    portfolio_account_snapshots.c.as_of,
    portfolio_account_snapshots.c.revision,
)

portfolio_performance_intervals = Table(
    "portfolio_performance_intervals",
    metadata,
    Column("interval_id", String(128), primary_key=True),
    Column("portfolio_id", String(64), nullable=False),
    Column(
        "start_snapshot_id",
        ForeignKey("portfolio_account_snapshots.snapshot_id"),
        nullable=False,
    ),
    Column(
        "end_snapshot_id",
        ForeignKey("portfolio_account_snapshots.snapshot_id"),
        nullable=False,
        unique=True,
    ),
    Column("start_as_of", DateTime(timezone=True), nullable=False),
    Column("end_as_of", DateTime(timezone=True), nullable=False),
    Column("start_revision", Integer, nullable=False),
    Column("end_revision", Integer, nullable=False),
    Column("kind", String(32), nullable=False),
    Column("net_pnl", Numeric(38, 18), nullable=False),
    Column("return_fraction", Numeric(38, 18), nullable=False),
    Column("interval_hash", String(64), nullable=False, unique=True),
    Column("payload", JSON, nullable=False),
)
Index(
    "ix_portfolio_performance_end",
    portfolio_performance_intervals.c.portfolio_id,
    portfolio_performance_intervals.c.end_as_of,
    portfolio_performance_intervals.c.end_revision,
    portfolio_performance_intervals.c.interval_id,
)

portfolio_targets = Table(
    "portfolio_targets",
    metadata,
    Column("target_id", String(128), primary_key=True),
    Column("cycle_id", String(128), nullable=False, unique=True),
    Column("portfolio_id", String(64), nullable=False),
    Column(
        "account_snapshot_id",
        ForeignKey("portfolio_account_snapshots.snapshot_id"),
        nullable=False,
    ),
    Column("as_of", DateTime(timezone=True), nullable=False),
    Column("valid_until", DateTime(timezone=True), nullable=False),
    Column("target_hash", String(64), nullable=False, unique=True),
    Column("payload", JSON, nullable=False),
)
Index(
    "ix_portfolio_targets_valid_until",
    portfolio_targets.c.valid_until,
    portfolio_targets.c.target_id,
)

portfolio_target_forecasts = Table(
    "portfolio_target_forecasts",
    metadata,
    Column(
        "target_id",
        ForeignKey("portfolio_targets.target_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "forecast_id",
        ForeignKey("forecasts.forecast_id"),
        primary_key=True,
    ),
)

historical_portfolio_target_forecasts = Table(
    "historical_portfolio_target_forecasts",
    metadata,
    Column("target_id", String(128), primary_key=True),
    Column("forecast_id", String(128), primary_key=True),
)

capital_cycle_records = Table(
    "capital_cycle_records",
    metadata,
    Column("record_id", String(128), primary_key=True),
    Column("portfolio_id", String(64), nullable=False),
    Column("pipeline_id", String(128), nullable=False),
    Column("cause_id", String(128), nullable=False),
    Column("evaluated_at", DateTime(timezone=True), nullable=False),
    Column("decision_cycle_id", String(128), nullable=False),
    Column(
        "account_snapshot_id",
        ForeignKey("portfolio_account_snapshots.snapshot_id"),
        nullable=False,
    ),
    Column(
        "target_id",
        ForeignKey("portfolio_targets.target_id"),
        nullable=True,
    ),
    Column("outcome", String(48), nullable=False),
    Column("payload", JSON, nullable=False),
    UniqueConstraint(
        "portfolio_id",
        "pipeline_id",
        "cause_id",
        name="uq_capital_cycle_cause",
    ),
)
Index(
    "ix_capital_cycle_records_time",
    capital_cycle_records.c.pipeline_id,
    capital_cycle_records.c.evaluated_at,
    capital_cycle_records.c.record_id,
)
