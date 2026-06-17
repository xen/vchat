"""add widget error message

Revision ID: i0j1k2l3m4n5
Revises: h0i1j2k3l4m5
Create Date: 2026-06-17 20:00:00.000000
"""

import sqlalchemy as sa
from alembic import op


revision = "i0j1k2l3m4n5"
down_revision = "h0i1j2k3l4m5"
branch_labels = None
depends_on = None


DEFAULT_ERROR_MESSAGE = (
    "Извините, сейчас не удалось получить ответ. Попробуйте отправить сообщение позже."
)


def upgrade() -> None:
    op.add_column(
        "widget_integration",
        sa.Column(
            "error_message",
            sa.Text(),
            nullable=False,
            server_default=DEFAULT_ERROR_MESSAGE,
        ),
    )
    op.alter_column("widget_integration", "error_message", server_default=None)


def downgrade() -> None:
    op.drop_column("widget_integration", "error_message")
