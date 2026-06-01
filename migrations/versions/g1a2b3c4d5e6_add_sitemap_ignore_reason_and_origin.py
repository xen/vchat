"""add sitemap ignore reason and origin

Revision ID: g1a2b3c4d5e6
Revises: f7a8b9c0d1e2
Create Date: 2026-06-01
"""

from alembic import op
import sqlalchemy as sa


revision = "g1a2b3c4d5e6"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sitemap", sa.Column("discovered_from_url", sa.Text(), nullable=True))
    op.add_column("sitemap", sa.Column("ignore_reason", sa.String(length=64), nullable=True))


def downgrade():
    op.drop_column("sitemap", "ignore_reason")
    op.drop_column("sitemap", "discovered_from_url")
