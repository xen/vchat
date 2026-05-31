"""fix_refresh_document_chunk_fts_trigger

Revision ID: 3824f23ac6a5
Revises: d3e4f5a6b7c8
Create Date: 2026-05-31 16:48:20.716554

"""
from alembic import op

revision = '3824f23ac6a5'
down_revision = 'd3e4f5a6b7c8'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE OR REPLACE FUNCTION refresh_document_chunk_fts()
        RETURNS trigger AS $$
        BEGIN
            UPDATE chunk
            SET header_text = header_text
            WHERE page_id = NEW.id;
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql;
    """)


def downgrade():
    op.execute("""
        CREATE OR REPLACE FUNCTION refresh_document_chunk_fts()
        RETURNS trigger AS $$
        BEGIN
            UPDATE chunk
            SET header_text = header_text
            WHERE document_id = NEW.id;
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql;
    """)
