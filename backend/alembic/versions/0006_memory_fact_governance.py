"""add memory fact governance fields

Revision ID: 0006_memory_fact_governance
Revises: 0005_conversation_working_memory
Create Date: 2026-07-06
"""

import sqlalchemy as sa

from alembic import op

revision = "0006_memory_fact_governance"
down_revision = "0005_conversation_working_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "memory_fact",
        sa.Column("scope", sa.String(length=32), nullable=False, server_default="user"),
    )
    op.add_column(
        "memory_fact",
        sa.Column(
            "fact_type",
            sa.String(length=64),
            nullable=False,
            server_default="preference",
        ),
    )
    op.add_column("memory_fact", sa.Column("expires_at", sa.DateTime(timezone=True)))
    op.add_column("memory_fact", sa.Column("last_used_at", sa.DateTime(timezone=True)))
    op.add_column("memory_fact", sa.Column("disabled_at", sa.DateTime(timezone=True)))
    op.create_index(
        "idx_memory_fact_user_scope_type",
        "memory_fact",
        ["user_id", "scope", "fact_type"],
    )
    op.create_index("idx_memory_fact_disabled_at", "memory_fact", ["disabled_at"])
    op.create_index("idx_memory_fact_expires_at", "memory_fact", ["expires_at"])


def downgrade() -> None:
    op.drop_index("idx_memory_fact_expires_at", table_name="memory_fact")
    op.drop_index("idx_memory_fact_disabled_at", table_name="memory_fact")
    op.drop_index("idx_memory_fact_user_scope_type", table_name="memory_fact")
    op.drop_column("memory_fact", "disabled_at")
    op.drop_column("memory_fact", "last_used_at")
    op.drop_column("memory_fact", "expires_at")
    op.drop_column("memory_fact", "fact_type")
    op.drop_column("memory_fact", "scope")
