"""Add meta to project

Revision ID: d55808f71ebf
Revises: e8c3e6da7eee
Create Date: 2025-12-22 00:14:38.642132

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "d55808f71ebf"
down_revision = "e8c3e6da7eee"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "project",
        sa.Column(
            "meta",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade():
    op.drop_column("project", "meta")
