"""replace source shingles with page shingles

Revision ID: r5s6t7u8v9w0
Revises: q4r5s6t7u8v9
Create Date: 2026-06-04 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "r5s6t7u8v9w0"
down_revision: Union[str, None] = "q4r5s6t7u8v9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "page_shingle",
        sa.Column("page_id", sa.Integer(), nullable=False),
        sa.Column("shingle_hash", sa.BigInteger(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["page_id"], ["page.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["source.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("page_id", "shingle_hash"),
    )
    op.create_index("ix_page_shingle_source_id", "page_shingle", ["source_id"])
    op.create_index(
        "ix_page_shingle_source_hash",
        "page_shingle",
        ["source_id", "shingle_hash"],
    )
    op.drop_index("ix_source_shingle_freq_source_id", table_name="source_shingle_freq")
    op.drop_table("source_shingle_freq")


def downgrade() -> None:
    op.create_table(
        "source_shingle_freq",
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("shingle_hash", sa.BigInteger(), nullable=False),
        sa.Column("count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["source.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("source_id", "shingle_hash"),
    )
    op.create_index(
        "ix_source_shingle_freq_source_id",
        "source_shingle_freq",
        ["source_id"],
    )
    op.drop_index("ix_page_shingle_source_hash", table_name="page_shingle")
    op.drop_index("ix_page_shingle_source_id", table_name="page_shingle")
    op.drop_table("page_shingle")
