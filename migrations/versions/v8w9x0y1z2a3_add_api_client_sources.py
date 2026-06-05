"""add api client source restrictions

Revision ID: v8w9x0y1z2a3
Revises: u7v8w9x0y1z2
Create Date: 2026-06-05 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "v8w9x0y1z2a3"
down_revision: Union[str, None] = "u7v8w9x0y1z2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "api_client_source",
        sa.Column("api_client_id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["api_client_id"],
            ["api_client.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["source.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("api_client_id", "source_id"),
    )
    op.create_index(
        op.f("ix_api_client_source_source_id"),
        "api_client_source",
        ["source_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_api_client_source_source_id"),
        table_name="api_client_source",
    )
    op.drop_table("api_client_source")
