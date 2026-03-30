from typing import Annotated

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, Created, Updated

str_2 = Annotated[str, 2]


class User(Base, Created, Updated):
    __tablename__ = "users"
    __table_args__ = (sa.Index("ix_users_email", "email", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str]
    name: Mapped[str]
    password: Mapped[str]
    is_active: Mapped[bool] = mapped_column(default=False)
