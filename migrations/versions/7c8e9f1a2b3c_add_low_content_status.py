"""add_low_content_status

Revision ID: 7c8e9f1a2b3c
Revises: 3824f23ac6a5
Create Date: 2026-05-31 17:35:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "7c8e9f1a2b3c"
down_revision = "3824f23ac6a5"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text("ALTER TYPE status ADD VALUE IF NOT EXISTS 'low_content'"))


def downgrade():
    # PostgreSQL ENUM value removal is intentionally not attempted here.
    pass
