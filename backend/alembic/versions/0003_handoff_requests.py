"""add handoff request records

Revision ID: 0003_handoff_requests
Revises: 0002_user_auth_sessions
Create Date: 2026-07-04
"""

import sqlalchemy as sa

from alembic import op

revision = "0003_handoff_requests"
down_revision = "0002_user_auth_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "handoff_request",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("session_id", sa.BigInteger(), nullable=False),
        sa.Column("order_id", sa.BigInteger(), sa.ForeignKey("order_info.id")),
        sa.Column("request_type", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("boundary_category", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "request_type IN ('refund', 'return', 'repair', 'order_change', 'other')",
            name="type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'acknowledged', 'resolved')",
            name="status",
        ),
    )
    op.create_index("idx_handoff_request_user_id", "handoff_request", ["user_id"])
    op.create_index("idx_handoff_request_session_id", "handoff_request", ["session_id"])
    op.create_index("idx_handoff_request_order_id", "handoff_request", ["order_id"])
    op.create_index(
        "idx_handoff_request_user_status",
        "handoff_request",
        ["user_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("idx_handoff_request_user_status", table_name="handoff_request")
    op.drop_index("idx_handoff_request_order_id", table_name="handoff_request")
    op.drop_index("idx_handoff_request_session_id", table_name="handoff_request")
    op.drop_index("idx_handoff_request_user_id", table_name="handoff_request")
    op.drop_table("handoff_request")
