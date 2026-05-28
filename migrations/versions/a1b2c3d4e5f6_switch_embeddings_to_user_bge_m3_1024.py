"""switch embeddings to USER-bge-m3 1024

Revision ID: a1b2c3d4e5f6
Revises: f6d8a2c4b9e1
Create Date: 2026-05-29 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "f6d8a2c4b9e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Clear existing vectors (built by Giga-Embeddings 2048-dim) and resize column.
    # Re-indexing is triggered automatically by status='added' on affected documents.
    op.execute("UPDATE chunk SET embedding = NULL WHERE embedding IS NOT NULL")
    op.execute("ALTER TABLE chunk ALTER COLUMN embedding TYPE vector(1024)")
    op.execute(
        """
        UPDATE document
        SET status = 'added'
        WHERE id IN (
            SELECT DISTINCT document_id
            FROM chunk
            WHERE document_id IS NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("UPDATE chunk SET embedding = NULL WHERE embedding IS NOT NULL")
    op.execute("ALTER TABLE chunk ALTER COLUMN embedding TYPE vector(2048)")
