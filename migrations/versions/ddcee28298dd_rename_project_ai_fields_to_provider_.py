"""rename project ai fields to provider and model

Revision ID: ddcee28298dd
Revises: d55808f71ebf
Create Date: 2025-12-22 01:38:29.610080

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "ddcee28298dd"
down_revision = "d55808f71ebf"
branch_labels = None
depends_on = None


def _get_project_columns():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # Cache the columns for repeated checks during this migration run
    return {column["name"] for column in inspector.get_columns("project")}


def upgrade():
    columns = _get_project_columns()
    if "ai_provider" in columns:
        op.alter_column("project", "ai_provider", new_column_name="provider")
    if "ai_model" in columns:
        op.alter_column("project", "ai_model", new_column_name="model")


def downgrade():
    columns = _get_project_columns()
    if "provider" in columns:
        op.alter_column("project", "provider", new_column_name="ai_provider")
    if "model" in columns:
        op.alter_column("project", "model", new_column_name="ai_model")
