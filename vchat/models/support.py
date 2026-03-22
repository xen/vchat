import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import ForeignKey


from .base import Base, Created, UUID7, Updated


class Request(Base, Created, Updated, UUID7):
    __tablename__ = "request"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chat.id"))
    status: Mapped[str] = mapped_column(sa.String(20), default="open")
    name: Mapped[str]
    email: Mapped[str]
    phone: Mapped[str]
    body: Mapped[str] = mapped_column(sa.Text)
    ip_address: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
