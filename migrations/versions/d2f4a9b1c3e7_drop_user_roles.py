"""drop user role column and enum

Revision ID: d2f4a9b1c3e7
Revises: c4d3e1a7f2b9
Create Date: 2026-03-30 22:10:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "d2f4a9b1c3e7"
down_revision: Union[str, Sequence[str], None] = "c4d3e1a7f2b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_USER_ROLE_ENUM = postgresql.ENUM("admin", "user", "guest", name="userrole")


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def upgrade() -> None:
    if _has_column("users", "role"):
        op.drop_column("users", "role")

    bind = op.get_bind()
    _USER_ROLE_ENUM.drop(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    _USER_ROLE_ENUM.create(bind, checkfirst=True)

    if not _has_column("users", "role"):
        op.add_column(
            "users",
            sa.Column(
                "role",
                sa.Enum("admin", "user", "guest", name="userrole", create_type=False),
                nullable=False,
                server_default=sa.text("'admin'"),
            ),
        )
