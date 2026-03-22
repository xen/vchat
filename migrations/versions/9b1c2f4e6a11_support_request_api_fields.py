"""support request api fields

Revision ID: 9b1c2f4e6a11
Revises: 837f34aaef4c
Create Date: 2026-03-22 18:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "9b1c2f4e6a11"
down_revision: Union[str, Sequence[str], None] = "837f34aaef4c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("request", "subject", new_column_name="name")
    op.alter_column("request", "text", new_column_name="body")
    op.add_column("request", sa.Column("ip_address", sa.String(length=64), nullable=True))
    op.add_column("request", sa.Column("user_agent", sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column("request", "user_agent")
    op.drop_column("request", "ip_address")
    op.alter_column("request", "body", new_column_name="text")
    op.alter_column("request", "name", new_column_name="subject")
