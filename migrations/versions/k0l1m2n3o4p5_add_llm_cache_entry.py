"""add llm cache entry

Revision ID: k0l1m2n3o4p5
Revises: j0k1l2m3n4o5
Create Date: 2026-06-17 22:20:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "k0l1m2n3o4p5"
down_revision = "j0k1l2m3n4o5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_cache_entry",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(length=64), nullable=False),
        sa.Column("cache_key", sa.String(length=64), nullable=False),
        sa.Column("question_hash", sa.String(length=64), nullable=False),
        sa.Column("retrieval_context_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "key_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "response_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "source_scope",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "observed_count",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "potential_saved_requests",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "potential_saved_tokens",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "is_enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("disabled_reason", sa.String(length=255), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "purpose",
            "cache_key",
            name="uq_llm_cache_entry_purpose_key",
        ),
    )
    op.create_index(
        op.f("ix_llm_cache_entry_is_enabled"),
        "llm_cache_entry",
        ["is_enabled"],
        unique=False,
    )
    op.create_index(
        op.f("ix_llm_cache_entry_last_seen_at"),
        "llm_cache_entry",
        ["last_seen_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_llm_cache_entry_purpose"),
        "llm_cache_entry",
        ["purpose"],
        unique=False,
    )
    op.create_index(
        op.f("ix_llm_cache_entry_question_hash"),
        "llm_cache_entry",
        ["question_hash"],
        unique=False,
    )
    op.create_index(
        op.f("ix_llm_cache_entry_retrieval_context_hash"),
        "llm_cache_entry",
        ["retrieval_context_hash"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_llm_cache_entry_retrieval_context_hash"),
        table_name="llm_cache_entry",
    )
    op.drop_index(op.f("ix_llm_cache_entry_question_hash"), table_name="llm_cache_entry")
    op.drop_index(op.f("ix_llm_cache_entry_purpose"), table_name="llm_cache_entry")
    op.drop_index(op.f("ix_llm_cache_entry_last_seen_at"), table_name="llm_cache_entry")
    op.drop_index(op.f("ix_llm_cache_entry_is_enabled"), table_name="llm_cache_entry")
    op.drop_table("llm_cache_entry")
