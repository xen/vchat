"""Add index_status field to page for tracking chunking/embedding progress

Revision ID: c5d6e7f8a9b0
Revises: a2b3c4d5e6f7
Create Date: 2026-05-31

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c5d6e7f8a9b0"
down_revision: Union[str, Sequence[str], None] = "a2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "page",
        sa.Column("index_status", sa.String(32), nullable=True),
    )
    op.create_index("ix_page_index_status", "page", ["index_status"])

    # Backfill: pages with status='indexed' were fully embedded
    op.get_bind().execute(
        sa.text("UPDATE page SET index_status = 'indexed' WHERE status = 'indexed'")
    )
    # Pages with status='added' were waiting for embedding
    op.get_bind().execute(
        sa.text("UPDATE page SET index_status = 'queued' WHERE status = 'added'")
    )


def downgrade() -> None:
    op.drop_index("ix_page_index_status", table_name="page")
    op.drop_column("page", "index_status")
