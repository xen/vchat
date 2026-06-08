"""add page link page id indexes

Revision ID: bb2c3d4e5f6a
Revises: aa1b2c3d4e5f
Create Date: 2026-06-08 17:15:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "bb2c3d4e5f6a"
down_revision: Union[str, None] = "aa1b2c3d4e5f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_page_link_source_page_id",
        "page_link",
        ["source_page_id"],
        unique=False,
    )
    op.create_index(
        "ix_page_link_target_page_id",
        "page_link",
        ["target_page_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_page_link_target_page_id", table_name="page_link")
    op.drop_index("ix_page_link_source_page_id", table_name="page_link")
