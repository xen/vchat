"""normalize source uri to origin after discovery fields

Revision ID: y1z2a3b4c5d6
Revises: x0y1z2a3b4c5
Create Date: 2026-06-05 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "y1z2a3b4c5d6"
down_revision: Union[str, None] = "x0y1z2a3b4c5"
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
