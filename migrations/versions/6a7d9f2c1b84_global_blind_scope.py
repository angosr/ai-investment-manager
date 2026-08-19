"""prevent repeated reveals of one blind market window

Revision ID: 6a7d9f2c1b84
Revises: b3f6e1a8c920
Create Date: 2026-08-19 21:55:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6a7d9f2c1b84"
down_revision: str | Sequence[str] | None = "b3f6e1a8c920"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("blind_evaluation_claims") as batch:
        batch.add_column(sa.Column("blind_scope_id", sa.String(length=128)))
        batch.add_column(sa.Column("blind_symbol", sa.String(length=32)))
        batch.add_column(sa.Column("blind_start", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("blind_end", sa.DateTime(timezone=True)))

    claim_count = op.get_bind().execute(
        sa.text("SELECT count(*) FROM blind_evaluation_claims")
    ).scalar_one()
    if claim_count:
        raise RuntimeError(
            "已有盲测揭示缺少全局时间窗身份；必须人工审计后才能升级"
        )

    with op.batch_alter_table("blind_evaluation_claims") as batch:
        batch.alter_column(
            "blind_scope_id",
            existing_type=sa.String(length=128),
            nullable=False,
        )
        batch.alter_column(
            "blind_symbol",
            existing_type=sa.String(length=32),
            nullable=False,
        )
        batch.alter_column(
            "blind_start",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )
        batch.alter_column(
            "blind_end",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )
        batch.create_unique_constraint(
            "uq_blind_evaluation_claim_scope",
            ["blind_scope_id"],
        )
        batch.create_index(
            "ix_blind_evaluation_claim_symbol_window",
            ["blind_symbol", "blind_start", "blind_end"],
        )


def downgrade() -> None:
    with op.batch_alter_table("blind_evaluation_claims") as batch:
        batch.drop_index("ix_blind_evaluation_claim_symbol_window")
        batch.drop_constraint(
            "uq_blind_evaluation_claim_scope",
            type_="unique",
        )
        batch.drop_column("blind_end")
        batch.drop_column("blind_start")
        batch.drop_column("blind_symbol")
        batch.drop_column("blind_scope_id")
