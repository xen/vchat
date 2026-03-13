"""partition_chunk

Revision ID: a5420397d268
Revises: fd5ec2ad7aba
Create Date: 2025-12-02 04:01:37.630441

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "a5420397d268"
down_revision = "fd5ec2ad7aba"
branch_labels = None
depends_on = None


def upgrade():
    # 1. Rename old table
    op.rename_table("chunk", "chunk_old")

    # Rename old indexes to avoid conflict with new table's indexes
    # Postgres index names must be unique in the schema
    op.execute("ALTER INDEX IF EXISTS chunk_pkey RENAME TO chunk_old_pkey")
    op.execute(
        "ALTER INDEX IF EXISTS ix_chunk_user_uid RENAME TO ix_chunk_old_user_uid"
    )
    op.execute("ALTER INDEX IF EXISTS ix_chunk_chat_id RENAME TO ix_chunk_old_chat_id")
    op.execute(
        "ALTER INDEX IF EXISTS ix_chunk_document_id RENAME TO ix_chunk_old_document_id"
    )
    op.execute("ALTER INDEX IF EXISTS ix_chunk_msg_id RENAME TO ix_chunk_old_msg_id")
    # Also rename embedding index if it exists (might be named differently, but let's try standard naming or skip if not sure)
    # Assuming it was not created by default SA, but maybe manually. If it fails, we'll know.

    # 2. Create new partitioned table
    op.execute("""
        CREATE TABLE chunk (
            id SERIAL,
            chat_id INTEGER,
            user_uid VARCHAR(256) NOT NULL,
            msg_id INTEGER,
            document_id INTEGER,
            chunk_ix INTEGER NOT NULL,
            start_offset INTEGER,
            end_offset INTEGER,
            content TEXT NOT NULL,
            tsv TSVECTOR,
            embedding VECTOR(1024),
            project_id INTEGER NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE,
            PRIMARY KEY (id, project_id),
            FOREIGN KEY (chat_id) REFERENCES chat(id) ON DELETE CASCADE,
            FOREIGN KEY (msg_id) REFERENCES chat_msg(id),
            FOREIGN KEY (document_id) REFERENCES document(id) ON DELETE CASCADE,
            FOREIGN KEY (project_id) REFERENCES project(id) ON DELETE CASCADE
        ) PARTITION BY HASH (project_id);
    """)

    # 3. Create partitions (64 partitions)
    for i in range(64):
        op.execute(
            f"CREATE TABLE chunk_p{i} PARTITION OF chunk FOR VALUES WITH (MODULUS 64, REMAINDER {i});"
        )

    # 4. Migrate data
    op.execute("""
        INSERT INTO chunk (
            id, chat_id, user_uid, msg_id, document_id, chunk_ix, start_offset, end_offset,
            content, tsv, embedding, project_id, created_at, updated_at
        )
        SELECT
            c.id, c.chat_id, c.user_uid, c.msg_id, c.document_id, c.chunk_ix, c.start_offset, c.end_offset,
            c.content, c.tsv, c.embedding,
            COALESCE(s.project_id, ch.project_id) as project_id,
            c.created_at, c.updated_at
        FROM chunk_old c
        LEFT JOIN document d ON c.document_id = d.id
        LEFT JOIN source s ON d.source_id = s.id
        LEFT JOIN chat ch ON c.chat_id = ch.id
        WHERE COALESCE(s.project_id, ch.project_id) IS NOT NULL;
    """)

    # 5. Sync sequence
    op.execute(
        "SELECT setval(pg_get_serial_sequence('chunk', 'id'), COALESCE((SELECT MAX(id) FROM chunk), 1));"
    )

    # 6. Create Indexes
    op.create_index("ix_chunk_project_id", "chunk", ["project_id"])
    op.create_index("ix_chunk_user_uid", "chunk", ["user_uid"])
    op.create_index("ix_chunk_chat_id", "chunk", ["chat_id"])
    op.create_index("ix_chunk_document_id", "chunk", ["document_id"])
    op.create_index("ix_chunk_msg_id", "chunk", ["msg_id"])

    # HNSW Index
    op.execute(
        "CREATE INDEX ix_chunk_embedding ON chunk USING hnsw (embedding vector_cosine_ops);"
    )

    # 7. Drop old table
    op.drop_table("chunk_old")


def downgrade():
    op.drop_table("chunk")
    op.rename_table("chunk_old", "chunk")

    # Rename indexes back
    op.execute("ALTER INDEX IF EXISTS chunk_old_pkey RENAME TO chunk_pkey")
    op.execute(
        "ALTER INDEX IF EXISTS ix_chunk_old_user_uid RENAME TO ix_chunk_user_uid"
    )
    op.execute("ALTER INDEX IF EXISTS ix_chunk_old_chat_id RENAME TO ix_chunk_chat_id")
    op.execute(
        "ALTER INDEX IF EXISTS ix_chunk_old_document_id RENAME TO ix_chunk_document_id"
    )
    op.execute("ALTER INDEX IF EXISTS ix_chunk_old_msg_id RENAME TO ix_chunk_msg_id")
