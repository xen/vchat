"""add widget waiting messages

Revision ID: h0i1j2k3l4m5
Revises: g0b1c2d3e4f5
Create Date: 2026-06-12 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "h0i1j2k3l4m5"
down_revision: Union[str, None] = "g0b1c2d3e4f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "widget_integration",
        sa.Column(
            "waiting_messages",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[\"Готовлю ответ\"]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("widget_integration", "waiting_messages")
