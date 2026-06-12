"""replace widget contact url with footer text

Revision ID: f0a1b2c3d4e5
Revises: e9f0a1b2c3d4
Create Date: 2026-06-12 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "f0a1b2c3d4e5"
down_revision: Union[str, None] = "e9f0a1b2c3d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


DEFAULT_WIDGET_FOOTER_TEXT = (
    '<a href="https://vbudushee.ru/faq/">Пользовательское соглашение</a>.<br>'
    "Отправить Enter, новая строка Shift+Enter"
)


def upgrade() -> None:
    op.add_column(
        "widget_integration",
        sa.Column(
            "footer_text",
            sa.Text(),
            nullable=False,
            server_default=DEFAULT_WIDGET_FOOTER_TEXT,
        ),
    )
    op.drop_column("widget_integration", "contact_url")


def downgrade() -> None:
    op.add_column(
        "widget_integration",
        sa.Column("contact_url", sa.Text(), nullable=False, server_default=""),
    )
    op.drop_column("widget_integration", "footer_text")
