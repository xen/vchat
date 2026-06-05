"""add page discovery fields

Revision ID: t6u7v8w9x0y1
Revises: s6t7u8v9w0x1
Create Date: 2026-06-05 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "t6u7v8w9x0y1"
down_revision: Union[str, None] = "s6t7u8v9w0x1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "page",
        sa.Column("discover_by", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "page",
        sa.Column("discover_source", sa.Text(), nullable=True),
    )
    op.create_index("ix_page_discover_by", "page", ["discover_by"])


def downgrade() -> None:
    op.drop_index("ix_page_discover_by", table_name="page")
    op.drop_column("page", "discover_source")
    op.drop_column("page", "discover_by")
