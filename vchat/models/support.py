import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import ForeignKey


from .base import Base, Created, UUID7, Updated


class Request(Base, Created, Updated, UUID7):
    __tablename__ = "request"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chat.id"))
    status: Mapped[str] = mapped_column(sa.String(20), default="open")
    email: Mapped[str]
    phone: Mapped[str]
    subject: Mapped[str]
    text: Mapped[str] = mapped_column(sa.Text)
