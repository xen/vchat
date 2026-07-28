"""Add pg_search BM25 indexes for page and chunk retrieval.

Revision ID: r2s3t4u5v6w7
Revises: q1r2s3t4u5v6
Create Date: 2026-07-10 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op


revision = "r2s3t4u5v6w7"
down_revision = "q1r2s3t4u5v6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_search")
    op.add_column(
        "page",
        sa.Column(
            "uri_slug",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )
    op.add_column(
        "chunk",
        sa.Column(
            "entity_terms_text",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )
    op.execute(
        """
        UPDATE page
        SET uri_slug = trim(
            regexp_replace(
                regexp_replace(
                    regexp_replace(lower(coalesce(uri, '')), '^https?://', ''),
                    '[^[:alnum:]_]+',
                    ' ',
                    'g'
                ),
                '\\s+',
                ' ',
                'g'
            )
        )
        """
    )
    op.execute(
        """
        UPDATE chunk
        SET entity_terms_text = coalesce(array_to_string(entity_terms, ' '), '')
        """
    )
    op.execute(
        """
        CREATE INDEX ix_page_bm25 ON page
        USING bm25 (
            id,
            (title::pdb.simple('alias=title')),
            (uri_slug::pdb.ngram(3, 5, 'alias=uri_slug')),
            (content::pdb.simple('alias=body')),
            source_id,
            status_error,
            content_value
        )
        WITH (key_field = 'id')
        """
    )
    op.execute(
        """
        CREATE INDEX ix_chunk_bm25 ON chunk
        USING bm25 (
            id,
            (header_text::pdb.simple('alias=header')),
            (section_path::pdb.simple('alias=section')),
            (entity_terms_text::pdb.simple('alias=entities')),
            (text::pdb.simple('alias=body')),
            (kind::pdb.literal('alias=kind')),
            page_id,
            is_duplicate
        )
        WITH (key_field = 'id')
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunk_bm25")
    op.execute("DROP INDEX IF EXISTS ix_page_bm25")
    op.drop_column("chunk", "entity_terms_text")
    op.drop_column("page", "uri_slug")
