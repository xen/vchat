"""add start_pages and sitemaps columns to source

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-05-29 14:30:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e2f3a4b5c6d7"
down_revision: Union[str, Sequence[str], None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "source",
        sa.Column(
            "start_pages",
            sa.ARRAY(sa.Text),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.add_column(
        "source",
        sa.Column(
            "sitemaps",
            sa.ARRAY(sa.Text),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )

    # Migrate sitemaps stored in config.sitemaps (from the previous migration)
    bind = op.get_bind()
    bind.execute(
        sa.text("""
            UPDATE source
            SET
                sitemaps = ARRAY(
                    SELECT jsonb_array_elements_text(config->'sitemaps')
                ),
                config = config - 'sitemaps'
            WHERE config ? 'sitemaps'
        """)
    )


def downgrade() -> None:
    op.drop_column("source", "sitemaps")
    op.drop_column("source", "start_pages")
