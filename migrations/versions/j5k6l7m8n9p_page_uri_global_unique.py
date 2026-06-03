"""enforce globally unique page uri

Revision ID: j5k6l7m8n9p
Revises: i4j5k6l7m8n9
Create Date: 2026-06-03 18:10:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "j5k6l7m8n9p"
down_revision: Union[str, None] = "i4j5k6l7m8n9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_RANKED_PAGES_SQL = """
WITH ranked AS (
    SELECT
        id,
        uri,
        ROW_NUMBER() OVER (
            PARTITION BY uri
            ORDER BY
                CASE WHEN content IS NOT NULL AND content <> '' THEN 0 ELSE 1 END,
                CASE WHEN status = 'ready' THEN 0 WHEN status = 'parsing' THEN 1 ELSE 2 END,
                last_crawled_at DESC NULLS LAST,
                updated_at DESC NULLS LAST,
                id ASC
        ) AS rn,
        FIRST_VALUE(id) OVER (
            PARTITION BY uri
            ORDER BY
                CASE WHEN content IS NOT NULL AND content <> '' THEN 0 ELSE 1 END,
                CASE WHEN status = 'ready' THEN 0 WHEN status = 'parsing' THEN 1 ELSE 2 END,
                last_crawled_at DESC NULLS LAST,
                updated_at DESC NULLS LAST,
                id ASC
        ) AS keeper_id
    FROM page
    WHERE uri IS NOT NULL
),
duplicates AS (
    SELECT id AS loser_id, keeper_id
    FROM ranked
    WHERE rn > 1
)
"""


def _has_index(index_name: str, table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    bind = op.get_bind()

    bind.execute(
        sa.text(
            _RANKED_PAGES_SQL
            + """
            UPDATE chunk
            SET page_id = duplicates.keeper_id
            FROM duplicates
            WHERE chunk.page_id = duplicates.loser_id
            """
        )
    )

    bind.execute(
        sa.text(
            _RANKED_PAGES_SQL
            + """
            UPDATE page_link
            SET source_page_id = duplicates.keeper_id
            FROM duplicates
            WHERE page_link.source_page_id = duplicates.loser_id
            """
        )
    )

    bind.execute(
        sa.text(
            _RANKED_PAGES_SQL
            + """
            UPDATE page_link
            SET target_page_id = duplicates.keeper_id
            FROM duplicates
            WHERE page_link.target_page_id = duplicates.loser_id
            """
        )
    )

    bind.execute(
        sa.text(
            _RANKED_PAGES_SQL
            + """
            DELETE FROM page
            WHERE id IN (
                SELECT loser_id FROM duplicates
            )
            """
        )
    )

    if _has_index("uq_page_source_id_uri", "page"):
        op.drop_index("uq_page_source_id_uri", table_name="page")

    op.create_index("uq_page_uri", "page", ["uri"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_page_uri", table_name="page")
    op.create_index(
        "uq_page_source_id_uri",
        "page",
        ["source_id", "uri"],
        unique=True,
    )
