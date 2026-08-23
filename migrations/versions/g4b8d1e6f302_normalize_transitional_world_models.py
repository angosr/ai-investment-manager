"""archive and normalize transitional WorldModel v2 payloads

Revision ID: g4b8d1e6f302
Revises: f3a7c9e2d501
Create Date: 2026-08-23 15:55:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "g4b8d1e6f302"
down_revision: str | Sequence[str] | None = "f3a7c9e2d501"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RETIRED_KEYS = (
    "market_mechanism",
    "mechanism_evidence_ids",
    "drivers",
    "capital_relevance",
    "views",
    "contradictions",
    "data_gaps",
    "hypotheses",
    "decision_blockers",
)


def _retired_key_predicate(dialect: str) -> str:
    if dialect == "postgresql":
        keys = ", ".join(f"'{key}'" for key in _RETIRED_KEYS)
        return f"payload::jsonb ?| ARRAY[{keys}]"
    return " OR ".join(
        f"json_type(payload, '$.{key}') IS NOT NULL" for key in _RETIRED_KEYS
    )


def _canonical_payload_expression(dialect: str) -> str:
    if dialect == "postgresql":
        keys = ", ".join(f"'{key}'" for key in _RETIRED_KEYS)
        return f"(payload::jsonb - ARRAY[{keys}])::json"
    paths = ", ".join(f"'$.{key}'" for key in _RETIRED_KEYS)
    return f"json_remove(payload, {paths})"


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    predicate = _retired_key_predicate(dialect)

    # The active facts are projected onto the sole v2 contract only after their
    # exact transitional payload has been preserved in the immutable archive.
    op.execute(
        sa.text(
            "INSERT INTO historical_context_assessments "
            "SELECT assessment_id, packet_id, analysis_scope, available_at, "
            "analysis_behavior_hash, payload FROM context_assessments "
            "WHERE " + predicate
        )
    )
    op.execute(
        sa.text(
            "UPDATE context_assessments SET payload = "
            + _canonical_payload_expression(dialect)
            + " WHERE "
            + predicate
        )
    )


def downgrade() -> None:
    raise RuntimeError(
        "Transitional WorldModel payloads were archived before normalization; "
        "restoring them requires an explicit audited data migration"
    )
