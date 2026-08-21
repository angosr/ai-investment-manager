"""Database tables owned by product-level risk authorization."""

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Index, String, Table, UniqueConstraint

from investment_manager.platform.database import metadata

portfolio_risk_decisions = Table(
    "portfolio_risk_decisions",
    metadata,
    Column("decision_id", String(128), primary_key=True),
    Column(
        "target_id",
        ForeignKey("portfolio_targets.target_id"),
        nullable=False,
        unique=True,
    ),
    Column("approved_target_id", String(128), nullable=True, unique=True),
    Column("outcome", String(32), nullable=False),
    Column("decided_at", DateTime(timezone=True), nullable=False),
    Column("decision_hash", String(64), nullable=False, unique=True),
    Column("payload", JSON, nullable=False),
)
Index(
    "ix_portfolio_risk_decisions_outcome",
    portfolio_risk_decisions.c.outcome,
    portfolio_risk_decisions.c.decided_at,
)

portfolio_holding_risk_reviews = Table(
    "portfolio_holding_risk_reviews",
    metadata,
    Column("review_id", String(128), primary_key=True),
    Column(
        "account_snapshot_id",
        ForeignKey("portfolio_account_snapshots.snapshot_id"),
        nullable=False,
    ),
    Column("policy_version", String(128), nullable=False),
    Column("portfolio_id", String(64), nullable=False),
    Column("reviewed_at", DateTime(timezone=True), nullable=False),
    Column("outcome", String(32), nullable=False),
    Column("review_hash", String(64), nullable=False, unique=True),
    Column("payload", JSON, nullable=False),
    UniqueConstraint(
        "account_snapshot_id",
        "policy_version",
        name="uq_holding_risk_account_policy",
    ),
)
Index(
    "ix_holding_risk_portfolio_time",
    portfolio_holding_risk_reviews.c.portfolio_id,
    portfolio_holding_risk_reviews.c.reviewed_at,
)
