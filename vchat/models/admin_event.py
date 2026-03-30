import sqlalchemy as sa
from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, Created


class AdminEvent(Base, Created):
    __tablename__ = "admin_event"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        sa.Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    user_email: Mapped[str] = mapped_column(sa.String(254), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    event_name: Mapped[str] = mapped_column(sa.String(128), nullable=False, index=True)
