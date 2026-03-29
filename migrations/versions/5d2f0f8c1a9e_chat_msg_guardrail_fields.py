"""add guardrail analytics fields to chat_msg

Revision ID: 5d2f0f8c1a9e
Revises: 3f91f6d2b0aa
Create Date: 2026-03-30 10:20:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "5d2f0f8c1a9e"
down_revision: Union[str, Sequence[str], None] = "3f91f6d2b0aa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(inspector: sa.Inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _column_exists(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    if not _table_exists(inspector, table_name):
        return False
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def _index_exists(inspector: sa.Inspector, table_name: str, index_name: str) -> bool:
    if not _table_exists(inspector, table_name):
        return False
    return any(index.get("name") == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "chat_msg"):
        return

    if not _column_exists(inspector, "chat_msg", "guardrail_triggered"):
        op.add_column(
            "chat_msg",
            sa.Column(
                "guardrail_triggered",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )
        inspector = sa.inspect(bind)

    if not _column_exists(inspector, "chat_msg", "guardrail_stage"):
        op.add_column(
            "chat_msg",
            sa.Column("guardrail_stage", sa.String(length=16), nullable=True),
        )
        inspector = sa.inspect(bind)

    if not _column_exists(inspector, "chat_msg", "guardrail_reasons"):
        op.add_column(
            "chat_msg",
            sa.Column("guardrail_reasons", sa.ARRAY(sa.String()), nullable=True),
        )
        inspector = sa.inspect(bind)

    index_name = op.f("ix_chat_msg_guardrail_triggered")
    if not _index_exists(inspector, "chat_msg", index_name):
        op.create_index(
            index_name,
            "chat_msg",
            ["guardrail_triggered"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _table_exists(inspector, "chat_msg"):
        return

    index_name = op.f("ix_chat_msg_guardrail_triggered")
    if _index_exists(inspector, "chat_msg", index_name):
        op.drop_index(index_name, table_name="chat_msg")
        inspector = sa.inspect(bind)

    if _column_exists(inspector, "chat_msg", "guardrail_reasons"):
        op.drop_column("chat_msg", "guardrail_reasons")
        inspector = sa.inspect(bind)

    if _column_exists(inspector, "chat_msg", "guardrail_stage"):
        op.drop_column("chat_msg", "guardrail_stage")
        inspector = sa.inspect(bind)

    if _column_exists(inspector, "chat_msg", "guardrail_triggered"):
        op.drop_column("chat_msg", "guardrail_triggered")
