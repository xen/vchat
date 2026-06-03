"""drop source start_pages column

Revision ID: m8n9p0q1r2s
Revises: l7m8n9p0q1r
Create Date: 2026-06-03 19:40:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "m8n9p0q1r2s"
down_revision: Union[str, None] = "l7m8n9p0q1r"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("source", "start_pages")


def downgrade() -> None:
    op.add_column(
        "source",
        sa.Column(
            "start_pages",
            sa.ARRAY(sa.Text),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
