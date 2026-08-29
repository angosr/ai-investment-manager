"""retire cash yield behavior while preserving release overlap

Revision ID: z5e0c8a3f164
Revises: y4d9b7f2e253
Create Date: 2026-08-29 00:00:00
"""

from collections.abc import Sequence

revision: str = "z5e0c8a3f164"
down_revision: str | Sequence[str] | None = "y4d9b7f2e253"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The preceding release still imports the retired table.  Mark the code
    # transition first so it can remain a valid rollback target; a later head
    # may physically remove the table after every compatible release has
    # stopped reading it.
    pass


def downgrade() -> None:
    pass
