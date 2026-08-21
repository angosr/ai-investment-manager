"""Database tables owned by execution and reconciliation."""

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

account_snapshots = Table(
    "account_snapshots",
    metadata,
    Column("snapshot_id", String(128), primary_key=True),
    Column("cycle_id", ForeignKey("analysis_cycles.cycle_id"), nullable=False),
    Column("phase", String(32), nullable=False),
    Column("as_of", DateTime(timezone=True), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("reconciled", Boolean, nullable=False),
    Column("payload", JSON, nullable=False),
    UniqueConstraint("cycle_id", "phase", name="uq_account_snapshot_cycle_phase"),
)
Index("ix_account_snapshots_as_of", account_snapshots.c.as_of)

execution_requests = Table(
    "execution_requests",
    metadata,
    Column("execution_id", String(128), primary_key=True),
    Column("cycle_id", ForeignKey("analysis_cycles.cycle_id"), nullable=False, unique=True),
    Column("request_hash", String(64), nullable=False, unique=True),
    Column("status", String(32), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("result_payload", JSON, nullable=True),
)
Index("ix_execution_requests_status", execution_requests.c.status)

trade_plans = Table(
    "trade_plans",
    metadata,
    Column("plan_id", String(128), primary_key=True),
    Column(
        "approved_target_id",
        ForeignKey("portfolio_risk_decisions.approved_target_id"),
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

mock_exchange_orders = Table(
    "mock_exchange_orders",
    metadata,
    Column("client_order_id", String(36), primary_key=True),
    Column("order_id", String(128), nullable=False, unique=True),
    Column("role", String(32), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)
Index("ix_mock_exchange_orders_observed_at", mock_exchange_orders.c.observed_at)

mock_exchange_protections = Table(
    "mock_exchange_protections",
    metadata,
    Column("protection_id", String(128), primary_key=True),
    Column("position_id", String(128), nullable=False, unique=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)

reconciliation_reports = Table(
    "reconciliation_reports",
    metadata,
    Column("report_id", String(128), primary_key=True),
    Column("as_of", DateTime(timezone=True), nullable=False),
    Column("policy_version", String(128), nullable=False),
    Column("status", String(32), nullable=False),
    Column("freeze_new_risk", Boolean, nullable=False),
    Column("payload", JSON, nullable=False),
)
Index("ix_reconciliation_reports_as_of", reconciliation_reports.c.as_of)
Index("ix_reconciliation_reports_status", reconciliation_reports.c.status)

orders = Table(
    "orders",
    metadata,
    Column("order_id", String(128), primary_key=True),
    Column("client_order_id", String(36), nullable=False, unique=True),
    Column("cycle_id", ForeignKey("analysis_cycles.cycle_id"), nullable=False),
    Column("intent_id", ForeignKey("trade_intents.intent_id"), nullable=False),
    Column("role", String(32), nullable=False),
    Column("status", String(32), nullable=False),
    Column("payload", JSON, nullable=False),
    UniqueConstraint("cycle_id", "role", name="uq_order_cycle_role"),
)
Index("ix_orders_role_cycle", orders.c.role, orders.c.cycle_id)

fills = Table(
    "fills",
    metadata,
    Column("fill_id", String(128), primary_key=True),
    Column("order_id", ForeignKey("orders.order_id"), nullable=False),
    Column("event_time", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)
Index("ix_fills_order", fills.c.order_id)

position_lifecycles = Table(
    "position_lifecycles",
    metadata,
    Column("position_id", String(128), primary_key=True),
    Column("cycle_id", ForeignKey("analysis_cycles.cycle_id"), nullable=False, unique=True),
    Column("intent_id", ForeignKey("trade_intents.intent_id"), nullable=False, unique=True),
    Column("status", String(32), nullable=False),
    Column("payload", JSON, nullable=False),
)
