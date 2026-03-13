from typing import Any, TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.schema import ForeignKey

from sqlalchemy.dialects.postgresql import JSONB

from .base import Base, Created, ShortId, Updated

if TYPE_CHECKING:  # pragma: no cover
    from .user import User


class Page(Base, Created, Updated):
    __tablename__ = "support_pages"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str]
    slug: Mapped[str]
    lang: Mapped[str] = mapped_column(sa.String(10))
    body: Mapped[str] = mapped_column(sa.Text)
    body_html: Mapped[str] = mapped_column(sa.Text)
    is_translated: Mapped[bool] = mapped_column(default=False)
    is_hidden: Mapped[bool] = mapped_column(default=False)
    from_id: Mapped[int | None] = mapped_column(
        ForeignKey("support_pages.id"), nullable=True
    )
    is_favorite: Mapped[bool] = mapped_column(default=False)
    favorit_order: Mapped[int] = mapped_column(default=0)
    chapter_id: Mapped[int] = mapped_column(ForeignKey("support_chapters.id"))
    order: Mapped[int] = mapped_column(default=0)


class Chapter(Base, Created, Updated):
    __tablename__ = "support_chapters"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str]
    lang: Mapped[str] = mapped_column(sa.String(10))
    slug: Mapped[str]
    is_translated: Mapped[bool] = mapped_column(default=False)
    is_hidden: Mapped[bool] = mapped_column(default=False)
    from_id: Mapped[int | None] = mapped_column(
        ForeignKey("support_chapters.id"), nullable=True
    )
    order: Mapped[int] = mapped_column(default=0)


class Ticket(Base, Created, Updated, ShortId):
    __tablename__ = "support_tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    subject: Mapped[str]
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(sa.String(20), default="open")
    lang: Mapped[str] = mapped_column(sa.String(10))
    user: Mapped["User"] = relationship("User", backref="support_tickets")
    comments: Mapped[list["TicketComment"]] = relationship(
        "TicketComment",
        back_populates="ticket",
        cascade="all, delete-orphan",
    )


class TicketComment(Base, Created, Updated):
    __tablename__ = "support_ticket_comments"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("support_tickets.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    is_by_admin: Mapped[bool] = mapped_column(default=False)
    body: Mapped[str] = mapped_column(sa.Text)
    is_internal: Mapped[bool] = mapped_column(default=False)
    uploads: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    ticket: Mapped["Ticket"] = relationship("Ticket", back_populates="comments")
    user: Mapped["User"] = relationship("User")
