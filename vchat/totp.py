"""Mandatory local TOTP: RFC 6238, SHA-1, six digits, 30-second steps."""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import pyotp
import sqlalchemy as sa
from aiohttp import web
from aiohttp_session import new_session
from cryptography.fernet import Fernet

from vchat.models import User, UserTotp
from vchat.middlewares import UserInfo
from vchat.utils import admin_event
from vchat.settings import cfg

CHALLENGE_TTL = timedelta(minutes=5)
FAILURE_WINDOW = timedelta(minutes=15)
MAX_FAILURES = 5


def cipher() -> Fernet:
    return Fernet(cfg.totp_encryption_key.encode("ascii"))


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def password_digest(user: User) -> str:
    return token_digest(user.password or "")


def accepted_counter(
    secret: str, code: str, now: datetime, last: int | None
) -> int | None:
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        return None
    otp = pyotp.TOTP(secret, digits=6, interval=30)
    current = int(now.timestamp()) // 30
    # At most one adjacent time step in either direction; never reuse a step.
    for counter in (current + 1, current, current - 1):
        if (last is None or counter > last) and secrets.compare_digest(
            otp.at(counter * 30), code
        ):
            return counter
    return None


def clear_challenge(state: UserTotp) -> None:
    state.pending_secret = None
    state.token_hash = None
    state.password_digest = None
    state.expires_at = None


def rate_limited(state: UserTotp, now: datetime) -> bool:
    if (
        state.failure_window_at is None
        or now >= state.failure_window_at + FAILURE_WINDOW
    ):
        state.failures = 0
        state.failure_window_at = now
    return state.failures >= MAX_FAILURES


async def start_challenge(request: web.Request, user: User) -> None:
    db = request["db"]
    verified_password = password_digest(user)
    user = (
        await db.execute(
            sa.select(User)
            .where(User.id == user.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if not user.is_active or user.blocked_at is not None or user.is_ldap:
        raise web.HTTPForbidden()
    if password_digest(user) != verified_password:
        raise web.HTTPForbidden(text="Password changed; sign in again")
    now = datetime.now(timezone.utc)
    state = await db.get(UserTotp, user.id)
    if state is None:
        state = UserTotp(user_id=user.id, failures=0)
        db.add(state)
    if rate_limited(state, now):
        await db.rollback()
        raise web.HTTPTooManyRequests(
            text="Слишком много попыток. Повторите вход через 15 минут."
        )
    token = secrets.token_urlsafe(32)
    state.token_hash = token_digest(token)
    state.password_digest = password_digest(user)
    state.expires_at = now + CHALLENGE_TTL
    state.pending_secret = (
        cipher().encrypt(pyotp.random_base32().encode()).decode()
        if state.secret is None
        else None
    )
    request["user"] = UserInfo(user.id, user.email, user.name, user.is_active)
    await admin_event(
        "user_totp_setup_started"
        if state.secret is None
        else "user_totp_challenge_started",
        request,
    )
    request["user"] = None
    session = await new_session(request)
    session["totp_challenge"] = token
    next_path = request.rel_url.query.get("next", "").strip()
    if (
        next_path.startswith("/")
        and not next_path.startswith("//")
        and "\\" not in next_path
        and not any(ord(char) < 32 for char in next_path)
    ):
        session["totp_next"] = next_path


async def validate_totp_storage(db) -> None:
    """Fail startup on a lost/wrong encryption key or damaged stored credentials."""
    encryption = cipher()
    rows = await db.execute(sa.select(UserTotp.secret, UserTotp.pending_secret))
    for active, pending in rows:
        for encrypted in (active, pending):
            if encrypted is not None:
                encryption.decrypt(encrypted.encode("ascii"))
