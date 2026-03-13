"""project crawl limit

Revision ID: d3f0c4f2e825
Revises: ee439cbc3357
Create Date: 2025-11-23 00:45:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "d3f0c4f2e825"
down_revision = "4a9a9ebdbb14"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "project",
        sa.Column(
            "crawl_page_limit",
            sa.Integer(),
            nullable=False,
            server_default="100",
        ),
    )
    op.alter_column("project", "crawl_page_limit", server_default=None)


def downgrade():
    op.drop_column("project", "crawl_page_limit")
