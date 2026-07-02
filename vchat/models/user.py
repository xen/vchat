from datetime import datetime
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
    password: Mapped[str | None]
    is_active: Mapped[bool] = mapped_column(default=False)
    is_ldap: Mapped[bool] = mapped_column(default=False)


class UserSession(Base, Created, Updated):
    __tablename__ = "user_session"
    __table_args__ = (
        sa.Index("ix_user_session_user_active", "user_id", "revoked_at"),
        sa.Index("ix_user_session_token", "session_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(sa.String(128))
    user_agent: Mapped[str | None] = mapped_column(sa.String(512))
    last_seen_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(sa.String(64))
