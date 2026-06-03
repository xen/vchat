"""add source blocked status fields

Revision ID: n1o2p3q4r5s
Revises: m8n9p0q1r2s
Create Date: 2026-06-03 21:10:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "n1o2p3q4r5s"
down_revision: Union[str, None] = "m8n9p0q1r2s"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("source", sa.Column("blocked_reason", sa.String(length=64), nullable=True))
    op.add_column("source", sa.Column("blocked_message", sa.Text(), nullable=True))
    op.add_column(
        "source",
        sa.Column("blocked_checked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("source", "blocked_checked_at")
    op.drop_column("source", "blocked_message")
    op.drop_column("source", "blocked_reason")
