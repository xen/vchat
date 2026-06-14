import asyncio
import logging
import time
from typing import Any

import aiohttp_jinja2
import sqlalchemy as sa
import bonsai
from aiohttp import web
from aiohttp_session import get_session, new_session
from datetime import timedelta
from passlib.context import CryptContext
from wtforms import Form, PasswordField, StringField, validators
from wtforms.csrf.session import SessionCSRF

from vchat.settings import CONFIG_KEY, REDIS_KEY
from vchat.middlewares import UserInfo
from vchat.models import User
from vchat.settings import config
from vchat.utils import (
    admin_event,
    login_required,
    meta,
)

__all__ = [
    "login",
    "login_ldap",
    "logout",
]

logger = logging.getLogger(__name__)


LOGIN_FAILURE_DELAY_SECONDS = 3
LOGIN_CHECK_LOCK_TTL_SECONDS = LOGIN_FAILURE_DELAY_SECONDS
password_context = CryptContext(schemes=["pbkdf2_sha512"], deprecated="auto")


def _ldap_attr_values(entry: Any, attr_name: str) -> list[str]:
    raw_values = entry.get(attr_name, [])
    if raw_values is None:
        return []
    if isinstance(raw_values, str):
        return [raw_values]
    return [str(value) for value in raw_values]


def _normalize_ldap_dn(value: str) -> str:
    return ",".join(part.strip().casefold() for part in value.split(","))


class LoginForm(Form):
    class Meta:
        csrf = True
        csrf_secret = config["secret_key"]
        csrf_class = SessionCSRF
        csrf_time_limit = timedelta(minutes=20)

    email = StringField(
        "Ваш email",
        [
            validators.Length(
                min=6,
                max=254,
                message="Длина от 6 до 254 символов",
            ),
            validators.Email(message="Введите корректный email"),
            validators.DataRequired(message="Обязательное поле"),
        ],
        render_kw={"placeholder": "name@company.com"},
    )
    password = PasswordField(
        "Ваш пароль",
        [
            validators.Length(min=4, max=35, message="Длина от 4 до 35 символов"),
            validators.DataRequired(message="Обязательное поле"),
        ],
        render_kw={"placeholder": "Пароль"},
    )


async def authenticate_ldap(email: str, password: str, config: dict) -> dict | None:
    server = config["ldap_server"]
    use_ssl = config.get("ldap_use_ssl", False)
    bind_dn = config.get("ldap_bind_dn", "")
    bind_password = config.get("ldap_bind_password", "")
    search_base = config["ldap_search_base"]
    search_filter = config["ldap_search_filter"].format(
        email=bonsai.escape_filter_exp(email)
    )
    attr_name = config.get("ldap_attr_name", "displayName")
    required_group_dn = (config.get("ldap_required_group_dn") or "").strip()
    member_of_attr = config.get("ldap_member_of_attr", "memberOf")
    attrlist = [attr_name]
    if required_group_dn and member_of_attr not in attrlist:
        attrlist.append(member_of_attr)

    service_client = bonsai.LDAPClient(server, tls=use_ssl)
    if bind_dn:
        service_client.set_credentials("SIMPLE", user=bind_dn, password=bind_password)

    try:
        async with service_client.connect(is_async=True) as conn:
            results = await conn.search(
                base=search_base,
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
            for group_dn in _ldap_attr_values(user_entry, member_of_attr)
        }
        if required_group not in user_groups:
            return None

    name_values = _ldap_attr_values(user_entry, attr_name)
    name = name_values[0] if name_values else email

    user_client = bonsai.LDAPClient(server, tls=use_ssl)
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
    config = request.app[CONFIG_KEY]
    if not config.get("auth_basic_enabled", True):
        raise web.HTTPFound(location=request.app.router["login_ldap"].url_for())

    session = await get_session(request)
    data = await request.post()
    form = LoginForm(data, meta={"csrf_context": session})

    if request.method == "POST" and form.validate():
        normalized_email = form.email.data.strip().lower()
        lock_key = f"auth:login_check_lock:{normalized_email}"

        if await request.app[REDIS_KEY].exists(lock_key):
            email_field: Any = form.email
            email_field.errors = [
                *form.email.errors,
                "Слишком много попыток входа. Попробуйте ещё раз через несколько секунд",
            ]
            return {
                "form": form,
                "ldap_enabled": config.get("auth_ldap_enabled", False),
            }

        await request.app[REDIS_KEY].set(lock_key, "1", ex=LOGIN_CHECK_LOCK_TTL_SECONDS)

        result = await request["db"].execute(
            sa.select(User).where(User.email == normalized_email)
        )
        user = result.scalar()
        if not user:
            await asyncio.sleep(LOGIN_FAILURE_DELAY_SECONDS)
            email_field: Any = form.email
            email_field.errors = [*form.email.errors, "Неверный email или пароль"]
            return {
                "form": form,
                "ldap_enabled": config.get("auth_ldap_enabled", False),
            }
        if user.is_active is False:
            email_field: Any = form.email
            email_field.errors = [
                *form.email.errors,
                "Вы не подтвердили email. Проверьте почту и папку Спам, "
                "затем попробуйте снова.",
            ]
            return {
                "form": form,
                "ldap_enabled": config.get("auth_ldap_enabled", False),
            }
        if user.is_ldap:
            email_field: Any = form.email
            email_field.errors = [
                *form.email.errors,
                "Для этой учётной записи используется LDAP-аутентификация",
            ]
            return {
                "form": form,
                "ldap_enabled": config.get("auth_ldap_enabled", False),
            }
        if not user.password or not password_context.verify(
            form.password.data, user.password
        ):
            await asyncio.sleep(LOGIN_FAILURE_DELAY_SECONDS)
            email_field: Any = form.email
            email_field.errors = [*form.email.errors, "Неверный email или пароль"]
            return {
                "form": form,
                "ldap_enabled": config.get("auth_ldap_enabled", False),
            }

        # Warning: always use new_session() instead of get_session() in login views
        # to guard against Session Fixation attacks!
        session = await new_session(request)
        session["user_id"] = user.id
        session["login_at"] = int(time.time())
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
        raise web.HTTPFound(location=request.app.router[target].url_for())

    return {"form": form, "ldap_enabled": config.get("auth_ldap_enabled", False)}


@meta(title="LDAP-вход в vchat")
@aiohttp_jinja2.template("auth/login_ldap.html")
async def login_ldap(request):
    config = request.app[CONFIG_KEY]
    if not config.get("auth_ldap_enabled", False):
        raise web.HTTPFound(location=request.app.router["login"].url_for())

    session = await get_session(request)
    data = await request.post()
    form = LoginForm(data, meta={"csrf_context": session})

    if request.method == "POST" and form.validate():
        normalized_email = form.email.data.strip().lower()
        lock_key = f"auth:login_check_lock:{normalized_email}"

        if await request.app[REDIS_KEY].exists(lock_key):
            email_field: Any = form.email
            email_field.errors = [
                *form.email.errors,
                "Слишком много попыток входа. Попробуйте ещё раз через несколько секунд",
            ]
            return {
                "form": form,
                "basic_enabled": config.get("auth_basic_enabled", True),
            }

        await request.app[REDIS_KEY].set(lock_key, "1", ex=LOGIN_CHECK_LOCK_TTL_SECONDS)

        ldap_result = await authenticate_ldap(
            normalized_email, form.password.data, config
        )
        if ldap_result is None:
            await asyncio.sleep(LOGIN_FAILURE_DELAY_SECONDS)
            email_field: Any = form.email
            email_field.errors = [*form.email.errors, "Неверный email или пароль"]
            return {
                "form": form,
                "basic_enabled": config.get("auth_basic_enabled", True),
            }

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
            email_field: Any = form.email
            email_field.errors = [*form.email.errors, "Пользователь заблокирован"]
            return {
                "form": form,
                "basic_enabled": config.get("auth_basic_enabled", True),
            }
        elif not user.is_ldap:
            email_field: Any = form.email
            email_field.errors = [
                *form.email.errors,
                "Для этой учётной записи используется локальная аутентификация",
            ]
            return {
                "form": form,
                "basic_enabled": config.get("auth_basic_enabled", True),
            }

        # Warning: always use new_session() instead of get_session() in login views
        # to guard against Session Fixation attacks!
        session = await new_session(request)
        session["user_id"] = user.id
        session["login_at"] = int(time.time())
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
        raise web.HTTPFound(location=request.app.router[target].url_for())

    return {"form": form, "basic_enabled": config.get("auth_basic_enabled", True)}


@meta(title="Выход из vchat")
@login_required()
async def logout(request):
    await admin_event("user_logout", request)
    session = await get_session(request)
    session.invalidate()
    raise web.HTTPFound(location=request.app.router["login"].url_for())
