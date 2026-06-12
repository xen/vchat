"""replace widget welcome message with message list

Revision ID: g0b1c2d3e4f5
Revises: f0a1b2c3d4e5
Create Date: 2026-06-12 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "g0b1c2d3e4f5"
down_revision: Union[str, None] = "f0a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "widget_integration",
        sa.Column(
            "welcome_messages",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.execute(
        """
        UPDATE widget_integration
        SET welcome_messages = jsonb_build_array(welcome_message)
        WHERE nullif(btrim(welcome_message), '') IS NOT NULL
        """
    )
    op.drop_column("widget_integration", "welcome_message")


def downgrade() -> None:
    op.add_column(
        "widget_integration",
        sa.Column("welcome_message", sa.Text(), nullable=False, server_default=""),
    )
    op.execute(
        """
        UPDATE widget_integration
        SET welcome_message = coalesce(welcome_messages->>0, '')
        """
    )
    op.drop_column("widget_integration", "welcome_messages")
