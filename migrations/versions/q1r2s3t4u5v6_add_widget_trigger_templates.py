"""Add widget trigger templates.

Revision ID: q1r2s3t4u5v6
Revises: p9q8r7s6t5u4
Create Date: 2026-07-04 22:05:00.000000
"""

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "q1r2s3t4u5v6"
down_revision = "p9q8r7s6t5u4"
branch_labels = None
depends_on = None


DEFAULT_TRIGGER_TEMPLATES = [
    "Хотите узнать больше о {title}?",
    "Помочь разобраться с {title}?",
    "Есть вопросы по {title}?",
    "Показать главное про {title}?",
    "Обсудим детали страницы {title}?",
    "Найти важное в {title}?",
]


def upgrade() -> None:
    default_json = json.dumps(DEFAULT_TRIGGER_TEMPLATES, ensure_ascii=False)
    op.add_column(
        "widget_integration",
        sa.Column(
            "trigger_templates",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text(f"'{default_json}'::jsonb"),
        ),
    )
    op.alter_column(
        "widget_integration",
        "trigger_templates",
        server_default=None,
    )


def downgrade() -> None:
    op.drop_column("widget_integration", "trigger_templates")
