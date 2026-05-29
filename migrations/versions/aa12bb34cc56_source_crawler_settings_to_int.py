"""cast source crawler settings to int

Revision ID: aa12bb34cc56
Revises: f7a8b9c0d1e2
Create Date: 2026-05-29 18:20:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "aa12bb34cc56"
down_revision: Union[str, Sequence[str], None] = "f7a8b9c0d1e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE source
            SET config =
                CASE
                    WHEN config ? 'crawler_concurrent_requests' THEN
                        jsonb_set(
                            config,
                            '{crawler_concurrent_requests}',
                            to_jsonb(((config->>'crawler_concurrent_requests')::numeric)::int),
                            true
                        )
                    ELSE config
                END
            WHERE config ? 'crawler_concurrent_requests'
            """
        )
    )
    bind.execute(
        sa.text(
            """
            UPDATE source
            SET config =
                CASE
                    WHEN config ? 'crawler_download_delay' THEN
                        jsonb_set(
                            config,
                            '{crawler_download_delay}',
                            to_jsonb(((config->>'crawler_download_delay')::numeric)::int),
                            true
                        )
                    ELSE config
                END
            WHERE config ? 'crawler_download_delay'
            """
        )
    )
    bind.execute(
        sa.text(
            """
            UPDATE source
            SET config =
                CASE
                    WHEN config ? 'crawler_download_timeout' THEN
                        jsonb_set(
                            config,
                            '{crawler_download_timeout}',
                            to_jsonb(((config->>'crawler_download_timeout')::numeric)::int),
                            true
                        )
                    ELSE config
                END
            WHERE config ? 'crawler_download_timeout'
            """
        )
    )


def downgrade() -> None:
    pass
