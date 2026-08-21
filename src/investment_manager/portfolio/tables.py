"""Database tables owned by Portfolio economic state and targets."""

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Index,
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
    Column("as_of", DateTime(timezone=True), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("snapshot_hash", String(64), nullable=False, unique=True),
    Column("payload", JSON, nullable=False),
    UniqueConstraint(
        "cycle_id",
        "portfolio_id",
        name="uq_portfolio_account_cycle",
    ),
)
Index(
    "ix_portfolio_account_as_of",
    portfolio_account_snapshots.c.portfolio_id,
    portfolio_account_snapshots.c.as_of,
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
