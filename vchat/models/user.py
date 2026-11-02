from datetime import datetime
from typing import Annotated

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, Created, Updated

str_2 = Annotated[str, 2]


class User(Base, Created, Updated):
    __tablename__ = "users"
    __table_args__ = (
        sa.Index("ix_users_email", "email", unique=True),
        sa.CheckConstraint(
            "blocked_at IS NULL OR NOT is_active", name="ck_users_blocked_inactive"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str]
    name: Mapped[str]
    password: Mapped[str | None]
    is_active: Mapped[bool] = mapped_column(default=False)
    blocked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
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
    totp_verified: Mapped[bool] = mapped_column(
        default=False, server_default=sa.false()
    )
    session_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(sa.String(128))
    user_agent: Mapped[str | None] = mapped_column(sa.String(512))
    last_seen_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(sa.String(64))


class UserTotp(Base):
    """Encrypted credentials and one short-lived password-verified challenge per user."""

    __tablename__ = "user_totp"

    user_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id"), primary_key=True)
    secret: Mapped[str | None]
    pending_secret: Mapped[str | None]
    token_hash: Mapped[str | None] = mapped_column(sa.String(64), unique=True)
    password_digest: Mapped[str | None]
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    failure_window_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True)
    )
    failures: Mapped[int] = mapped_column(default=0, server_default="0")
    last_counter: Mapped[int | None] = mapped_column(sa.BigInteger)
