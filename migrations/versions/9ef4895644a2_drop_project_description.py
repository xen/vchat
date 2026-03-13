"""drop project description

Revision ID: 9ef4895644a2
Revises: merge_c3e2426f1c9b
Create Date: 2024-10-12 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "9ef4895644a2"
down_revision = "merge_c3e2426f1c9b"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_column("project", "description")


def downgrade():
    op.add_column(
        "project",
        sa.Column("description", sa.Text(), nullable=True),
    )
