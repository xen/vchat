import asyncio
import logging
import secrets
import time
from typing import Any

import aiohttp_jinja2
import sqlalchemy as sa
import bonsai
from aiohttp import web
from aiohttp_session import get_session, new_session
from datetime import datetime, timezone
from passlib.context import CryptContext

from vchat.settings import REDIS_KEY
from vchat.middlewares import UserInfo
from vchat.models import User, UserSession
from vchat.settings import cfg
from vchat.utils import (
    admin_event,
    get_client_ip,
    login_required,
    meta,
    validate_signed_user_csrf,
)
from . import forms

__all__ = [
    "login",
    "login_ldap",
    "logout",
    "sessions",
    "sessions_action",
]

logger = logging.getLogger(__name__)


LOGIN_FAILURE_DELAY_SECONDS = 3
password_context = CryptContext(schemes=["pbkdf2_sha512"], deprecated="auto")


async def _record_login_failure(
    *,
    redis: Any,
    failure_key: str,
    lockout_key: str,
) -> None:
    await asyncio.sleep(LOGIN_FAILURE_DELAY_SECONDS)
    attempts = int(await redis.incr(failure_key))
    if attempts == 1:
        await redis.expire(failure_key, 15 * 60)
    if attempts >= 10:
        await redis.set(lockout_key, "1", ex=15 * 60)


async def _finish_login(
    request: web.Request,
    *,
    user: User,
    redis: Any,
    failure_key: str,
) -> None:
    session = await new_session(request)
    session["user_id"] = user.id
    session["login_at"] = int(time.time())
    session_id = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    session["session_id"] = session_id
    request["db"].add(
        UserSession(
            user_id=user.id,
            session_id=session_id,
            ip_address=get_client_ip(request),
            user_agent=(request.headers.get("User-Agent") or "")[:512] or None,
            last_seen_at=now,
            revoked_at=None,
            revoked_reason=None,
            updated_at=now,
        )
    )
    await request["db"].flush()
    request["user"] = UserInfo(
        id=user.id,
        email=user.email,
        name=user.name,
        is_active=user.is_active,
    )
    await redis.delete(failure_key)
    await admin_event("user_login", request)
    await request["db"].commit()


async def _render_sessions_form_error(
    request: web.Request,
    *,
    current_session_id: str | None,
    form: forms.PasswordChange,
) -> web.Response:
    rows = (
        (
            await request["db"].execute(
                sa.select(UserSession)
                .where(UserSession.user_id == request["user"].id)
                .order_by(
                    UserSession.revoked_at.is_(None).desc(),
                    UserSession.created_at.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    return aiohttp_jinja2.render_template(
        "auth/sessions.html",
        request,
        {
            "sessions": rows,
            "current_session_id": current_session_id,
            "password_form": form,
        },
        status=400,
    )


def _ldap_attr_values(entry: Any, attr_name: str) -> list[str]:
    raw_values = entry.get(attr_name, [])
    if raw_values is None:
        return []
    if isinstance(raw_values, str):
        return [raw_values]
    return [str(value) for value in raw_values]


def _normalize_ldap_dn(value: str) -> str:
    return ",".join(part.strip().casefold() for part in value.split(","))


async def authenticate_ldap(email: str, password: str) -> dict | None:
    search_filter = cfg.ldap_search_filter.format(email=bonsai.escape_filter_exp(email))
    required_group_dn = cfg.ldap_required_group_dn.strip()
    attrlist = [cfg.ldap_attr_name]
    if required_group_dn and cfg.ldap_member_of_attr not in attrlist:
        attrlist.append(cfg.ldap_member_of_attr)

    service_client = bonsai.LDAPClient(cfg.ldap_server, tls=cfg.ldap_use_ssl)
    if cfg.ldap_bind_dn:
        service_client.set_credentials(
            "SIMPLE", user=cfg.ldap_bind_dn, password=cfg.ldap_bind_password
        )

    try:
        async with service_client.connect(is_async=True) as conn:
            results = await conn.search(
                base=cfg.ldap_search_base,
                scope=bonsai.LDAPSearchScope.SUB,
                filter_exp=search_filter,
                attrlist=attrlist,
            )
    except bonsai.LDAPError:
        logger.exception("LDAP service bind or search failed for %s", email)
        return None

    if not results:
        return None

    user_entry = results[0]
    user_dn = str(user_entry.dn)
    if required_group_dn:
        required_group = _normalize_ldap_dn(required_group_dn)
        user_groups = {
            _normalize_ldap_dn(group_dn)
            for group_dn in _ldap_attr_values(user_entry, cfg.ldap_member_of_attr)
        }
        if required_group not in user_groups:
            return None

    name_values = _ldap_attr_values(user_entry, cfg.ldap_attr_name)
    name = name_values[0] if name_values else email

    user_client = bonsai.LDAPClient(cfg.ldap_server, tls=cfg.ldap_use_ssl)
    user_client.set_credentials("SIMPLE", user=user_dn, password=password)

    try:
        async with user_client.connect(is_async=True):
            return {"email": email, "name": name}
    except bonsai.AuthenticationError:
        return None
    except bonsai.LDAPError:
        logger.exception("LDAP user bind failed for dn=%s", user_dn)
        return None


@meta(title="Вход в vchat")
@aiohttp_jinja2.template("auth/login.html")
async def login(request):
    if not cfg.auth_basic_enabled:
        raise web.HTTPFound(location=request.app.router["login_ldap"].url_for())

    session = await get_session(request)
    data = await request.post()
    form = forms.Login(data, meta={"csrf_context": session})

    if request.method == "POST" and form.validate():
        normalized_email = form.email.data.strip().lower()
        redis = request.app[REDIS_KEY]
        login_check_lock_key = f"auth:login_check_lock:{normalized_email}"
        login_failure_key = f"auth:login_failures:{normalized_email}"
        login_lockout_key = f"auth:login_lockout:{normalized_email}"

        if await redis.exists(login_check_lock_key) or await redis.exists(
            login_lockout_key
        ):
            await admin_event("user_login_rate_limited", request)
            form.add_email_error("Слишком много попыток входа. Попробуйте позже")
            return {
                "form": form,
                "ldap_enabled": cfg.auth_ldap_enabled,
            }

        await redis.set(
            login_check_lock_key,
            "1",
            ex=LOGIN_FAILURE_DELAY_SECONDS,
        )

        result = await request["db"].execute(
            sa.select(User).where(User.email == normalized_email)
        )
        user = result.scalar()
        if not user:
            await _record_login_failure(
                redis=redis,
                failure_key=login_failure_key,
                lockout_key=login_lockout_key,
            )
            await admin_event("user_login_failed", request)
            form.add_email_error("Неверный email или пароль")
            return {
                "form": form,
                "ldap_enabled": cfg.auth_ldap_enabled,
            }
        if user.is_active is False:
            form.add_email_error(
                "Вы не подтвердили email. Проверьте почту и папку Спам, "
                "затем попробуйте снова.",
            )
            return {
                "form": form,
                "ldap_enabled": cfg.auth_ldap_enabled,
            }
        if user.is_ldap:
            form.add_email_error(
                "Для этой учётной записи используется LDAP-аутентификация",
            )
            return {
                "form": form,
                "ldap_enabled": cfg.auth_ldap_enabled,
            }
        if not user.password or not password_context.verify(
            form.password.data, user.password
        ):
            await _record_login_failure(
                redis=redis,
                failure_key=login_failure_key,
                lockout_key=login_lockout_key,
            )
            await admin_event("user_login_failed", request)
            form.add_email_error("Неверный email или пароль")
            return {"form": form, "ldap_enabled": cfg.auth_ldap_enabled}

        # Warning: always use new_session() instead of get_session() in login views
        # to guard against Session Fixation attacks!
        await _finish_login(
            request,
            user=user,
            redis=redis,
            failure_key=login_failure_key,
        )

        next_location = (request.rel_url.query.get("next") or "").strip()
        if next_location.startswith("/") and not next_location.startswith("//"):
            raise web.HTTPFound(location=next_location)
        raise web.HTTPFound(location=request.app.router["index"].url_for())

    return {"form": form, "ldap_enabled": cfg.auth_ldap_enabled}


@meta(title="LDAP-вход в vchat")
@aiohttp_jinja2.template("auth/login_ldap.html")
async def login_ldap(request):
    if not cfg.auth_ldap_enabled:
        raise web.HTTPFound(location=request.app.router["login"].url_for())

    session = await get_session(request)
    data = await request.post()
    form = forms.Login(data, meta={"csrf_context": session})

    if request.method == "POST" and form.validate():
        normalized_email = form.email.data.strip().lower()
        redis = request.app[REDIS_KEY]
        login_check_lock_key = f"auth:login_check_lock:{normalized_email}"
        login_failure_key = f"auth:login_failures:{normalized_email}"
        login_lockout_key = f"auth:login_lockout:{normalized_email}"

        if await redis.exists(login_check_lock_key) or await redis.exists(
            login_lockout_key
        ):
            await admin_event("user_login_ldap_rate_limited", request)
            form.add_email_error("Слишком много попыток входа. Попробуйте позже")
            return {"form": form, "basic_enabled": cfg.auth_basic_enabled}

        await redis.set(
            login_check_lock_key,
            "1",
            ex=LOGIN_FAILURE_DELAY_SECONDS,
        )

        ldap_result = await authenticate_ldap(normalized_email, form.password.data)
        if ldap_result is None:
            await _record_login_failure(
                redis=redis,
                failure_key=login_failure_key,
                lockout_key=login_lockout_key,
            )
            await admin_event("user_login_ldap_failed", request)
            form.add_email_error("Неверный email или пароль")
            return {"form": form, "basic_enabled": cfg.auth_basic_enabled}

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
        elif user.is_active is False:
            form.add_email_error("Пользователь заблокирован")
            return {"form": form, "basic_enabled": cfg.auth_basic_enabled}
        elif not user.is_ldap:
            form.add_email_error(
                "Для этой учётной записи используется локальная аутентификация",
            )
            return {"form": form, "basic_enabled": cfg.auth_basic_enabled}

        # Warning: always use new_session() instead of get_session() in login views
        # to guard against Session Fixation attacks!
        await _finish_login(
            request,
            user=user,
            redis=redis,
            failure_key=login_failure_key,
        )

        next_location = (request.rel_url.query.get("next") or "").strip()
        if next_location.startswith("/") and not next_location.startswith("//"):
            raise web.HTTPFound(location=next_location)
        raise web.HTTPFound(location=request.app.router["index"].url_for())

    return {"form": form, "basic_enabled": cfg.auth_basic_enabled}


@meta(title="Выход из vchat")
@login_required()
async def logout(request):
    await admin_event("user_logout", request)
    session = await get_session(request)
    session_id = session.get("session_id")
    user_id = session.get("user_id")
    if session_id and user_id is not None:
        now = datetime.now(timezone.utc)
        await request["db"].execute(
            sa.update(UserSession)
            .where(
                UserSession.user_id == int(user_id),
                UserSession.session_id == session_id,
                UserSession.revoked_at.is_(None),
            )
            .values(
                revoked_at=now,
                revoked_reason="logout",
                updated_at=now,
            )
        )
    await request["db"].commit()
    session.invalidate()
    response = web.HTTPFound(location=request.app.router["login"].url_for())
    response.headers["Clear-Site-Data"] = '"cache", "storage"'
    raise response


@meta(title="Активные сессии")
@login_required()
@aiohttp_jinja2.template("auth/sessions.html")
async def sessions(request):
    auth_session = await get_session(request)
    current_session_id = auth_session.get("session_id")
    form = forms.PasswordChange(meta={"csrf_context": auth_session})
    rows = (
        (
            await request["db"].execute(
                sa.select(UserSession)
                .where(UserSession.user_id == request["user"].id)
                .order_by(
                    UserSession.revoked_at.is_(None).desc(),
                    UserSession.created_at.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    return {
        "sessions": rows,
        "current_session_id": current_session_id,
        "password_form": form,
    }


@meta(title="Активные сессии")
@login_required()
async def sessions_action(request):
    auth_session = await get_session(request)
    data = await request.post()
    action = data.get("action", "")
    current_session_id = auth_session.get("session_id")

    if action == "revoke_other":
        validate_signed_user_csrf(request, data.get("csrf_token"))
        now = datetime.now(timezone.utc)
        stmt = (
            sa.update(UserSession)
            .where(
                UserSession.user_id == request["user"].id,
                UserSession.revoked_at.is_(None),
            )
            .values(
                revoked_at=now,
                revoked_reason="user_revoke_other",
                updated_at=now,
            )
        )
        if current_session_id:
            stmt = stmt.where(UserSession.session_id != current_session_id)
        await request["db"].execute(stmt)
        await admin_event("user_session_revoke_other", request)
        await request["db"].commit()
        raise web.HTTPFound(location=request.app.router["sessions"].url_for())

    if action == "revoke_all":
        validate_signed_user_csrf(request, data.get("csrf_token"))
        now = datetime.now(timezone.utc)
        await request["db"].execute(
            sa.update(UserSession)
            .where(
                UserSession.user_id == request["user"].id,
                UserSession.revoked_at.is_(None),
            )
            .values(
                revoked_at=now,
                revoked_reason="user_revoke_all",
                updated_at=now,
            )
        )
        auth_session.invalidate()
        await admin_event("user_session_revoke_all", request)
        await request["db"].commit()
        response = web.HTTPFound(location=request.app.router["login"].url_for())
        response.headers["Clear-Site-Data"] = '"cache", "storage"'
        raise response

    if action == "change_password":
        if request["user"].is_active is False:
            raise web.HTTPForbidden()
        user = await request["db"].scalar(
            sa.select(User).where(
                User.id == request["user"].id,
                User.is_active.is_(True),
            )
        )
        if user is None:
            auth_session.invalidate()
            raise web.HTTPFound(location=request.app.router["login"].url_for())
        if user.is_ldap:
            raise web.HTTPForbidden(text="Password is managed by LDAP")

        form = forms.PasswordChange(data, meta={"csrf_context": auth_session})
        if not form.validate():
            return await _render_sessions_form_error(
                request,
                current_session_id=current_session_id,
                form=form,
            )
        if not user.password or not password_context.verify(
            form.current_password.data, user.password
        ):
            form.current_password.errors = [
                *form.current_password.errors,
                "Неверный текущий пароль",
            ]
            return await _render_sessions_form_error(
                request,
                current_session_id=current_session_id,
                form=form,
            )
        user.password = password_context.hash(form.password.data)
        user.updated_at = datetime.now(timezone.utc)
        now = datetime.now(timezone.utc)
        stmt = (
            sa.update(UserSession)
            .where(
                UserSession.user_id == user.id,
                UserSession.revoked_at.is_(None),
            )
            .values(
                revoked_at=now,
                revoked_reason="password_change",
                updated_at=now,
            )
        )
        if current_session_id:
            stmt = stmt.where(UserSession.session_id != current_session_id)
        await request["db"].execute(stmt)
        await admin_event("user_password_change", request)
        await request["db"].commit()
        raise web.HTTPFound(location=request.app.router["sessions"].url_for())

    raise web.HTTPBadRequest(text="Unknown session action")
