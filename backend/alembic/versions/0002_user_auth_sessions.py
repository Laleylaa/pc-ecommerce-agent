"""add user auth credentials and sessions

Revision ID: 0002_user_auth_sessions
Revises: 0001_initial_schema
Create Date: 2026-07-03
"""

import sqlalchemy as sa

from alembic import op

revision = "0002_user_auth_sessions"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("app_user", sa.Column("login_identifier", sa.String(length=128), nullable=True))
    op.add_column(
        "app_user",
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
    )
    op.add_column("app_user", sa.Column("last_login_at", sa.DateTime(timezone=True)))
    op.add_column(
        "app_user",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.execute(
        """
        UPDATE app_user
        SET login_identifier = 'user-' || id::text
        WHERE login_identifier IS NULL
        """
    )
    op.alter_column("app_user", "login_identifier", nullable=False)
    op.create_unique_constraint(
        "uq_app_user_login_identifier", "app_user", ["login_identifier"]
    )

    op.create_table(
        "user_auth_credential",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("login_identifier", sa.String(length=128), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "password_updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
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
        sa.UniqueConstraint("user_id", name="uq_user_auth_credential_user_id"),
        sa.UniqueConstraint(
            "login_identifier", name="uq_user_auth_credential_login_identifier"
        ),
    )
    op.create_index(
        "idx_user_auth_login_identifier",
        "user_auth_credential",
        ["login_identifier"],
    )

    op.create_table(
        "user_session",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("refresh_token_hash", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("user_agent", sa.String(length=255)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "last_used_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("refresh_token_hash", name="uq_user_session_refresh_token_hash"),
    )
    op.create_index("idx_user_session_user_id", "user_session", ["user_id"])
    op.create_index(
        "idx_user_session_status_expires",
        "user_session",
        ["status", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_user_session_status_expires", table_name="user_session")
    op.drop_index("idx_user_session_user_id", table_name="user_session")
    op.drop_table("user_session")
    op.drop_index("idx_user_auth_login_identifier", table_name="user_auth_credential")
    op.drop_table("user_auth_credential")
    op.drop_constraint("uq_app_user_login_identifier", "app_user", type_="unique")
    op.drop_column("app_user", "updated_at")
    op.drop_column("app_user", "last_login_at")
    op.drop_column("app_user", "status")
    op.drop_column("app_user", "login_identifier")
