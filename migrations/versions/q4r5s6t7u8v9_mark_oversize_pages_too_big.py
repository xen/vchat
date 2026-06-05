"""mark oversize pages as too_big

Revision ID: q4r5s6t7u8v9
Revises: p3q4r5s6t7u8
Create Date: 2026-06-04 23:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "q4r5s6t7u8v9"
down_revision: Union[str, None] = "p3q4r5s6t7u8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DOCUMENT_MAX_CHARS = 100000


def upgrade() -> None:
    op.execute(
        f"""
        WITH oversized AS (
            SELECT id, length(content) AS content_chars
            FROM page
            WHERE content IS NOT NULL
              AND length(content) > {DOCUMENT_MAX_CHARS}
        ),
        marked AS (
            UPDATE page
            SET status = 'ready',
                status_error = 'too_big',
                meta = jsonb_set(
                    jsonb_set(
                        jsonb_set(
                            jsonb_set(
                                coalesce(meta, '{{}}'::jsonb)
                                    - 'error'
                                    - 'exception_class',
                                '{{reason}}',
                                to_jsonb('too_big'::text),
                                true
                            ),
                            '{{message}}',
                            to_jsonb(
                                (
                                    'Document content is too large to index ('
                                    || oversized.content_chars::text
                                    || ' chars > {DOCUMENT_MAX_CHARS}).'
                                )::text
                            ),
                            true
                        ),
                        '{{content_chars}}',
                        to_jsonb(oversized.content_chars),
                        true
                    ),
                    '{{max_content_chars}}',
                    to_jsonb({DOCUMENT_MAX_CHARS}),
                    true
                )
            FROM oversized
            WHERE page.id = oversized.id
            RETURNING page.id
        )
        DELETE FROM chunk
        USING marked
        WHERE chunk.page_id = marked.id
        """
    )


def downgrade() -> None:
    raise NotImplementedError("Downgrade not supported for oversize page backfill")
