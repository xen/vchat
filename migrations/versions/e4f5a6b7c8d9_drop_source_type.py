"""drop source.type column

Revision ID: e4f5a6b7c8d9
Revises: d1e2f3a4b5c6
Create Date: 2026-05-29 15:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "e4f5a6b7c8d9"
down_revision: Union[str, Sequence[str], None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    # Delete sources that are not site type (s3, google_drive, list, upload)
    bind.execute(
        sa.text("DELETE FROM source WHERE type != 'site'::sourcetype")
    )

    # Drop the type column
    op.drop_column("source", "type")

    # Drop the sourcetype enum
    bind.execute(sa.text("DROP TYPE IF EXISTS sourcetype"))


def downgrade() -> None:
    bind = op.get_bind()

    # Recreate the enum
    source_type_enum = postgresql.ENUM(
        "site", "list", "s3", "google_drive", "upload",
        name="sourcetype",
    )
    source_type_enum.create(bind)

    # Re-add the column with default value
    op.add_column(
        "source",
        sa.Column(
            "type",
            postgresql.ENUM(
                "site", "list", "s3", "google_drive", "upload",
                name="sourcetype",
                create_type=False,
            ),
            nullable=False,
            server_default="site",
        ),
    )

    # Remove the server default after backfill
    op.alter_column("source", "type", server_default=None)
