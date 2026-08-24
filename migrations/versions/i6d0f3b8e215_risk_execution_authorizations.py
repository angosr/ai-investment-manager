"""persist risk-owned execution authorizations

Revision ID: i6d0f3b8e215
Revises: h5c9e2a7d104
Create Date: 2026-08-24 09:25:00
"""

import hashlib
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "i6d0f3b8e215"
down_revision: str | Sequence[str] | None = "h5c9e2a7d104"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _content_hash(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def upgrade() -> None:
    op.create_table(
        "risk_execution_authorizations",
        sa.Column("authorization_id", sa.String(length=128), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authorization_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("authorization_id"),
        sa.UniqueConstraint("authorization_hash"),
        sa.UniqueConstraint("source_id"),
    )
    op.create_index(
        "ix_risk_execution_authorizations_time",
        "risk_execution_authorizations",
        ["authorized_at", "authorization_id"],
    )

    connection = op.get_bind()
    decisions = sa.table(
        "portfolio_risk_decisions",
        sa.column("decision_id", sa.String()),
        sa.column("approved_target_id", sa.String()),
        sa.column("decided_at", sa.DateTime(timezone=True)),
        sa.column("payload", sa.JSON()),
    )
    authorizations = sa.table(
        "risk_execution_authorizations",
        sa.column("authorization_id", sa.String()),
        sa.column("source_type", sa.String()),
        sa.column("source_id", sa.String()),
        sa.column("authorized_at", sa.DateTime(timezone=True)),
        sa.column("authorization_hash", sa.String()),
        sa.column("payload", sa.JSON()),
    )
    rows = connection.execute(
        sa.select(decisions).where(decisions.c.approved_target_id.is_not(None))
    ).mappings()
    for row in rows:
        payload = row["payload"]["approved_target"]
        connection.execute(
            authorizations.insert().values(
                authorization_id=row["approved_target_id"],
                source_type="PORTFOLIO_TARGET",
                source_id=row["decision_id"],
                authorized_at=row["decided_at"],
                authorization_hash=_content_hash(payload),
                payload=payload,
            )
        )

    op.add_column(
        "capital_cycle_records",
        sa.Column("execution_authorization_id", sa.String(length=128), nullable=True),
    )
    if connection.dialect.name == "postgresql":
        op.create_foreign_key(
            "capital_cycle_records_execution_authorization_id_fkey",
            "capital_cycle_records",
            "risk_execution_authorizations",
            ["execution_authorization_id"],
            ["authorization_id"],
        )
        op.drop_constraint(
            "trade_plans_approved_target_id_fkey",
            "trade_plans",
            type_="foreignkey",
        )
        op.create_foreign_key(
            "trade_plans_approved_target_id_fkey",
            "trade_plans",
            "risk_execution_authorizations",
            ["approved_target_id"],
            ["authorization_id"],
        )
    else:
        # SQLite cannot alter foreign keys in place.  Batch recreation keeps
        # migrated development/test ledgers structurally identical to a fresh
        # database instead of silently accepting weaker referential integrity.
        naming_convention = {
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"
        }
        with op.batch_alter_table(
            "capital_cycle_records",
            recreate="always",
            naming_convention=naming_convention,
        ) as batch:
            batch.create_foreign_key(
                "fk_capital_cycle_records_execution_authorization_id_"
                "risk_execution_authorizations",
                "risk_execution_authorizations",
                ["execution_authorization_id"],
                ["authorization_id"],
            )
        with op.batch_alter_table(
            "trade_plans",
            recreate="always",
            naming_convention=naming_convention,
        ) as batch:
            batch.drop_constraint(
                "fk_trade_plans_approved_target_id_portfolio_risk_decisions",
                type_="foreignkey",
            )
            batch.create_foreign_key(
                "fk_trade_plans_approved_target_id_risk_execution_authorizations",
                "risk_execution_authorizations",
                ["approved_target_id"],
                ["authorization_id"],
            )


def downgrade() -> None:
    raise RuntimeError(
        "Risk execution authorizations include immutable protective actions and "
        "require an explicit audited restore migration"
    )
