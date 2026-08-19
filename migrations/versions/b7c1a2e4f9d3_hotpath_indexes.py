"""bounded read-path indexes

给持续运行热路径补最小索引，避免账户、行情、周期和触发计划随历史增长退化。
纯加索引，无数据变更、无行为变化。

Revision ID: b7c1a2e4f9d3
Revises: a61d42f1be90
Create Date: 2026-08-18 13:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7c1a2e4f9d3"
down_revision: str | Sequence[str] | None = "a61d42f1be90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_analysis_trigger_plan_scope_current",
        "analysis_trigger_plans",
        ["symbol", "pipeline_id"],
        unique=True,
        postgresql_where=sa.text("is_current IS TRUE"),
        sqlite_where=sa.text("is_current IS 1"),
    )
    op.create_index(
        "ix_normalized_events_event_time",
        "normalized_events",
        ["event_time"],
        unique=False,
    )
    op.create_index("ix_analysis_cycles_as_of", "analysis_cycles", ["as_of"], unique=False)
    op.create_index(
        "ix_market_snapshots_symbol_as_of",
        "market_snapshots",
        ["symbol", "as_of"],
        unique=False,
    )
    op.create_index("ix_account_snapshots_as_of", "account_snapshots", ["as_of"], unique=False)
    op.create_index("ix_panel_snapshots_as_of", "panel_snapshots", ["as_of"], unique=False)
    op.create_index("ix_orders_role_cycle", "orders", ["role", "cycle_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_orders_role_cycle", table_name="orders")
    op.drop_index("ix_panel_snapshots_as_of", table_name="panel_snapshots")
    op.drop_index("ix_account_snapshots_as_of", table_name="account_snapshots")
    op.drop_index("ix_market_snapshots_symbol_as_of", table_name="market_snapshots")
    op.drop_index("ix_analysis_cycles_as_of", table_name="analysis_cycles")
    op.drop_index("ix_normalized_events_event_time", table_name="normalized_events")
    op.drop_index("uq_analysis_trigger_plan_scope_current", table_name="analysis_trigger_plans")
