import asyncio

import aiohttp_jinja2
import sqlalchemy as sa
from aiohttp import web
from aiohttp_session import get_session, new_session
from passlib.hash import pbkdf2_sha512

from vchat.i18n import _
from vchat.models import User
from vchat.utils import (
    DELAY_PROTECTION,
    admin_event,
    login_required,
    meta,
)

from . import forms

__all__ = [
    "login",
    "logout",
]


@meta(title=_("Login to vchat"))
@aiohttp_jinja2.template("auth/login.html")
async def login(request):
    session = await get_session(request)
    data = await request.post()
    form = forms.LoginForm(data, meta={"csrf_context": session})

    if request.method == "POST" and form.validate():
        record = await request["db"].execute(
            sa.select(User).where(User.email == form.email.data.lower())
        )
        user = record.scalar()
        if not user:
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
        if not pbkdf2_sha512.verify(form.password.data, user.password):
            # Delay brute force
            await asyncio.sleep(DELAY_PROTECTION)
            form.email.errors.append(_("Wrong email or password"))
            return {"form": form}
        # Save to aoihttp session
        # Warning
        # Always use new_session() instead of get_session() in your login views
        # to guard against Session Fixation attacks!

        session = await new_session(request)
        session["staff_id"] = user.id
        request["user"] = user
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
