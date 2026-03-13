"""add ai provider columns

Revision ID: d4f1d1b7c123
Revises: b30757544984
Create Date: 2025-12-20 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "d4f1d1b7c123"
down_revision = "b30757544984"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "project",
        sa.Column(
            "provider",
            sa.String(length=64),
            server_default=sa.text("'openai'"),
        ),
    )
    op.add_column(
        "project",
        sa.Column(
            "model",
            sa.String(length=128),
            server_default=sa.text("'gpt-4o-mini'"),
        ),
    )
    op.add_column(
        "chat_msg",
        sa.Column("provider", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "chat_msg",
        sa.Column("model", sa.String(length=128), nullable=True),
    )
    op.execute("UPDATE chat_msg SET provider = 'openai' WHERE provider IS NULL")
    # set provider not nullable
    op.execute("ALTER TABLE chat_msg ALTER COLUMN provider SET NOT NULL")
    op.execute("UPDATE chat_msg SET model = 'gpt-4o-mini' WHERE model IS NULL")
    # set model not nullable
    op.execute("ALTER TABLE chat_msg ALTER COLUMN model SET NOT NULL")


def downgrade():
    op.drop_column("chat_msg", "model")
    op.drop_column("chat_msg", "provider")
    op.drop_column("project", "model")
    op.drop_column("project", "provider")
