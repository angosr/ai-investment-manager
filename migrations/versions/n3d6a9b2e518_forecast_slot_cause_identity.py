"""allow distinct Forecast slot causes at one economic time

Revision ID: n3d6a9b2e518
Revises: m2c5f8a1d407
Create Date: 2026-08-25 06:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "n3d6a9b2e518"
down_revision: str | Sequence[str] | None = "m2c5f8a1d407"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("forecast_decision_slots") as batch:
        batch.drop_constraint(
            "uq_forecast_decision_slot_contract_time",
            type_="unique",
        )


def downgrade() -> None:
    bind = op.get_bind()
    duplicates = bind.scalar(
        sa.text(
            """
            SELECT COUNT(*)
            FROM (
                SELECT contract_id, slot_as_of
                FROM forecast_decision_slots
                GROUP BY contract_id, slot_as_of
                HAVING COUNT(*) > 1
            ) AS duplicate_times
            """
        )
    )
    if duplicates:
        raise RuntimeError("存在同合同同时点的多来源 Forecast 槽，不能恢复旧唯一约束")
    with op.batch_alter_table("forecast_decision_slots") as batch:
        batch.create_unique_constraint(
            "uq_forecast_decision_slot_contract_time",
            ["contract_id", "slot_as_of"],
        )
