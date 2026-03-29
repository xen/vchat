"""single project settings and chat uuid key

Revision ID: 3f91f6d2b0aa
Revises: 9b1c2f4e6a11
Create Date: 2026-03-25 20:10:00.000000

"""
from __future__ import annotations

import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "3f91f6d2b0aa"
down_revision: Union[str, Sequence[str], None] = "9b1c2f4e6a11"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


DEFAULT_SETTINGS = {
    "project.title": "vchat",
    "project.system_prompt": "",
    "project.agent_style": "",
    "project.provider": "openai",
    "project.model": "gpt-4o-mini",
    "project.crawl_page_limit": "100",
    "project.agent_name": "",
    "project.welcome_message": "",
    "project.secret": "",
    "project.topics": "[]",
    "project.intents": "[]",
}


def _table_exists(inspector: sa.Inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _column_exists(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    if not _table_exists(inspector, table_name):
        return False
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def _upsert_setting(conn, key: str, value: str | None) -> None:
    conn.execute(
        sa.text(
            """
            INSERT INTO settings(key, value)
            VALUES (:key, :value)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """
        ),
        {"key": key, "value": value},
    )


def _insert_default_setting(conn, key: str, value: str | None) -> None:
    conn.execute(
        sa.text(
            """
            INSERT INTO settings(key, value)
            VALUES (:key, :value)
            ON CONFLICT (key) DO NOTHING
            """
        ),
        {"key": key, "value": value},
    )


def _drop_chat_fk_constraints(inspector: sa.Inspector) -> None:
    for table in ("chat_msg", "chunk", "request"):
        if not _table_exists(inspector, table):
            continue
        for fk in inspector.get_foreign_keys(table):
            if fk.get("referred_table") == "chat" and "chat_id" in (fk.get("constrained_columns") or []):
                name = fk.get("name")
                if name:
                    op.drop_constraint(name, table, type_="foreignkey")


def _create_chat_fk_constraints(inspector: sa.Inspector) -> None:
    if _table_exists(inspector, "chat_msg"):
        op.create_foreign_key(
            "fk_chat_msg_chat_id_chat",
            "chat_msg",
            "chat",
            ["chat_id"],
            ["id"],
            ondelete="CASCADE",
        )

    if _table_exists(inspector, "chunk"):
        op.create_foreign_key(
            "fk_chunk_chat_id_chat",
            "chunk",
            "chat",
            ["chat_id"],
            ["id"],
            ondelete="CASCADE",
        )

    if _table_exists(inspector, "request"):
        op.create_foreign_key(
            "fk_request_chat_id_chat",
            "request",
            "chat",
            ["chat_id"],
            ["id"],
        )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _table_exists(inspector, "settings"):
        op.create_table(
            "settings",
            sa.Column("key", sa.String(length=255), nullable=False),
            sa.Column("value", sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint("key"),
        )

    # migrate project data into settings before dropping project table
    if _table_exists(inspector, "project"):
        row = bind.execute(
            sa.text(
                """
                SELECT title, system_prompt, agent_style, provider, model,
                       crawl_page_limit, config, meta
                FROM project
                ORDER BY id ASC
                LIMIT 1
                """
            )
        ).mappings().first()

        if row:
            config = row.get("config") or {}
            meta = row.get("meta") or {}
            if not isinstance(config, dict):
                config = {}
            if not isinstance(meta, dict):
                meta = {}

            migrated = {
                "project.title": row.get("title") or DEFAULT_SETTINGS["project.title"],
                "project.system_prompt": row.get("system_prompt") or "",
                "project.agent_style": row.get("agent_style") or "",
                "project.provider": row.get("provider") or DEFAULT_SETTINGS["project.provider"],
                "project.model": row.get("model") or DEFAULT_SETTINGS["project.model"],
                "project.crawl_page_limit": str(row.get("crawl_page_limit") or DEFAULT_SETTINGS["project.crawl_page_limit"]),
                "project.agent_name": str(config.get("agent_name") or ""),
                "project.welcome_message": str(config.get("welcome_message") or ""),
                "project.secret": str(config.get("secret") or ""),
                "project.topics": json.dumps(meta.get("topics") if isinstance(meta.get("topics"), list) else []),
                "project.intents": json.dumps(meta.get("intents") if isinstance(meta.get("intents"), list) else []),
            }
            for key, value in migrated.items():
                _upsert_setting(bind, key, value)

    # ensure defaults exist
    for key, value in DEFAULT_SETTINGS.items():
        _insert_default_setting(bind, key, value)

    # remove short_id columns
    for table_name in ("chat", "document", "request"):
        if _column_exists(inspector, table_name, "short_id"):
            op.drop_column(table_name, "short_id")

    inspector = sa.inspect(bind)

    # convert chat id and related fks to string
    if _column_exists(inspector, "chat", "id"):
        _drop_chat_fk_constraints(inspector)
        inspector = sa.inspect(bind)

        op.alter_column(
            "chat",
            "id",
            existing_type=sa.Integer(),
            type_=sa.String(length=36),
            postgresql_using="id::text",
        )

        if _column_exists(inspector, "chat_msg", "chat_id"):
            op.alter_column(
                "chat_msg",
                "chat_id",
                existing_type=sa.Integer(),
                type_=sa.String(length=36),
                postgresql_using="chat_id::text",
                existing_nullable=False,
            )

        if _column_exists(inspector, "chunk", "chat_id"):
            op.alter_column(
                "chunk",
                "chat_id",
                existing_type=sa.Integer(),
                type_=sa.String(length=36),
                postgresql_using="chat_id::text",
                existing_nullable=True,
            )

        if _column_exists(inspector, "request", "chat_id"):
            op.alter_column(
                "request",
                "chat_id",
                existing_type=sa.Integer(),
                type_=sa.String(length=36),
                postgresql_using="chat_id::text",
                existing_nullable=False,
            )

        inspector = sa.inspect(bind)
        _create_chat_fk_constraints(inspector)

    # remove legacy project links if they existed in db
    op.execute("ALTER TABLE source DROP COLUMN IF EXISTS project_id CASCADE")
    op.execute("ALTER TABLE chat DROP COLUMN IF EXISTS project_id CASCADE")
    op.execute("ALTER TABLE chat_msg DROP COLUMN IF EXISTS project_id CASCADE")
    op.execute("ALTER TABLE chunk DROP COLUMN IF EXISTS project_id CASCADE")

    # remove project table
    op.execute("DROP TABLE IF EXISTS project CASCADE")


def downgrade() -> None:
    raise RuntimeError("Downgrade is not supported for single-project migration")
