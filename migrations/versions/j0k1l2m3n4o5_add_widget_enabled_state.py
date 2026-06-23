"""add widget enabled state

Revision ID: j0k1l2m3n4o5
Revises: i0j1k2l3m4n5
Create Date: 2026-06-17 21:00:00.000000
"""

import sqlalchemy as sa
from alembic import op


revision = "j0k1l2m3n4o5"
down_revision = "i0j1k2l3m4n5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "widget_integration",
        sa.Column(
            "is_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("widget_integration", "is_enabled")
