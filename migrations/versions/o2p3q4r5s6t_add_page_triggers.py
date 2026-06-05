"""add page triggers

Revision ID: o2p3q4r5s6t
Revises: n1o2p3q4r5s
Create Date: 2026-06-04 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "o2p3q4r5s6t"
down_revision: Union[str, None] = "n1o2p3q4r5s"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "source",
        sa.Column(
            "enable_triggers",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.execute(
        """
        UPDATE source
        SET enable_triggers = COALESCE((config->>'allow_custom_triggers')::boolean, false)
        WHERE config ? 'allow_custom_triggers'
        """
    )
    op.execute("UPDATE source SET config = config - 'allow_custom_triggers'")

    op.add_column(
        "page",
        sa.Column(
            "has_triggers",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "page",
        sa.Column("triggers", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_index("ix_page_has_triggers", "page", ["has_triggers"])

    op.create_table(
        "trigger_response_cache",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("page_id", sa.Integer(), nullable=False),
        sa.Column("trigger_key", sa.String(length=64), nullable=False),
        sa.Column("prompt_hash", sa.String(length=64), nullable=False),
        sa.Column("response_text", sa.Text(), nullable=False),
        sa.Column("full_context", sa.Text(), nullable=False),
        sa.Column(
            "used_chunks", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("provider", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["page_id"], ["page.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "page_id",
            "trigger_key",
            "prompt_hash",
            name="uq_trigger_response_cache_page_trigger_prompt",
        ),
    )
    op.create_index(
        "ix_trigger_response_cache_page_id",
        "trigger_response_cache",
        ["page_id"],
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE source
        SET config = jsonb_set(
            config,
            '{allow_custom_triggers}',
            to_jsonb(enable_triggers),
            true
        )
        """
    )
    op.drop_index(
        "ix_trigger_response_cache_page_id",
        table_name="trigger_response_cache",
    )
    op.drop_table("trigger_response_cache")
    op.drop_index("ix_page_has_triggers", table_name="page")
    op.drop_column("page", "triggers")
    op.drop_column("page", "has_triggers")
    op.drop_column("source", "enable_triggers")
