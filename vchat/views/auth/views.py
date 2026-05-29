import asyncio

import aiohttp_jinja2
import sqlalchemy as sa
from aiohttp import web
from aiohttp_session import get_session, new_session
from passlib.context import CryptContext

from vchat.app_keys import REDIS_KEY
from vchat.i18n import _
from vchat.middlewares import UserInfo
from vchat.models import User
from vchat.utils import (
    admin_event,
    login_required,
    meta,
)

from . import forms

__all__ = [
    "login",
    "logout",
]


LOGIN_FAILURE_DELAY_SECONDS = 3
LOGIN_CHECK_LOCK_TTL_SECONDS = LOGIN_FAILURE_DELAY_SECONDS
password_context = CryptContext(schemes=["pbkdf2_sha512"], deprecated="auto")


@meta(title=_("Login to vchat"))
@aiohttp_jinja2.template("auth/login.html")
async def login(request):
    session = await get_session(request)
    data = await request.post()
    form = forms.LoginForm(data, meta={"csrf_context": session})

    if request.method == "POST" and form.validate():
        normalized_email = (form.email.data or "").strip().lower()
        lock_key = f"auth:login_check_lock:{normalized_email}"

        if await request.app[REDIS_KEY].exists(lock_key):
            form.email.errors.append(
                _("Too many login attempts. Try again in a few seconds")
            )
            return {"form": form}

        await request.app[REDIS_KEY].set(lock_key, "1", ex=LOGIN_CHECK_LOCK_TTL_SECONDS)

        record = await request["db"].execute(
            sa.select(User).where(User.email == normalized_email)
        )
        user = record.scalar()
        if not user:
            await asyncio.sleep(LOGIN_FAILURE_DELAY_SECONDS)
            form.email.errors.append(_("Email or password is incorrect"))
            return {"form": form}
        if user.is_active is False:
            form.email.errors.append(
                _(
                    "You have not confirmed your email. Check your email and Spam "
                    "folder for the activation link, then try again."
                )
            )
            return {"form": form}
        if not password_context.verify(form.password.data, user.password):
            await asyncio.sleep(LOGIN_FAILURE_DELAY_SECONDS)
            form.email.errors.append(_("Wrong email or password"))
            return {"form": form}
        # Save to aoihttp session
        # Warning
        # Always use new_session() instead of get_session() in your login views
        # to guard against Session Fixation attacks!

        session = await new_session(request)
        session["user_id"] = user.id
        request["user"] = UserInfo(
            id=user.id,
            email=user.email,
            name=user.name,
            is_active=user.is_active,
        )
        await admin_event("user_login", request)

        target = "index"
        if request.rel_url.query.get("next"):
            target = request.rel_url.query["next"]
        return web.HTTPFound(location=request.app.router[target].url_for())

    return {"form": form}


@meta(title=_("Logout from vchat"))
@login_required()
async def logout(request):
    await admin_event("user_logout", request)
    session = await get_session(request)
    session.invalidate()
    return web.HTTPFound(location=request.app.router["login"].url_for())
