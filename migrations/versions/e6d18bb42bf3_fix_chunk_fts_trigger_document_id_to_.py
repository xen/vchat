"""fix_chunk_fts_trigger_document_id_to_page_id

Revision ID: e6d18bb42bf3
Revises: d6e7f8a9b0c1
Create Date: 2026-05-31 09:13:20.949813

"""

from alembic import op

revision = "e6d18bb42bf3"
down_revision = "d6e7f8a9b0c1"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DROP TRIGGER IF EXISTS trigger_update_chunk_fts ON chunk")
    op.execute("DROP FUNCTION IF EXISTS update_chunk_fts()")
    op.execute("""
        CREATE OR REPLACE FUNCTION update_chunk_fts() RETURNS trigger AS $$
        DECLARE
            page_title text;
        BEGIN
            IF NEW.page_id IS NOT NULL THEN
                SELECT title INTO page_title FROM page WHERE id = NEW.page_id;
            ELSE
                page_title := NULL;
            END IF;

            NEW.fts :=
                setweight(
                    to_tsvector('russian', coalesce(page_title, '')) ||
                    to_tsvector('english', coalesce(page_title, '')),
                    'A'
                ) ||
                setweight(
                    to_tsvector('russian', coalesce(NEW.header_text, '')) ||
                    to_tsvector('english', coalesce(NEW.header_text, '')),
                    'A'
                ) ||
                setweight(
                    to_tsvector('russian', coalesce(NEW.section_path, '')) ||
                    to_tsvector('english', coalesce(NEW.section_path, '')) ||
                    to_tsvector('simple', coalesce(array_to_string(NEW.entity_terms, ' '), '')),
                    'B'
                ) ||
                setweight(
                    to_tsvector('russian', coalesce(NEW.text, '')) ||
                    to_tsvector('english', coalesce(NEW.text, '')),
                    'C'
                );
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trigger_update_chunk_fts
        BEFORE INSERT OR UPDATE OF text, header_text, section_path, entity_terms, page_id
        ON chunk
        FOR EACH ROW EXECUTE FUNCTION update_chunk_fts()
    """)


def downgrade():
    op.execute("DROP TRIGGER IF EXISTS trigger_update_chunk_fts ON chunk")
    op.execute("DROP FUNCTION IF EXISTS update_chunk_fts()")
    op.execute("""
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
                    to_tsvector('russian', coalesce(document_title, '')) ||
                    to_tsvector('english', coalesce(document_title, '')),
                    'A'
                ) ||
                setweight(
                    to_tsvector('russian', coalesce(NEW.header_text, '')) ||
                    to_tsvector('english', coalesce(NEW.header_text, '')),
                    'A'
                ) ||
                setweight(
                    to_tsvector('russian', coalesce(NEW.section_path, '')) ||
                    to_tsvector('english', coalesce(NEW.section_path, '')) ||
                    to_tsvector('simple', coalesce(array_to_string(NEW.entity_terms, ' '), '')),
                    'B'
                ) ||
                setweight(
                    to_tsvector('russian', coalesce(NEW.text, '')) ||
                    to_tsvector('english', coalesce(NEW.text, '')),
                    'C'
                );
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trigger_update_chunk_fts
        BEFORE INSERT OR UPDATE OF text, header_text, section_path, entity_terms, document_id
        ON chunk
        FOR EACH ROW EXECUTE FUNCTION update_chunk_fts()
    """)
