import asyncio

import aiohttp_jinja2
import sqlalchemy as sa
from aiohttp import web
from aiohttp_session import get_session, new_session
from passlib.context import CryptContext

from vchat.app_keys import CONFIG_KEY, REDIS_KEY
from vchat.i18n import _
from vchat.middlewares import UserInfo
from vchat.models import User
from vchat.utils import (
    admin_event,
    login_required,
    meta,
)

from . import forms
from .ldap import authenticate_ldap

__all__ = [
    "login",
    "login_ldap",
    "logout",
]


LOGIN_FAILURE_DELAY_SECONDS = 3
LOGIN_CHECK_LOCK_TTL_SECONDS = LOGIN_FAILURE_DELAY_SECONDS
password_context = CryptContext(schemes=["pbkdf2_sha512"], deprecated="auto")


@meta(title=_("Login to vchat"))
@aiohttp_jinja2.template("auth/login.html")
async def login(request):
    config = request.app[CONFIG_KEY]
    if not config.get("auth_basic_enabled", True):
        return web.HTTPFound(location=request.app.router["login_ldap"].url_for())

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
            return {"form": form, "ldap_enabled": config.get("auth_ldap_enabled", False)}

        await request.app[REDIS_KEY].set(lock_key, "1", ex=LOGIN_CHECK_LOCK_TTL_SECONDS)

        result = await request["db"].execute(
            sa.select(User).where(User.email == normalized_email)
        )
        user = result.scalar()
        if not user:
            await asyncio.sleep(LOGIN_FAILURE_DELAY_SECONDS)
            form.email.errors.append(_("Email or password is incorrect"))
            return {"form": form, "ldap_enabled": config.get("auth_ldap_enabled", False)}
        if user.is_active is False:
            form.email.errors.append(
                _(
                    "You have not confirmed your email. Check your email and Spam "
                    "folder for the activation link, then try again."
                )
            )
            return {"form": form, "ldap_enabled": config.get("auth_ldap_enabled", False)}
        if user.is_ldap:
            form.email.errors.append(_("This account uses LDAP authentication"))
            return {"form": form, "ldap_enabled": config.get("auth_ldap_enabled", False)}
        if not user.password or not password_context.verify(form.password.data, user.password):
            await asyncio.sleep(LOGIN_FAILURE_DELAY_SECONDS)
            form.email.errors.append(_("Wrong email or password"))
            return {"form": form, "ldap_enabled": config.get("auth_ldap_enabled", False)}

        # Warning: always use new_session() instead of get_session() in login views
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

    return {"form": form, "ldap_enabled": config.get("auth_ldap_enabled", False)}


@meta(title=_("LDAP Login to vchat"))
@aiohttp_jinja2.template("auth/login_ldap.html")
async def login_ldap(request):
    config = request.app[CONFIG_KEY]
    if not config.get("auth_ldap_enabled", False):
        return web.HTTPFound(location=request.app.router["login"].url_for())

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
            return {"form": form, "basic_enabled": config.get("auth_basic_enabled", True)}

        await request.app[REDIS_KEY].set(lock_key, "1", ex=LOGIN_CHECK_LOCK_TTL_SECONDS)

        ldap_result = await authenticate_ldap(normalized_email, form.password.data, config)
        if ldap_result is None:
            await asyncio.sleep(LOGIN_FAILURE_DELAY_SECONDS)
            form.email.errors.append(_("Email or password is incorrect"))
            return {"form": form, "basic_enabled": config.get("auth_basic_enabled", True)}

        result = await request["db"].execute(
            sa.select(User).where(User.email == normalized_email)
        )
        user = result.scalar()
        if user is None:
            user = User(
                email=ldap_result["email"],
                name=ldap_result["name"],
                password=None,
                is_active=True,
                is_ldap=True,
            )
            request["db"].add(user)
            await request["db"].flush()

        # Warning: always use new_session() instead of get_session() in login views
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

    return {"form": form, "basic_enabled": config.get("auth_basic_enabled", True)}


@meta(title=_("Logout from vchat"))
@login_required()
async def logout(request):
    await admin_event("user_logout", request)
    session = await get_session(request)
    session.invalidate()
    return web.HTTPFound(location=request.app.router["login"].url_for())
