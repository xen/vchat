"""add chunk embedding hnsw indexes

Revision ID: d8e9f0a1b2c3
Revises: ab4c7d8e9f01
Create Date: 2026-06-08 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "d8e9f0a1b2c3"
down_revision: Union[str, None] = "ab4c7d8e9f01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
                ix_chunk_embedding_kb_hnsw_cosine
            ON chunk
            USING hnsw (embedding vector_cosine_ops)
            WHERE chat_id IS NULL
              AND page_id IS NOT NULL
              AND is_duplicate = false
              AND embedding IS NOT NULL
            """
        )
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
                ix_chunk_embedding_chat_hnsw_cosine
            ON chunk
            USING hnsw (embedding vector_cosine_ops)
            WHERE chat_id IS NOT NULL
              AND is_duplicate = false
              AND embedding IS NOT NULL
            """
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_chunk_embedding_chat_hnsw_cosine")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_chunk_embedding_kb_hnsw_cosine")
