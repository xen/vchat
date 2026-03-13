"""add_vote_fields_to_chatmsg

Revision ID: c70e9c58c5b7
Revises: 9ef4895644a2
Create Date: 2025-12-06 20:59:09.133641

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c70e9c58c5b7"
down_revision = "9ef4895644a2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "chat_msg",
        sa.Column("vote", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("chat_msg", sa.Column("vote_comment", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("chat_msg", "vote_comment")
    op.drop_column("chat_msg", "vote")
