"""add source_shingle_freq table for boilerplate detection

Revision ID: d3e4f5a6b7c8
Revises: e6d18bb42bf3
Create Date: 2026-05-31 10:30:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "d3e4f5a6b7c8"
down_revision: Union[str, Sequence[str], None] = "e6d18bb42bf3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "source_shingle_freq",
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("shingle_hash", sa.BigInteger(), nullable=False),
        sa.Column(
            "count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["source.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("source_id", "shingle_hash"),
    )
    op.create_index(
        "ix_source_shingle_freq_source_id",
        "source_shingle_freq",
        ["source_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_source_shingle_freq_source_id", table_name="source_shingle_freq")
    op.drop_table("source_shingle_freq")
