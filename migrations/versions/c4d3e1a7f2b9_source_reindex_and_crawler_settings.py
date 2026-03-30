"""add source reindex period and last reindex date

Revision ID: c4d3e1a7f2b9
Revises: b19e2a4c7d31
Create Date: 2026-03-30 15:20:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "c4d3e1a7f2b9"
down_revision: Union[str, Sequence[str], None] = "b19e2a4c7d31"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_SOURCE_REINDEX_ENUM = postgresql.ENUM(
    "weekly",
    "monthly",
    "manual",
    name="sourcereindexperiod",
)


def upgrade() -> None:
    bind = op.get_bind()
    _SOURCE_REINDEX_ENUM.create(bind, checkfirst=True)

    op.add_column(
        "source",
        sa.Column(
            "reindex_period",
            sa.Enum(
                "weekly",
                "monthly",
                "manual",
                name="sourcereindexperiod",
                create_type=False,
            ),
            nullable=False,
            server_default=sa.text("'manual'"),
        ),
    )
    op.add_column(
        "source",
        sa.Column("last_reindexed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("source", "last_reindexed_at")
    op.drop_column("source", "reindex_period")

    bind = op.get_bind()
    _SOURCE_REINDEX_ENUM.drop(bind, checkfirst=True)
