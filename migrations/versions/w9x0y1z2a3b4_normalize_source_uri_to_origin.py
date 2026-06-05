"""normalize source uri to origin

Revision ID: w9x0y1z2a3b4
Revises: v8w9x0y1z2a3
Create Date: 2026-06-05 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "w9x0y1z2a3b4"
down_revision: Union[str, None] = "v8w9x0y1z2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE source
        SET uri = lower(substring(uri from '^[a-zA-Z][a-zA-Z0-9+.-]*://[^/?#]+'))
        WHERE uri ~ '^[a-zA-Z][a-zA-Z0-9+.-]*://[^/?#]+'
        """
    )


def downgrade() -> None:
    pass
