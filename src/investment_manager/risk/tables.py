"""Database tables owned by product-level risk authorization."""

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Index, String, Table

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
