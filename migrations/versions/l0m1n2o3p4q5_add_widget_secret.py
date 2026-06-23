"""add widget secret

Revision ID: l0m1n2o3p4q5
Revises: k0l1m2n3o4p5
Create Date: 2026-06-21 00:00:00.000000
"""

import secrets

import sqlalchemy as sa
from alembic import op


revision = "l0m1n2o3p4q5"
down_revision = "k0l1m2n3o4p5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "widget_integration",
        sa.Column("secret", sa.String(length=128), nullable=True),
    )
    bind = op.get_bind()
    widget_ids = bind.execute(sa.text("SELECT id FROM widget_integration")).scalars().all()
    for widget_id in widget_ids:
        bind.execute(
            sa.text("UPDATE widget_integration SET secret = :secret WHERE id = :id"),
            {"id": widget_id, "secret": secrets.token_urlsafe(32)},
        )
    op.alter_column("widget_integration", "secret", nullable=False)


def downgrade() -> None:
    op.drop_column("widget_integration", "secret")
