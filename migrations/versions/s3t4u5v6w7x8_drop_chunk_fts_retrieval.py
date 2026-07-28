"""Drop legacy chunk FTS retrieval artifacts.

Revision ID: s3t4u5v6w7x8
Revises: r2s3t4u5v6w7
Create Date: 2026-07-10 00:10:00.000000
"""

from alembic import op


revision = "s3t4u5v6w7x8"
down_revision = "r2s3t4u5v6w7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trigger_refresh_document_chunk_fts ON page")
    op.execute("DROP TRIGGER IF EXISTS trigger_update_chunk_fts ON chunk")
    op.execute("DROP FUNCTION IF EXISTS refresh_document_chunk_fts()")
    op.execute("DROP FUNCTION IF EXISTS update_chunk_fts()")
    op.execute("DROP INDEX IF EXISTS ix_chunk_fts")
    op.drop_column("chunk", "fts")


def downgrade() -> None:
    op.execute("ALTER TABLE chunk ADD COLUMN fts tsvector")
    op.execute(
        """
        CREATE FUNCTION update_chunk_fts() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            page_title text;
        BEGIN
            SELECT title INTO page_title FROM page WHERE id = NEW.page_id;
            NEW.fts :=
                setweight(to_tsvector('russian', coalesce(page_title, '')), 'A') ||
                setweight(to_tsvector('russian', coalesce(NEW.header_text, '')), 'A') ||
                setweight(to_tsvector('russian', coalesce(NEW.section_path, '')), 'B') ||
                setweight(to_tsvector('russian', coalesce(array_to_string(NEW.entity_terms, ' '), '')), 'B') ||
                setweight(to_tsvector('russian', coalesce(NEW.text, '')), 'C');
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION refresh_document_chunk_fts() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            UPDATE chunk SET text = text WHERE page_id = NEW.id;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trigger_update_chunk_fts
        BEFORE INSERT OR UPDATE OF text, header_text, section_path, entity_terms, page_id
        ON chunk
        FOR EACH ROW EXECUTE FUNCTION update_chunk_fts()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trigger_refresh_document_chunk_fts
        AFTER UPDATE OF title ON page
        FOR EACH ROW EXECUTE FUNCTION refresh_document_chunk_fts()
        """
    )
    op.execute("CREATE INDEX ix_chunk_fts ON chunk USING gin (fts)")
