"""drop page link target status

Revision ID: s6t7u8v9w0x1
Revises: r5s6t7u8v9w0
Create Date: 2026-06-04 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "s6t7u8v9w0x1"
down_revision: Union[str, None] = "r5s6t7u8v9w0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("page_link", "target_status")


def downgrade() -> None:
    op.add_column(
        "page_link",
        sa.Column("target_status", sa.String(length=32), nullable=True),
    )
