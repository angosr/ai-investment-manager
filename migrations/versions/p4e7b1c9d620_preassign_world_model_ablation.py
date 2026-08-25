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

_NAMING_CONVENTION = {"fk": "%(table_name)s_%(column_0_name)s_fkey"}


def upgrade() -> None:
    with op.batch_alter_table(
        "world_model_ablation_assignments",
        naming_convention=_NAMING_CONVENTION,
    ) as batch:
        batch.drop_constraint(
            "world_model_ablation_assignments_formal_forecast_id_fkey",
            type_="foreignkey",
        )


def downgrade() -> None:
    with op.batch_alter_table(
        "world_model_ablation_assignments",
        naming_convention=_NAMING_CONVENTION,
    ) as batch:
        batch.create_foreign_key(
            "world_model_ablation_assignments_formal_forecast_id_fkey",
            "forecasts",
            ["formal_forecast_id"],
            ["forecast_id"],
        )
