"""Enrollment and second-factor form. The challenge cookie grants no user identity."""

import base64
import io
from datetime import datetime, timezone

import aiohttp_jinja2
import pyotp
import qrcode
import sqlalchemy as sa
from aiohttp import web
from aiohttp_session import get_session

from vchat.middlewares import UserInfo
from vchat.models import User, UserTotp
from vchat.settings import cfg
from vchat.totp import (
    accepted_counter,
    cipher,
    clear_challenge,
    password_digest,
    rate_limited,
    token_digest,
)
from vchat.utils import admin_event, meta
from .forms import TotpConfirm
from .views import _finish_login


@meta(title="Подтверждение входа в VChat")
async def totp(request):
    if not cfg.auth_basic_enabled:
        raise web.HTTPForbidden()
    session = await get_session(request)
    token = session.get("totp_challenge")
    if not isinstance(token, str):
        raise web.HTTPFound(location=request.app.router["login"].url_for())
    db = request["db"]
    # Always lock the user first, just like permanent blocking and session creation.
    user = (
        await db.execute(
            sa.select(User)
            .join(UserTotp, User.id == UserTotp.user_id)
            .where(UserTotp.token_hash == token_digest(token))
            .with_for_update(of=User)
        )
    ).scalar_one_or_none()
    if (
        user is None
        or not user.is_active
        or user.blocked_at is not None
        or user.is_ldap
    ):
        session.invalidate()
        raise web.HTTPForbidden()
    state = await db.get(UserTotp, user.id, populate_existing=True)
    now = datetime.now(timezone.utc)
    if state.token_hash != token_digest(token):
        raise web.HTTPForbidden()
    if (
        state.expires_at is None
        or now >= state.expires_at
        or state.password_digest != password_digest(user)
    ):
        clear_challenge(state)
        await db.commit()
        session.invalidate()
        raise web.HTTPFound(location=request.app.router["login"].url_for())
    if rate_limited(state, now):
        clear_challenge(state)
        await db.commit()
        session.invalidate()
        raise web.HTTPTooManyRequests(
            text="Слишком много попыток. Повторите вход через 15 минут."
        )

    enrollment = state.secret is None
    encrypted_secret = state.pending_secret if enrollment else state.secret
    secret = cipher().decrypt(encrypted_secret.encode()).decode()
    data = await request.post() if request.method == "POST" else None
    form = TotpConfirm(formdata=data, meta={"csrf_context": session})
    status = 200
    if request.method == "POST":
        valid = form.validate()
        # Invalid CSRF never consumes an OTP or changes enrollment.
        if form.errors.get("csrf_token"):
            status = 400
        else:
            counter = (
                accepted_counter(secret, form.code.data, now, state.last_counter)
                if valid
                else None
            )
            request["user"] = UserInfo(user.id, user.email, user.name, user.is_active)
            if counter is not None:
                if enrollment:
                    state.secret = state.pending_secret
                state.last_counter = counter
                clear_challenge(state)
                next_path = session.get("totp_next")
                await _finish_login(request, user=user, totp_verified=True)
                await admin_event(
                    "user_totp_enrolled" if enrollment else "user_totp_verified",
                    request,
                )
                return web.HTTPFound(
                    location=next_path or request.app.router["index"].url_for()
                )
            state.failures += 1
            await admin_event("user_totp_failed", request)
            request["user"] = None
            form.add_code_error("Неверный, истекший или уже использованный код")
            status = 400
            if rate_limited(state, now):
                clear_challenge(state)
                await db.commit()
                session.invalidate()
                raise web.HTTPTooManyRequests(
                    text="Слишком много попыток. Повторите вход через 15 минут."
                )
    # Never make the password-verified actor visible to template/admin contexts.
    request["user"] = None
    qr_data = None
    if enrollment:
        uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name="VChat")
        qr = qrcode.QRCode(box_size=6, border=4)
        qr.add_data(uri)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")
        output = io.BytesIO()
        image.save(output, format="PNG")
        qr_data = base64.b64encode(output.getvalue()).decode("ascii")
    await db.commit()
    response = aiohttp_jinja2.render_template(
        "auth/totp.html",
        request,
        {"form": form, "enrollment": enrollment, "qr_data": qr_data},
        status=status,
    )
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    )
    return response
