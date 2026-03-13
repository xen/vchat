import sqlalchemy as sa
from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, Created, Updated


class Notify(Base, Created, Updated):
    __tablename__ = "notify"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        sa.Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    project_id: Mapped[int | None] = mapped_column(
        sa.Integer,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    task_name: Mapped[str] = mapped_column(sa.String(255), nullable=False, default="")
    body: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class NotifyRead(Base, Created):
    __tablename__ = "notify_read"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    notify_id: Mapped[int] = mapped_column(
        sa.Integer,
        ForeignKey("notify.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(
        sa.Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    __table_args__ = (
        UniqueConstraint("notify_id", "user_id", name="uq_notify_read_user"),
    )
