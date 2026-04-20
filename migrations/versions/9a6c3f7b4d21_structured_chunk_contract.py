"""structured chunk contract

Revision ID: 9a6c3f7b4d21
Revises: f6d8a2c4b9e1
Create Date: 2026-04-14 15:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "9a6c3f7b4d21"
down_revision: Union[str, Sequence[str], None] = "f6d8a2c4b9e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _drop_chunk_fts_triggers() -> None:
    op.execute("DROP TRIGGER IF EXISTS trigger_update_chunk_fts ON chunk")
    op.execute("DROP TRIGGER IF EXISTS trigger_refresh_document_chunk_fts ON document")
    op.execute("DROP FUNCTION IF EXISTS update_chunk_fts()")
    op.execute("DROP FUNCTION IF EXISTS refresh_document_chunk_fts()")


def _create_chunk_fts_triggers() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION update_chunk_fts() RETURNS trigger AS $$
        DECLARE
            document_title text;
        BEGIN
            IF NEW.document_id IS NOT NULL THEN
                SELECT title INTO document_title FROM document WHERE id = NEW.document_id;
            ELSE
                document_title := NULL;
            END IF;

            NEW.fts :=
                setweight(
                    to_tsvector(
                        'russian',
                        coalesce(document_title, '')
                    ) ||
                    to_tsvector(
                        'english',
                        coalesce(document_title, '')
                    ),
                    'A'
                ) ||
                setweight(
                    to_tsvector(
                        'russian',
                        coalesce(NEW.header_text, '')
                    ) ||
                    to_tsvector(
                        'english',
                        coalesce(NEW.header_text, '')
                    ),
                    'A'
                ) ||
                setweight(
                    to_tsvector(
                        'russian',
                        coalesce(NEW.section_path, '')
                    ) ||
                    to_tsvector(
                        'english',
                        coalesce(NEW.section_path, '')
                    ) ||
                    to_tsvector(
                        'simple',
                        coalesce(array_to_string(NEW.entity_terms, ' '), '')
                    ),
                    'B'
                ) ||
                setweight(
                    to_tsvector(
                        'russian',
                        coalesce(NEW.text, '')
                    ) ||
                    to_tsvector(
                        'english',
                        coalesce(NEW.text, '')
                    ),
                    'C'
                );
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trigger_update_chunk_fts
        BEFORE INSERT OR UPDATE OF text, header_text, section_path, entity_terms, document_id
        ON chunk
        FOR EACH ROW
        EXECUTE FUNCTION update_chunk_fts();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION refresh_document_chunk_fts() RETURNS trigger AS $$
        BEGIN
            UPDATE chunk
            SET header_text = header_text
            WHERE document_id = NEW.id;
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trigger_refresh_document_chunk_fts
        AFTER UPDATE OF title
        ON document
        FOR EACH ROW
        EXECUTE FUNCTION refresh_document_chunk_fts();
        """
    )


def upgrade() -> None:
    _drop_chunk_fts_triggers()
    op.execute("TRUNCATE TABLE chunk RESTART IDENTITY")

    op.alter_column("chunk", "content", new_column_name="text")
    op.alter_column("chunk", "tsv", new_column_name="fts")
    op.add_column(
        "chunk",
        sa.Column(
            "kind",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'text'"),
        ),
    )
    op.add_column("chunk", sa.Column("header_text", sa.Text(), nullable=True))
    op.add_column("chunk", sa.Column("section_path", sa.Text(), nullable=True))
    op.add_column("chunk", sa.Column("entity_terms", sa.ARRAY(sa.String()), nullable=True))
    op.add_column(
        "chunk",
        sa.Column(
            "token_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    op.execute("DROP INDEX IF EXISTS ix_chunk_tsv")
    op.execute("DROP INDEX IF EXISTS ix_chunk_fts")
    op.execute("CREATE INDEX IF NOT EXISTS ix_chunk_fts ON chunk USING GIN (fts)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_chunk_kind ON chunk (kind)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chunk_document_kind ON chunk (document_id, kind)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_chunk_chat_kind ON chunk (chat_id, kind)")

    _create_chunk_fts_triggers()


def downgrade() -> None:
    _drop_chunk_fts_triggers()
    op.execute("DROP INDEX IF EXISTS ix_chunk_chat_kind")
    op.execute("DROP INDEX IF EXISTS ix_chunk_document_kind")
    op.execute("DROP INDEX IF EXISTS ix_chunk_kind")
    op.execute("DROP INDEX IF EXISTS ix_chunk_fts")
    op.execute("CREATE INDEX IF NOT EXISTS ix_chunk_tsv ON chunk USING GIN (fts)")

    op.drop_column("chunk", "token_count")
    op.drop_column("chunk", "entity_terms")
    op.drop_column("chunk", "section_path")
    op.drop_column("chunk", "header_text")
    op.drop_column("chunk", "kind")
    op.alter_column("chunk", "fts", new_column_name="tsv")
    op.alter_column("chunk", "text", new_column_name="content")
