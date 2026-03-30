"""chat_msg vote as nullable boolean and drop vote_comment

Revision ID: b19e2a4c7d31
Revises: a7c1d9e4b2f0
Create Date: 2026-03-30 16:10:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b19e2a4c7d31"
down_revision: Union[str, Sequence[str], None] = "a7c1d9e4b2f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _get_vote_type(bind) -> sa.types.TypeEngine | None:
    inspector = sa.inspect(bind)
    for column in inspector.get_columns("chat_msg"):
        if column.get("name") == "vote":
            return column.get("type")
    return None


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("ALTER TABLE chat_msg DROP COLUMN IF EXISTS vote_comment")
    op.execute("ALTER TABLE chat_msg ALTER COLUMN vote DROP DEFAULT")
    op.execute("ALTER TABLE chat_msg ALTER COLUMN vote DROP NOT NULL")

    vote_type = _get_vote_type(bind)
    if not isinstance(vote_type, sa.Boolean):
        op.execute(
            """
            ALTER TABLE chat_msg
            ALTER COLUMN vote TYPE BOOLEAN
            USING (
                CASE
                    WHEN vote = 1 THEN TRUE
                    WHEN vote = -1 THEN FALSE
                    ELSE NULL
                END
            )
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    vote_type = _get_vote_type(bind)
    if not isinstance(vote_type, sa.Integer):
        op.execute(
            """
            ALTER TABLE chat_msg
            ALTER COLUMN vote TYPE INTEGER
            USING (
                CASE
                    WHEN vote IS TRUE THEN 1
                    WHEN vote IS FALSE THEN -1
                    ELSE 0
                END
            )
            """
        )
    op.execute("ALTER TABLE chat_msg ALTER COLUMN vote SET NOT NULL")
    op.execute("ALTER TABLE chat_msg ALTER COLUMN vote SET DEFAULT 0")
    op.add_column("chat_msg", sa.Column("vote_comment", sa.Text(), nullable=True))
