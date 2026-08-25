"""archive retired single-asset account benchmark facts

Revision ID: q5f8c2d1e731
Revises: p4e7b1c9d620
Create Date: 2026-08-25 08:30:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "q5f8c2d1e731"
down_revision: str | Sequence[str] | None = "p4e7b1c9d620"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.rename_table("capital_benchmark_points", "historical_capital_benchmark_points")
    op.drop_index(
        "uq_capital_benchmark_policy_account",
        table_name="historical_capital_benchmark_points",
    )
    op.drop_index(
        "ix_capital_benchmark_policy_revision",
        table_name="historical_capital_benchmark_points",
    )
    op.create_index(
        "uq_historical_capital_benchmark_policy_account",
        "historical_capital_benchmark_points",
        ["policy_id", "account_snapshot_id"],
        unique=True,
    )
    op.create_index(
        "ix_historical_capital_benchmark_policy_revision",
        "historical_capital_benchmark_points",
        ["policy_id", "revision"],
    )


def downgrade() -> None:
    op.drop_index(
        "uq_historical_capital_benchmark_policy_account",
        table_name="historical_capital_benchmark_points",
    )
    op.drop_index(
        "ix_historical_capital_benchmark_policy_revision",
        table_name="historical_capital_benchmark_points",
    )
    op.rename_table("historical_capital_benchmark_points", "capital_benchmark_points")
    op.create_index(
        "uq_capital_benchmark_policy_account",
        "capital_benchmark_points",
        ["policy_id", "account_snapshot_id"],
        unique=True,
    )
    op.create_index(
        "ix_capital_benchmark_policy_revision",
        "capital_benchmark_points",
        ["policy_id", "revision"],
    )
