import hashlib
from datetime import datetime

import pycld2 as cld2
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import ENUM, JSONB, TSVECTOR
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, Created, Updated, ShortId

project_role_enum = ENUM(
    "owner",
    "member",
    name="projectrole",
    create_type=False,
)


class Project(Base, Created, Updated):
    __tablename__ = "project"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    title: Mapped[str] = mapped_column(sa.String(62), nullable=False, default="")
    user_id: Mapped[int] = mapped_column(
        sa.Integer, ForeignKey("users.id"), nullable=False
    )
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    system_prompt: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    agent_style: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    provider: Mapped[str] = mapped_column(
        sa.String(64),
        nullable=False,
        default="openai",
        server_default=sa.text("'openai'"),
    )
    model: Mapped[str] = mapped_column(
        sa.String(128),
        nullable=False,
        default="gpt-4o-mini",
        server_default=sa.text("'gpt-4o-mini'"),
    )
    crawl_page_limit: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=100
    )


chat_type_enum = ENUM(
    "chat",
    "call",
    name="chattype",
    create_type=False,
)


class Chat(Base, Created, Updated, ShortId):
    __tablename__ = "chat"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    title: Mapped[str] = mapped_column(sa.String(200), nullable=False, default="")
    user_uid: Mapped[str] = mapped_column(sa.String(256), nullable=False, index=True)
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    type: Mapped[str] = mapped_column(chat_type_enum, nullable=False, default="chat")
    record: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")


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
    chat_id: Mapped[int] = mapped_column(
        sa.Integer,
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
    vote: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    vote_comment: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    # is_pinned: Mapped[bool] = mapped_column(default=False, nullable=False)


source_type_enum = ENUM(
    "site",
    "sitemap",
    "list",
    "s3",
    "google_drive",
    "upload",
    name="sourcetype",
    create_type=False,
)


class Source(Base, Created, Updated):
    __tablename__ = "source"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    type: Mapped[str] = mapped_column(source_type_enum, nullable=False)
    title: Mapped[str] = mapped_column(sa.String(255), nullable=False, default="")
    uri: Mapped[str] = mapped_column(sa.String, nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


status_enum = ENUM(
    "added",
    "indexed",
    name="status",
    create_type=False,
)


class Document(Base, Created, Updated, ShortId):
    __tablename__ = "document"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    uri: Mapped[str] = mapped_column(sa.String, nullable=True)
    source_id: Mapped[int] = mapped_column(
        sa.Integer,
        ForeignKey("source.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    content: Mapped[str] = mapped_column(sa.Text, nullable=True)
    _hash: Mapped[str] = mapped_column("hash", sa.String(64), nullable=False)  # sha256
    _lang: Mapped[str] = mapped_column(
        "lang", sa.String(2), nullable=False, default="ru"
    )
    _length: Mapped[int] = mapped_column(
        "length", sa.Integer, nullable=False, default=0
    )  # length in characters
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        status_enum, nullable=False, default="added", index=True
    )
    title: Mapped[str | None] = mapped_column(sa.String, nullable=True)
    is_ignored: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)

    # calculate hash before insert
    @hybrid_property
    def hash_value(self) -> str:
        return self._hash

    @hash_value.setter
    def hash_value(self, value: str) -> None:
        self._hash = hashlib.sha256(value.encode("utf-8")).hexdigest()

    # detect language using pycld2 and store in lang field
    @hybrid_property
    def language(self) -> str:
        return self.lang

    @language.setter
    def language(self, value: str) -> None:
        is_reliable, _, details = cld2.detect(value)
        if is_reliable and details:
            self.lang = details[0][1]  # get the most probable language code
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


class Chunk(Base, Created, Updated):
    __tablename__ = "chunk"

    id: Mapped[int] = mapped_column(sa.Integer, autoincrement=True)
    chat_id: Mapped[int | None] = mapped_column(
        sa.Integer,
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
    document_id: Mapped[int | None] = mapped_column(
        sa.Integer,
        ForeignKey("document.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    chunk_ix: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    start_offset: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    end_offset: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)

    content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    tsv: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024), nullable=True)

    __table_args__ = ({"postgresql_partition_by": "HASH (project_id)"},)
