"""detach uploaded files from sources

Revision ID: f3b5c8d1e2a4
Revises: e7c1ab24d9f0
Create Date: 2026-03-31 16:10:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f3b5c8d1e2a4"
down_revision: Union[str, Sequence[str], None] = "e7c1ab24d9f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("document", "source_id", existing_type=sa.Integer(), nullable=True)

    op.execute(
        """
        UPDATE document
        SET source_id = NULL
        WHERE source_id IN (
            SELECT id FROM source WHERE type = 'upload'
        )
        """
    )

    op.execute("DELETE FROM source WHERE type = 'upload'")


def downgrade() -> None:
    op.execute(
        """
        INSERT INTO source (type, title, uri, config, reindex_period, created_at, updated_at)
        VALUES ('upload', 'Uploaded Files', 'uploads://', '{}'::jsonb, 'manual', NOW(), NOW())
        RETURNING id
        """
    )

    op.execute(
        """
        UPDATE document
        SET source_id = (
            SELECT id FROM source
            WHERE type = 'upload'
            ORDER BY id ASC
            LIMIT 1
        )
        WHERE source_id IS NULL
        """
    )

    op.alter_column("document", "source_id", existing_type=sa.Integer(), nullable=False)
