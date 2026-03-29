"""add full text search indexes for chat history

Revision ID: a7c1d9e4b2f0
Revises: 5d2f0f8c1a9e
Create Date: 2026-03-30 14:10:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "a7c1d9e4b2f0"
down_revision: Union[str, Sequence[str], None] = "5d2f0f8c1a9e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_chat_msg_text_fts_simple
        ON chat_msg
        USING GIN (to_tsvector('simple', coalesce(text, '')))
        WHERE role IN ('user', 'assistant')
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_chat_search_fts_simple
        ON chat
        USING GIN (
            to_tsvector(
                'simple',
                (coalesce(title, '') || ' ' || coalesce(user_uid, ''))
            )
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chat_search_fts_simple")
    op.execute("DROP INDEX IF EXISTS ix_chat_msg_text_fts_simple")
