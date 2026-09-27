"""add SPU sales count

Revision ID: 0004_spu_sales_count
Revises: 0003_handoff_requests
Create Date: 2026-07-05
"""

import sqlalchemy as sa

from alembic import op

revision = "0004_spu_sales_count"
down_revision = "0003_handoff_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "spu",
        sa.Column(
            "sales_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_check_constraint(
        "ck_spu_sales_count_non_negative",
        "spu",
        "sales_count >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_spu_sales_count_non_negative", "spu", type_="check")
    op.drop_column("spu", "sales_count")
