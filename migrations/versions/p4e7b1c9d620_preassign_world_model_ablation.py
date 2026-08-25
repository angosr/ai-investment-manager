"""allow WorldModel ablation assignment before formal Forecast completion

Revision ID: p4e7b1c9d620
Revises: n3d6a9b2e518
Create Date: 2026-08-25 07:45:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "p4e7b1c9d620"
down_revision: str | Sequence[str] | None = "n3d6a9b2e518"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "world_model_ablation_assignments_formal_forecast_id_fkey",
        "world_model_ablation_assignments",
        type_="foreignkey",
    )


def downgrade() -> None:
    op.create_foreign_key(
        "world_model_ablation_assignments_formal_forecast_id_fkey",
        "world_model_ablation_assignments",
        "forecasts",
        ["formal_forecast_id"],
        ["forecast_id"],
    )
