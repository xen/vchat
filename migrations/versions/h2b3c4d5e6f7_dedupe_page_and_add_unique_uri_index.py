"""dedupe page rows and add unique index on source_id + uri

Revision ID: h2b3c4d5e6f7
Revises: g1a2b3c4d5e6
Create Date: 2026-06-01
"""

from alembic import op
import sqlalchemy as sa


revision = "h2b3c4d5e6f7"
down_revision = "g1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()

    bind.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT
                    id,
                    source_id,
                    uri,
                    ROW_NUMBER() OVER (
                        PARTITION BY source_id, uri
                        ORDER BY
                            CASE WHEN content IS NOT NULL AND content <> '' THEN 0 ELSE 1 END,
                            CASE WHEN status = 'ready' THEN 0 WHEN status = 'parsing' THEN 1 ELSE 2 END,
                            last_crawled_at DESC NULLS LAST,
                            updated_at DESC NULLS LAST,
                            id ASC
                    ) AS rn,
                    FIRST_VALUE(id) OVER (
                        PARTITION BY source_id, uri
                        ORDER BY
                            CASE WHEN content IS NOT NULL AND content <> '' THEN 0 ELSE 1 END,
                            CASE WHEN status = 'ready' THEN 0 WHEN status = 'parsing' THEN 1 ELSE 2 END,
                            last_crawled_at DESC NULLS LAST,
                            updated_at DESC NULLS LAST,
                            id ASC
                    ) AS keeper_id
                FROM page
                WHERE source_id IS NOT NULL
                  AND uri IS NOT NULL
            ),
            duplicates AS (
                SELECT id AS loser_id, keeper_id
                FROM ranked
                WHERE rn > 1
            )
            UPDATE chunk
            SET page_id = duplicates.keeper_id
            FROM duplicates
            WHERE chunk.page_id = duplicates.loser_id
            """
        )
    )

    bind.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT
                    id,
                    source_id,
                    uri,
                    ROW_NUMBER() OVER (
                        PARTITION BY source_id, uri
                        ORDER BY
                            CASE WHEN content IS NOT NULL AND content <> '' THEN 0 ELSE 1 END,
                            CASE WHEN status = 'ready' THEN 0 WHEN status = 'parsing' THEN 1 ELSE 2 END,
                            last_crawled_at DESC NULLS LAST,
                            updated_at DESC NULLS LAST,
                            id ASC
                    ) AS rn,
                    FIRST_VALUE(id) OVER (
                        PARTITION BY source_id, uri
                        ORDER BY
                            CASE WHEN content IS NOT NULL AND content <> '' THEN 0 ELSE 1 END,
                            CASE WHEN status = 'ready' THEN 0 WHEN status = 'parsing' THEN 1 ELSE 2 END,
                            last_crawled_at DESC NULLS LAST,
                            updated_at DESC NULLS LAST,
                            id ASC
                    ) AS keeper_id
                FROM page
                WHERE source_id IS NOT NULL
                  AND uri IS NOT NULL
            ),
            duplicates AS (
                SELECT id AS loser_id, keeper_id
                FROM ranked
                WHERE rn > 1
            )
            UPDATE page_link
            SET source_page_id = duplicates.keeper_id
            FROM duplicates
            WHERE page_link.source_page_id = duplicates.loser_id
            """
        )
    )

    bind.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT
                    id,
                    source_id,
                    uri,
                    ROW_NUMBER() OVER (
                        PARTITION BY source_id, uri
                        ORDER BY
                            CASE WHEN content IS NOT NULL AND content <> '' THEN 0 ELSE 1 END,
                            CASE WHEN status = 'ready' THEN 0 WHEN status = 'parsing' THEN 1 ELSE 2 END,
                            last_crawled_at DESC NULLS LAST,
                            updated_at DESC NULLS LAST,
                            id ASC
                    ) AS rn,
                    FIRST_VALUE(id) OVER (
                        PARTITION BY source_id, uri
                        ORDER BY
                            CASE WHEN content IS NOT NULL AND content <> '' THEN 0 ELSE 1 END,
                            CASE WHEN status = 'ready' THEN 0 WHEN status = 'parsing' THEN 1 ELSE 2 END,
                            last_crawled_at DESC NULLS LAST,
                            updated_at DESC NULLS LAST,
                            id ASC
                    ) AS keeper_id
                FROM page
                WHERE source_id IS NOT NULL
                  AND uri IS NOT NULL
            ),
            duplicates AS (
                SELECT id AS loser_id, keeper_id
                FROM ranked
                WHERE rn > 1
            )
            UPDATE page_link
            SET target_page_id = duplicates.keeper_id
            FROM duplicates
            WHERE page_link.target_page_id = duplicates.loser_id
            """
        )
    )

    bind.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY source_id, uri
                        ORDER BY
                            CASE WHEN content IS NOT NULL AND content <> '' THEN 0 ELSE 1 END,
                            CASE WHEN status = 'ready' THEN 0 WHEN status = 'parsing' THEN 1 ELSE 2 END,
                            last_crawled_at DESC NULLS LAST,
                            updated_at DESC NULLS LAST,
                            id ASC
                    ) AS rn
                FROM page
                WHERE source_id IS NOT NULL
                  AND uri IS NOT NULL
            )
            DELETE FROM page
            WHERE id IN (
                SELECT id FROM ranked WHERE rn > 1
            )
            """
        )
    )

    op.create_index(
        "uq_page_source_id_uri",
        "page",
        ["source_id", "uri"],
        unique=True,
    )


def downgrade():
    op.drop_index("uq_page_source_id_uri", table_name="page")
