"""add_order_to_page

Revision ID: 639683b33cf1
Revises: 2f5f09c0050f
Create Date: 2025-12-14 22:32:15.086393

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "639683b33cf1"
down_revision = "2f5f09c0050f"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "support_pages",
        sa.Column("order", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade():
    op.drop_column("support_pages", "order")
