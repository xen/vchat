"""drop chat record column

Revision ID: c91d4e7a1b2c
Revises: ab72c19f4e10
Create Date: 2026-03-31 22:20:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c91d4e7a1b2c"
down_revision: Union[str, Sequence[str], None] = "ab72c19f4e10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def upgrade() -> None:
    if _has_column("chat", "record"):
        op.drop_column("chat", "record")


def downgrade() -> None:
    if not _has_column("chat", "record"):
        op.add_column(
            "chat",
            sa.Column(
                "record",
                sa.Text(),
                nullable=False,
                server_default=sa.text("''"),
            ),
        )

