"""allow one forecast behavior to receive future capital admission

Revision ID: d8f4a1c9e620
Revises: c7a2e0d5b386
Create Date: 2026-08-29 13:30:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d8f4a1c9e620"
down_revision: str | Sequence[str] | None = "c7a2e0d5b386"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("forecast_producer_bindings") as batch:
        batch.drop_constraint(
            "uq_forecast_binding_contract_behavior",
            type_="unique",
        )
        batch.create_unique_constraint(
            "uq_forecast_binding_contract_behavior_permission",
            ("contract_id", "producer_behavior_id", "permission"),
        )


def downgrade() -> None:
    raise RuntimeError(
        "Forecast admission bindings are immutable facts and require an audited restore"
    )
