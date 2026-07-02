"""add user session registry

Revision ID: n0o1p2q3r4s5
Revises: m0n1o2p3q4r5
Create Date: 2026-07-02 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op


revision = "n0o1p2q3r4s5"
down_revision = "m0n1o2p3q4r5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_session",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("ip_address", sa.String(length=128), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_user_session_token",
        "user_session",
        ["session_id"],
        unique=True,
    )
    op.create_index(
        "ix_user_session_user_active",
        "user_session",
        ["user_id", "revoked_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_user_session_user_active", table_name="user_session")
    op.drop_index("ix_user_session_token", table_name="user_session")
    op.drop_table("user_session")
