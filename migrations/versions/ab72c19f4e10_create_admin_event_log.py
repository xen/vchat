"""create admin event log table

Revision ID: ab72c19f4e10
Revises: f3b5c8d1e2a4
Create Date: 2026-03-31 18:20:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "ab72c19f4e10"
down_revision: Union[str, Sequence[str], None] = "f3b5c8d1e2a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "admin_event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("user_email", sa.String(length=254), nullable=False),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("event_name", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_admin_event_user_id"), "admin_event", ["user_id"], unique=False)
    op.create_index(op.f("ix_admin_event_event_name"), "admin_event", ["event_name"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_admin_event_event_name"), table_name="admin_event")
    op.drop_index(op.f("ix_admin_event_user_id"), table_name="admin_event")
    op.drop_table("admin_event")
