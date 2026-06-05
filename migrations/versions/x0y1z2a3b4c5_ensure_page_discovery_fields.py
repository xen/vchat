"""ensure page discovery fields

Revision ID: x0y1z2a3b4c5
Revises: w9x0y1z2a3b4
Create Date: 2026-06-05 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "x0y1z2a3b4c5"
down_revision: Union[str, None] = "w9x0y1z2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE page ADD COLUMN IF NOT EXISTS discover_by VARCHAR(32)")
    op.execute("ALTER TABLE page ADD COLUMN IF NOT EXISTS discover_source TEXT")
    op.execute("CREATE INDEX IF NOT EXISTS ix_page_discover_by ON page (discover_by)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_page_discover_by")
    op.execute("ALTER TABLE page DROP COLUMN IF EXISTS discover_source")
    op.execute("ALTER TABLE page DROP COLUMN IF EXISTS discover_by")
