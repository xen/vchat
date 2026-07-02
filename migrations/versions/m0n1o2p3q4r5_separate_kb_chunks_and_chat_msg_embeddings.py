"""separate kb chunks and chat message embeddings

Revision ID: m0n1o2p3q4r5
Revises: l0m1n2o3p4q5
Create Date: 2026-06-25 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector


revision = "m0n1o2p3q4r5"
down_revision = "l0m1n2o3p4q5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_msg", sa.Column("embedding", Vector(1024), nullable=True))
    op.add_column(
        "chat_msg",
        sa.Column("text_hash", sa.String(length=64), nullable=True),
    )
    op.create_index("ix_chat_msg_text_hash", "chat_msg", ["text_hash"])
    op.create_check_constraint(
        "ck_chat_msg_user_text_max_4000",
        "chat_msg",
        "role <> 'user' OR char_length(text) <= 4000",
    )

    op.execute("DELETE FROM chunk WHERE msg_id IS NOT NULL OR chat_id IS NOT NULL")
    op.drop_index("ix_chunk_embedding_chat_hnsw_cosine", table_name="chunk")
    op.drop_index("ix_chunk_embedding_kb_hnsw_cosine", table_name="chunk")
    op.drop_index("ix_chunk_chat_kind", table_name="chunk")
    op.drop_index("ix_chunk_chat_id", table_name="chunk")
    op.drop_index("ix_chunk_msg_id", table_name="chunk")
    op.drop_constraint("chunk_msg_id_fkey", "chunk", type_="foreignkey")
    op.drop_constraint("fk_chunk_chat_id_chat", "chunk", type_="foreignkey")
    op.drop_column("chunk", "msg_id")
    op.drop_column("chunk", "chat_id")
    op.execute(
        """
        CREATE INDEX ix_chunk_embedding_kb_hnsw_cosine
        ON chunk
        USING hnsw (embedding vector_cosine_ops)
        WHERE page_id IS NOT NULL
          AND is_duplicate = false
          AND embedding IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_chunk_embedding_kb_hnsw_cosine", table_name="chunk")
    op.add_column("chunk", sa.Column("chat_id", sa.String(length=36), nullable=True))
    op.add_column("chunk", sa.Column("msg_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_chunk_chat_id_chat",
        "chunk",
        "chat",
        ["chat_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "chunk_msg_id_fkey",
        "chunk",
        "chat_msg",
        ["msg_id"],
        ["id"],
    )
    op.create_index("ix_chunk_msg_id", "chunk", ["msg_id"])
    op.create_index("ix_chunk_chat_id", "chunk", ["chat_id"])
    op.create_index("ix_chunk_chat_kind", "chunk", ["chat_id", "kind"])
    op.execute(
        """
        CREATE INDEX ix_chunk_embedding_chat_hnsw_cosine
        ON chunk
        USING hnsw (embedding vector_cosine_ops)
        WHERE chat_id IS NOT NULL
          AND is_duplicate = false
          AND embedding IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX ix_chunk_embedding_kb_hnsw_cosine
        ON chunk
        USING hnsw (embedding vector_cosine_ops)
        WHERE chat_id IS NULL
          AND page_id IS NOT NULL
          AND is_duplicate = false
          AND embedding IS NOT NULL
        """
    )

    op.drop_constraint("ck_chat_msg_user_text_max_4000", "chat_msg", type_="check")
    op.drop_index("ix_chat_msg_text_hash", table_name="chat_msg")
    op.drop_column("chat_msg", "text_hash")
    op.drop_column("chat_msg", "embedding")
