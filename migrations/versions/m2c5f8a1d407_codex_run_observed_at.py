"""normalize Codex run observation time

Revision ID: m2c5f8a1d407
Revises: l9a4b2d8e306
Create Date: 2026-08-24 20:20:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m2c5f8a1d407"
down_revision: str | Sequence[str] | None = "l9a4b2d8e306"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "codex_runs",
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
    )
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                """
                UPDATE codex_runs
                SET observed_at = COALESCE(
                    CAST(payload ->> 'observed_at' AS timestamptz),
                    CAST(payload ->> 'completed_at' AS timestamptz)
                )
                WHERE observed_at IS NULL
                """
            )
        )
    elif bind.dialect.name == "sqlite":
        op.execute(
            sa.text(
                """
                UPDATE codex_runs
                SET observed_at = COALESCE(
                    json_extract(payload, '$.observed_at'),
                    json_extract(payload, '$.completed_at')
                )
                WHERE observed_at IS NULL
                """
            )
        )
    else:
        raise RuntimeError(f"不支持迁移 codex_runs.observed_at: {bind.dialect.name}")
    missing = bind.scalar(sa.text("SELECT COUNT(*) FROM codex_runs WHERE observed_at IS NULL"))
    if missing:
        raise RuntimeError("历史 Codex 运行缺少可恢复的观测时间")
    with op.batch_alter_table("codex_runs") as batch:
        batch.alter_column(
            "observed_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )
    op.create_index(
        "ix_codex_runs_observed_at",
        "codex_runs",
        ["observed_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_codex_runs_observed_at", table_name="codex_runs")
    with op.batch_alter_table("codex_runs") as batch:
        batch.drop_column("observed_at")
