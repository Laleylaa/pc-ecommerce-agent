"""add structured explicit memory governance

Revision ID: 0007_context_memory_v2
Revises: 0006_memory_fact_governance
Create Date: 2026-07-11
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0007_context_memory_v2"
down_revision = "0006_memory_fact_governance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "memory_fact",
        sa.Column("value_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "memory_fact",
        sa.Column(
            "origin",
            sa.String(length=32),
            nullable=False,
            server_default="legacy_inferred",
        ),
    )
    op.execute(
        """
        WITH ranked_active_memories AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY user_id, scope, fact_type, key
                       ORDER BY updated_at DESC, id DESC
                   ) AS duplicate_rank
            FROM memory_fact
            WHERE disabled_at IS NULL
        )
        UPDATE memory_fact
        SET disabled_at = now(), updated_at = now()
        WHERE id IN (
            SELECT id
            FROM ranked_active_memories
            WHERE duplicate_rank > 1
        )
        """
    )
    op.create_index(
        "uq_memory_fact_active_identity",
        "memory_fact",
        ["user_id", "scope", "fact_type", "key"],
        unique=True,
        postgresql_where=sa.text("disabled_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_memory_fact_active_identity", table_name="memory_fact")
    op.drop_column("memory_fact", "origin")
    op.drop_column("memory_fact", "value_json")
