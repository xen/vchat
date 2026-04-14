"""switch embeddings to giga 2048

Revision ID: f6d8a2c4b9e1
Revises: c91d4e7a1b2c
Create Date: 2026-04-01 11:20:00.000000
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6d8a2c4b9e1"
down_revision: Union[str, None] = "c91d4e7a1b2c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing vectors were built by a different model and dimension.
    # Clear them and re-run background embedding for a clean migration.
    op.execute("UPDATE chunk SET embedding = NULL WHERE embedding IS NOT NULL")
    op.execute("ALTER TABLE chunk ALTER COLUMN embedding TYPE vector(2048)")
    op.execute(
        """
        UPDATE document
        SET status = 'added'
        WHERE id IN (
            SELECT DISTINCT document_id
            FROM chunk
            WHERE document_id IS NOT NULL
              AND embedding IS NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("UPDATE chunk SET embedding = NULL WHERE embedding IS NOT NULL")
    op.execute("ALTER TABLE chunk ALTER COLUMN embedding TYPE vector(1024)")
