"""add chunk text deduplication

Revision ID: aa1b2c3d4e5f
Revises: z2a3b4c5d6e7
Create Date: 2026-06-07 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "aa1b2c3d4e5f"
down_revision: Union[str, None] = "z2a3b4c5d6e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("chunk", sa.Column("text_hash", sa.String(length=64), nullable=True))
    op.add_column(
        "chunk",
        sa.Column(
            "is_duplicate",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "chunk", sa.Column("duplicate_of_chunk_id", sa.Integer(), nullable=True)
    )
    op.create_index(op.f("ix_chunk_text_hash"), "chunk", ["text_hash"], unique=False)
    op.create_index(
        op.f("ix_chunk_is_duplicate"), "chunk", ["is_duplicate"], unique=False
    )
    op.create_index(
        op.f("ix_chunk_duplicate_of_chunk_id"),
        "chunk",
        ["duplicate_of_chunk_id"],
        unique=False,
    )

    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute(
        """
        UPDATE chunk
        SET text_hash = encode(
            sha256(
                convert_to(
                    btrim(translate(text, U&'\\200B\\200C\\200D\\FEFF', '')),
                    'UTF8'
                )
            ),
            'hex'
        )
        WHERE text IS NOT NULL
          AND btrim(translate(text, U&'\\200B\\200C\\200D\\FEFF', '')) <> ''
        """
    )
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                first_value(id) OVER (
                    PARTITION BY page_id, text_hash
                    ORDER BY id
                ) AS canonical_id,
                row_number() OVER (
                    PARTITION BY page_id, text_hash
                    ORDER BY id
                ) AS rn
            FROM chunk
            WHERE chat_id IS NULL
              AND page_id IS NOT NULL
              AND text_hash IS NOT NULL
        )
        UPDATE chunk AS c
        SET
            is_duplicate = true,
            duplicate_of_chunk_id = ranked.canonical_id,
            embedding = NULL
        FROM ranked
        WHERE c.id = ranked.id
          AND ranked.rn > 1
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION promote_duplicate_chunk_on_delete()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            promoted_id integer;
        BEGIN
            IF OLD.is_duplicate = false
               AND OLD.text_hash IS NOT NULL
               AND OLD.chat_id IS NULL
               AND OLD.page_id IS NOT NULL THEN
                SELECT id
                INTO promoted_id
                FROM chunk
                WHERE text_hash = OLD.text_hash
                  AND page_id = OLD.page_id
                  AND is_duplicate = true
                  AND chat_id IS NULL
                  AND page_id IS NOT NULL
                  AND id <> OLD.id
                ORDER BY id
                LIMIT 1;

                IF promoted_id IS NOT NULL THEN
                    UPDATE chunk
                    SET
                        is_duplicate = false,
                        duplicate_of_chunk_id = NULL,
                        embedding = OLD.embedding
                    WHERE id = promoted_id;

                    UPDATE chunk
                    SET duplicate_of_chunk_id = promoted_id
                    WHERE text_hash = OLD.text_hash
                      AND page_id = OLD.page_id
                      AND is_duplicate = true
                      AND chat_id IS NULL
                      AND page_id IS NOT NULL
                      AND id <> promoted_id;
                END IF;
            END IF;

            RETURN OLD;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trigger_promote_duplicate_chunk_on_delete
        BEFORE DELETE ON chunk
        FOR EACH ROW
        EXECUTE FUNCTION promote_duplicate_chunk_on_delete()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trigger_promote_duplicate_chunk_on_delete ON chunk"
    )
    op.execute("DROP FUNCTION IF EXISTS promote_duplicate_chunk_on_delete()")
    op.drop_index(op.f("ix_chunk_duplicate_of_chunk_id"), table_name="chunk")
    op.drop_index(op.f("ix_chunk_is_duplicate"), table_name="chunk")
    op.drop_index(op.f("ix_chunk_text_hash"), table_name="chunk")
    op.drop_column("chunk", "duplicate_of_chunk_id")
    op.drop_column("chunk", "is_duplicate")
    op.drop_column("chunk", "text_hash")
