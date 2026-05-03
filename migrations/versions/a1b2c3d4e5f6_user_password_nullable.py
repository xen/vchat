"""user password nullable for ldap auth

Revision ID: a1b2c3d4e5f6
Revises: f6d8a2c4b9e1
Create Date: 2026-05-03 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "f6d8a2c4b9e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("users", "password", existing_type=sa.String(), nullable=True)
    op.add_column("users", sa.Column("is_ldap", sa.Boolean(), nullable=False, server_default="false"))


def downgrade() -> None:
    op.drop_column("users", "is_ldap")
    op.execute("UPDATE users SET password = '' WHERE password IS NULL")
    op.alter_column("users", "password", existing_type=sa.String(), nullable=False)
