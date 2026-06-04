"""add page raw content

Revision ID: p3q4r5s6t7u8
Revises: o2p3q4r5s6t
Create Date: 2026-06-04 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "p3q4r5s6t7u8"
down_revision: Union[str, None] = "o2p3q4r5s6t"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("page", sa.Column("raw_content", sa.LargeBinary(), nullable=True))
    op.add_column("page", sa.Column("raw_content_type", sa.Text(), nullable=True))
    op.add_column("page", sa.Column("raw_content_size", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("page", "raw_content_size")
    op.drop_column("page", "raw_content_type")
    op.drop_column("page", "raw_content")
