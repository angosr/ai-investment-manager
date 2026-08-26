"""Database tables owned by the active product-capital execution path."""

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    UniqueConstraint,
    false,
)

from investment_manager.platform.database import metadata

trade_plans = Table(
    "trade_plans",
    metadata,
    Column("plan_id", String(128), primary_key=True),
    Column(
        "approved_target_id",
        ForeignKey("risk_execution_authorizations.authorization_id"),
        nullable=False,
        unique=True,
    ),
    Column("cycle_id", String(128), nullable=False, unique=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("plan_hash", String(64), nullable=False, unique=True),
    Column("payload", JSON, nullable=False),
)
Index("ix_trade_plans_created_at", trade_plans.c.created_at, trade_plans.c.plan_id)

execution_groups = Table(
    "execution_groups",
    metadata,
    Column("group_id", String(128), primary_key=True),
    Column("plan_id", ForeignKey("trade_plans.plan_id"), nullable=False),
    Column("cycle_id", String(128), nullable=False),
    Column("sleeve_id", String(128), nullable=False),
    Column("status", String(32), nullable=False),
    Column("terminal", Boolean, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
    UniqueConstraint("plan_id", "sleeve_id", name="uq_execution_group_plan_sleeve"),
)
Index(
    "uq_execution_group_active_sleeve",
    execution_groups.c.sleeve_id,
    unique=True,
    postgresql_where=execution_groups.c.terminal.is_(false()),
    sqlite_where=execution_groups.c.terminal.is_(false()),
)
Index(
    "ix_execution_groups_status_updated",
    execution_groups.c.status,
    execution_groups.c.updated_at,
)

mock_product_orders = Table(
    "mock_product_orders",
    metadata,
    Column("client_order_id", String(36), primary_key=True),
    Column("venue_order_id", String(128), nullable=False, unique=True),
    Column("group_id", ForeignKey("execution_groups.group_id"), nullable=False),
    Column("execution_leg_id", String(128), nullable=False, unique=True),
    Column("status", String(32), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)
Index("ix_mock_product_orders_group", mock_product_orders.c.group_id)

product_order_observations = Table(
    "product_order_observations",
    metadata,
    Column("observation_id", String(128), primary_key=True),
    Column("observation_hash", String(64), nullable=False, unique=True),
    Column("client_order_id", String(36), nullable=False),
    Column("group_id", ForeignKey("execution_groups.group_id"), nullable=False),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)
Index(
    "ix_product_order_observations_group_available",
    product_order_observations.c.group_id,
    product_order_observations.c.available_at,
)
