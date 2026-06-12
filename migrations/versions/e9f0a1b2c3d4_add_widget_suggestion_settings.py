"""add widget suggestion settings

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
Create Date: 2026-06-12 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e9f0a1b2c3d4"
down_revision: Union[str, None] = "d8e9f0a1b2c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


DEFAULT_SUGGESTIONS_PROMPT = """Ты генерируешь подсказки для продолжения диалога в чат-виджете.

Сгенерируй 2-3 коротких следующих вопроса или действия от лица пользователя.
Подсказки должны быть напрямую связаны с последним вопросом, финальным ответом ассистента и использованными источниками.
Не повторяй уже отвеченный вопрос. Не придумывай факты, которых нет в ответе или источниках.
Пиши на языке последнего вопроса пользователя.
"""


def upgrade() -> None:
    op.add_column(
        "widget_integration",
        sa.Column(
            "suggestions_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "widget_integration",
        sa.Column(
            "suggestions_prompt",
            sa.Text(),
            nullable=False,
            server_default=DEFAULT_SUGGESTIONS_PROMPT,
        ),
    )


def downgrade() -> None:
    op.drop_column("widget_integration", "suggestions_prompt")
    op.drop_column("widget_integration", "suggestions_enabled")
