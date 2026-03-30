"""drop chat type column and enum

Revision ID: e7c1ab24d9f0
Revises: d2f4a9b1c3e7
Create Date: 2026-03-31 10:20:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "e7c1ab24d9f0"
down_revision: Union[str, Sequence[str], None] = "d2f4a9b1c3e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CHAT_TYPE_ENUM = postgresql.ENUM("chat", "call", name="chattype")


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def upgrade() -> None:
    if _has_column("chat", "type"):
        op.drop_column("chat", "type")

    bind = op.get_bind()
    _CHAT_TYPE_ENUM.drop(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    _CHAT_TYPE_ENUM.create(bind, checkfirst=True)

    if not _has_column("chat", "type"):
        op.add_column(
            "chat",
            sa.Column(
                "type",
                sa.Enum("chat", "call", name="chattype", create_type=False),
                nullable=False,
                server_default=sa.text("'chat'"),
            ),
        )
