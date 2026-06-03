"""add page_link source fk with delete cascade

Revision ID: l7m8n9p0q1r
Revises: k6l7m8n9p0q
Create Date: 2026-06-03 19:10:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "l7m8n9p0q1r"
down_revision: Union[str, None] = "k6l7m8n9p0q"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE page_link
            SET source_id = NULL
            WHERE source_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM source
                  WHERE source.id = page_link.source_id
              )
            """
        )
    )
    op.create_foreign_key(
        "page_link_source_id_fkey",
        "page_link",
        "source",
        ["source_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        "page_link_source_id_fkey",
        "page_link",
        type_="foreignkey",
    )
