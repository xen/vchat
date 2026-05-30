import hashlib
from datetime import datetime

import pycld2 as cld2
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import ENUM, JSONB, TSVECTOR
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, Created, Updated, generate_uuid7

from .source_config import SourceConfig


class Chat(Base, Created, Updated):
    __tablename__ = "chat"

    id: Mapped[str] = mapped_column(
        sa.String(36),
        primary_key=True,
        default=generate_uuid7,
    )
    title: Mapped[str] = mapped_column(sa.String(200), nullable=False, default="")
    user_uid: Mapped[str] = mapped_column(sa.String(256), nullable=False, index=True)
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


chat_role_enum = ENUM(
    "system",
    "user",
    "assistant",
    name="chatrole",
    create_type=False,
)


class ChatMsg(Base):
    __tablename__ = "chat_msg"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    full_context: Mapped[str] = mapped_column(sa.Text, nullable=False)
    role: Mapped[str] = mapped_column(chat_role_enum, nullable=False)
    chat_id: Mapped[str] = mapped_column(
        sa.String(36),
        ForeignKey("chat.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
    )
    user_uid: Mapped[str] = mapped_column(sa.String(256), nullable=False, index=True)
    used_chunks: Mapped[list[dict] | None] = mapped_column(JSONB, nullable=True)
    tokens: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    provider: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    vote: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    guardrail_triggered: Mapped[bool] = mapped_column(
        sa.Boolean,
        nullable=False,
        default=False,
        server_default=sa.text("false"),
        index=True,
    )
    guardrail_stage: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    guardrail_reasons: Mapped[list[str] | None] = mapped_column(
        sa.ARRAY(sa.String()),
        nullable=True,
    )


class Source(Base, Created, Updated):
    __tablename__ = "source"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    title: Mapped[str] = mapped_column(sa.String(255), nullable=False, default="")
    uri: Mapped[str] = mapped_column(sa.String, nullable=False)
    _config: Mapped[dict] = mapped_column("config", JSONB, nullable=False, default=dict)

    @property
    def config(self) -> "SourceConfig":
        return SourceConfig.from_dict(self._config)

    @config.setter
    def config(self, value: "SourceConfig") -> None:
        self._config = value.to_dict()

    start_pages: Mapped[list[str]] = mapped_column(
        sa.ARRAY(sa.Text),
        nullable=False,
        default=list,
        server_default=sa.text("'{}'"),
    )
    reindex_cron: Mapped[str] = mapped_column(
        sa.String(100),
        nullable=False,
        default="0 3 * * 1",
        server_default=sa.text("'0 3 * * 1'"),
    )
    last_reindexed_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
    )
    robots_cache: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


status_enum = ENUM(
    "added",
    "indexed",
    "pending",
    "unchanged",
    "ok",
    "error_4xx",
    "error_5xx",
    "blocked",
    "redirect",
    "no_content",
    "excluded_robots",
    "excluded_rules",
    "excluded_auth",
    "excluded_ignored",
    name="status",
    create_type=False,
)


class Page(Base, Created, Updated):
    __tablename__ = "page"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    uri: Mapped[str] = mapped_column(sa.String, nullable=True)
    source_id: Mapped[int | None] = mapped_column(
        sa.Integer,
        ForeignKey("source.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    content: Mapped[str] = mapped_column(sa.Text, nullable=True)
    _hash: Mapped[str] = mapped_column("hash", sa.String(64), nullable=False)
    _lang: Mapped[str] = mapped_column(
        "lang", sa.String(2), nullable=False, default="ru"
    )
    _length: Mapped[int] = mapped_column(
        "length", sa.Integer, nullable=False, default=0
    )
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        status_enum, nullable=False, default="added", index=True
    )
    title: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    is_ignored: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)

    # New crawler fields
    http_status: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    last_crawled_at: Mapped[datetime | None] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    last_etag: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    last_modified_at: Mapped[datetime | None] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    check_interval_days: Mapped[int] = mapped_column(
        sa.Integer,
        nullable=False,
        default=7,
        server_default=sa.text("7"),
    )
    stable_count: Mapped[int] = mapped_column(
        sa.Integer,
        nullable=False,
        default=0,
        server_default=sa.text("0"),
    )
    error_count: Mapped[int] = mapped_column(
        sa.Integer,
        nullable=False,
        default=0,
        server_default=sa.text("0"),
    )
    is_hub_page: Mapped[bool] = mapped_column(
        sa.Boolean,
        nullable=False,
        default=False,
        server_default=sa.text("false"),
    )
    content_value: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    inlink_count: Mapped[int] = mapped_column(
        sa.Integer,
        nullable=False,
        default=0,
        server_default=sa.text("0"),
    )

    @hybrid_property
    def hash_value(self) -> str:
        return self._hash

    @hash_value.setter
    def hash_value(self, value: str) -> None:
        self._hash = hashlib.sha256(value.encode("utf-8")).hexdigest()

    @hybrid_property
    def language(self) -> str:
        return self.lang

    @language.setter
    def language(self, value: str) -> None:
        is_reliable, _, details = cld2.detect(value)
        if is_reliable and details:
            self.lang = details[0][1]
        else:
            self.lang = ""

    @hybrid_property
    def length(self) -> int:
        return self._length

    @length.setter
    def length(self, value: str | int) -> None:
        if isinstance(value, str):
            self._length = len(value)
        else:
            self._length = value


# Backward-compatible alias
Document = Page


class Chunk(Base, Created, Updated):
    __tablename__ = "chunk"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    chat_id: Mapped[str | None] = mapped_column(
        sa.String(36),
        ForeignKey("chat.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    user_uid: Mapped[str] = mapped_column(sa.String(256), nullable=False, index=True)
    msg_id: Mapped[int | None] = mapped_column(
        sa.Integer,
        ForeignKey("chat_msg.id"),
        nullable=True,
        index=True,
    )
    page_id: Mapped[int | None] = mapped_column(
        sa.Integer,
        ForeignKey("page.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    chunk_ix: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    start_offset: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    end_offset: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)

    kind: Mapped[str] = mapped_column(
        sa.String(32),
        nullable=False,
        default="text",
        server_default=sa.text("'text'"),
        index=True,
    )
    header_text: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    section_path: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    entity_terms: Mapped[list[str] | None] = mapped_column(
        sa.ARRAY(sa.String()),
        nullable=True,
    )
    token_count: Mapped[int] = mapped_column(
        sa.Integer,
        nullable=False,
        default=0,
        server_default=sa.text("0"),
    )
    text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    fts: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024), nullable=True)


class Sitemap(Base):
    __tablename__ = "sitemap"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        sa.Integer,
        ForeignKey("source.id", ondelete="CASCADE"),
        nullable=False,
    )
    url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    is_excluded: Mapped[bool] = mapped_column(
        sa.Boolean,
        nullable=False,
        default=False,
        server_default=sa.text("false"),
    )
    discovered_via: Mapped[str] = mapped_column(
        sa.String(32),
        nullable=False,
        default="manual",
        server_default=sa.text("'manual'"),
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    )
    last_fetched_at: Mapped[datetime | None] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    last_etag: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    last_content_hash: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    url_count: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)


class PageLink(Base):
    __tablename__ = "page_link"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    source_uri: Mapped[str] = mapped_column(sa.Text, nullable=False)
    target_uri: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source_page_id: Mapped[int | None] = mapped_column(
        sa.Integer,
        ForeignKey("page.id", ondelete="SET NULL"),
        nullable=True,
    )
    target_page_id: Mapped[int | None] = mapped_column(
        sa.Integer,
        ForeignKey("page.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_id: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    target_status: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    found_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    )


class CrawlRun(Base):
    __tablename__ = "crawl_run"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        sa.Integer,
        ForeignKey("source.id", ondelete="CASCADE"),
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    pages_crawled: Mapped[int | None] = mapped_column(
        sa.Integer, nullable=True, server_default=sa.text("0")
    )
    pages_new: Mapped[int | None] = mapped_column(
        sa.Integer, nullable=True, server_default=sa.text("0")
    )
    pages_changed: Mapped[int | None] = mapped_column(
        sa.Integer, nullable=True, server_default=sa.text("0")
    )
    pages_errors: Mapped[int | None] = mapped_column(
        sa.Integer, nullable=True, server_default=sa.text("0")
    )
    pages_excluded: Mapped[int | None] = mapped_column(
        sa.Integer, nullable=True, server_default=sa.text("0")
    )
    was_rate_limited: Mapped[bool | None] = mapped_column(
        sa.Boolean, nullable=True, server_default=sa.text("false")
    )
    exit_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(sa.Text, nullable=True)


class Settings(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(sa.String(255), primary_key=True)
    value: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
