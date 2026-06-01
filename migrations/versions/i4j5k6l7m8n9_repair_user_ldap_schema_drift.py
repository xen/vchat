"""repair user ldap schema drift

Revision ID: i4j5k6l7m8n9
Revises: h2b3c4d5e6f7
Create Date: 2026-06-02 00:25:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "i4j5k6l7m8n9"
down_revision: Union[str, None] = "h2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _get_column(table_name: str, column_name: str) -> dict | None:
    inspector = sa.inspect(op.get_bind())
    for column in inspector.get_columns(table_name):
        if column["name"] == column_name:
            return column
    return None


def upgrade() -> None:
    if _get_column("users", "is_ldap") is None:
        op.add_column(
            "users",
            sa.Column("is_ldap", sa.Boolean(), nullable=False, server_default="false"),
        )
        op.alter_column("users", "is_ldap", server_default=None)

    password_column = _get_column("users", "password")
    if password_column is not None and password_column.get("nullable") is False:
        op.alter_column("users", "password", existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    password_column = _get_column("users", "password")
    if password_column is not None and password_column.get("nullable") is True:
        op.execute("UPDATE users SET password = '' WHERE password IS NULL")
        op.alter_column("users", "password", existing_type=sa.String(), nullable=False)

    if _get_column("users", "is_ldap") is not None:
        op.drop_column("users", "is_ldap")
