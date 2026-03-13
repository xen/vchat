"""add_project_id_to_chat_msg

Revision ID: fa8d3a1c2b7f
Revises: a5420397d268
Create Date: 2026-02-06 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "fa8d3a1c2b7f"
down_revision = "a5420397d268"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "chat_msg",
        sa.Column("project_id", sa.Integer(), nullable=True),
    )

    op.execute(
        """
        UPDATE chat_msg
        SET project_id = ch.project_id
        FROM chat ch
        WHERE chat_msg.chat_id = ch.id
        """
    )

    op.create_index("ix_chat_msg_project_id", "chat_msg", ["project_id"], unique=False)

    op.create_foreign_key(
        "fk_chat_msg_project_id_project",
        "chat_msg",
        "project",
        ["project_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade():
    op.drop_constraint("fk_chat_msg_project_id_project", "chat_msg", type_="foreignkey")
    op.drop_index("ix_chat_msg_project_id", table_name="chat_msg")
    op.drop_column("chat_msg", "project_id")
