"""merge sitemap type into site, add start_pages/sitemaps to config

Revision ID: d1e2f3a4b5c6
Revises: c2edff6c633b
Create Date: 2026-05-29 14:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, Sequence[str], None] = "c2edff6c633b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    # Migrate existing sitemap sources: move URI to config.sitemaps, change type to site
    bind.execute(
        sa.text("""
            UPDATE source
            SET
                config = jsonb_set(
                    COALESCE(config, '{}'),
                    '{sitemaps}',
                    to_jsonb(ARRAY[uri])
                ),
                type = 'site'::sourcetype
            WHERE type = 'sitemap'::sourcetype
        """)
    )

    # Remove sitemap from enum: create new enum, cast, drop old
    bind.execute(sa.text("ALTER TYPE sourcetype RENAME TO sourcetype_old"))
    new_enum = postgresql.ENUM(
        "site", "list", "s3", "google_drive", "upload",
        name="sourcetype",
    )
    new_enum.create(bind)
    bind.execute(
        sa.text(
            "ALTER TABLE source ALTER COLUMN type TYPE sourcetype "
            "USING type::text::sourcetype"
        )
    )
    bind.execute(sa.text("DROP TYPE sourcetype_old"))


def downgrade() -> None:
    bind = op.get_bind()

    bind.execute(sa.text("ALTER TYPE sourcetype RENAME TO sourcetype_old"))
    old_enum = postgresql.ENUM(
        "site", "sitemap", "list", "s3", "google_drive", "upload",
        name="sourcetype",
    )
    old_enum.create(bind)
    bind.execute(
        sa.text(
            "ALTER TABLE source ALTER COLUMN type TYPE sourcetype "
            "USING type::text::sourcetype"
        )
    )
    bind.execute(sa.text("DROP TYPE sourcetype_old"))
