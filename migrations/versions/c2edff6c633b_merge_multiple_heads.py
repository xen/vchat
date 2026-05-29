"""merge multiple heads

Revision ID: c2edff6c633b
Revises: a1b2c3d4e5f6, b8e4d2a1c9f0
Create Date: 2026-05-29 13:41:15.895490

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c2edff6c633b'
down_revision = ('a1b2c3d4e5f6', 'b8e4d2a1c9f0')
branch_labels = None
depends_on = None

def upgrade():
    pass


def downgrade():
    pass
