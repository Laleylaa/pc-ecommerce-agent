"""add conversation working memory

Revision ID: 0005_conversation_working_memory
Revises: 0004_spu_sales_count
Create Date: 2026-07-06
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0005_conversation_working_memory"
down_revision = "0004_spu_sales_count"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversation",
        sa.Column("working_memory_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversation", "working_memory_json")
