"""Drop obsolete settings table.

Revision ID: p9q8r7s6t5u4
Revises: n0o1p2q3r4s5
Create Date: 2026-07-04 21:40:00.000000
"""

from alembic import op


revision = "p9q8r7s6t5u4"
down_revision = "n0o1p2q3r4s5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS settings")


def downgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key character varying(255) PRIMARY KEY,
            value text
        )
        """
    )
