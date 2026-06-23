from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import pytest
import sqlalchemy as sa
from aiohttp import web
from aiohttp_session import Session
from aiohttp_session.cookie_storage import EncryptedCookieStorage
from sqlalchemy.dialects.postgresql import insert

from jobs.db import create_sync_engine
from vchat import app as app_mod
from vchat.models import User
from vchat.settings import config


EXCLUDED_ROUTE_NAMES = {
    "api_update",
    "actions",
    "chat_actions",
    "chat_ws",
    "health_live",
    "health_ready",
    "login",
    "login_ldap",
    "logout",
    "metrics",
    "notify_ws",
    "static",
    "widget",
    "widget_triggers_resolve",
}

EXCLUDED_PATHS = {
    "/api-docs",
    "/api-docs/",
    "/api-docs/swagger.json",
    "/check",
    "/favicon.ico",
    "/robots.txt",
}

ROUTE_PARAMS = {
    "project_source_settings": ("source_id",),
    "source_sitemaps": ("source_id",),
    "project_document_detail": ("document_id",),
    "project_document_content": ("document_id",),
    "project_document_content_rest": ("document_id",),
    "file_document": ("document_id",),
    "project_chat_with_id": ("chat_id",),
    "project_history_detail": ("chat_id",),
    "public_widget_chat": ("code",),
    "project_widget_edit": ("widget_id",),
}

NO_FOLLOW_REDIRECTS = {
    "project_chat",
}

SMOKE_CHAT_USER_UID = "site-smoke-test"

ROUTE_QUERY_STRINGS = {
    "project_chat": f"?user_uid={SMOKE_CHAT_USER_UID}",
}


class _FakeRedis:
    async def lrange(self, *_args: Any) -> list[bytes]:
        return []

    async def delete(self, *_args: Any) -> None:
        return None

    async def get(self, *_args: Any) -> None:
        return None

    async def set(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def setex(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def exists(self, *_args: Any) -> int:
        return 0

    async def aclose(self) -> None:
        return None


@dataclass(frozen=True)
class _SmokeFixtures:
    user_id: int
    source_id: int | None
    document_id: int | None
    chat_id: str | None
    widget_id: int | None
    code: str | None

    def route_values(self) -> dict[str, str | None]:
        return {
            "source_id": str(self.source_id) if self.source_id is not None else None,
            "document_id": str(self.document_id) if self.document_id is not None else None,
            "chat_id": self.chat_id,
            "widget_id": str(self.widget_id) if self.widget_id is not None else None,
            "code": self.code,
        }


def _upsert_smoke_user() -> int:
    engine = create_sync_engine()
    with engine.begin() as conn:
        stmt = insert(User).values(
            email="site-smoke@example.test",
            name="Site Smoke",
            password=None,
            is_active=True,
            is_ldap=False,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[User.email],
            set_={
                "name": stmt.excluded.name,
                "is_active": True,
            },
        )
        conn.execute(stmt)
        return conn.execute(
            sa.text("SELECT id FROM users WHERE email = :email"),
            {"email": "site-smoke@example.test"},
        ).scalar_one()


def _load_smoke_fixtures() -> _SmokeFixtures:
    user_id = _upsert_smoke_user()
    engine = create_sync_engine()
    with engine.connect() as conn:
        source_id = conn.execute(
            sa.text("SELECT id FROM source ORDER BY id LIMIT 1")
        ).scalar_one_or_none()
        document_id = conn.execute(
            sa.text(
                "SELECT id FROM page "
                "WHERE coalesce(status_error, '') != 'duplicate_content' "
                "ORDER BY id LIMIT 1"
            )
        ).scalar_one_or_none()
        chat_id = conn.execute(
            sa.text(
                "SELECT id FROM chat "
                "WHERE NOT (meta ? 'widget_code') "
                "ORDER BY created_at DESC NULLS LAST LIMIT 1"
            )
        ).scalar_one_or_none()
        widget_row = conn.execute(
            sa.text("SELECT id, code FROM widget_integration ORDER BY id LIMIT 1")
        ).first()

    return _SmokeFixtures(
        user_id=user_id,
        source_id=source_id,
        document_id=document_id,
        chat_id=chat_id,
        widget_id=widget_row.id if widget_row else None,
        code=widget_row.code if widget_row else None,
    )


def _delete_smoke_chats() -> None:
    engine = create_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            sa.text("DELETE FROM chat WHERE user_uid = :user_uid"),
            {"user_uid": SMOKE_CHAT_USER_UID},
        )


def _logged_in_cookie(user_id: int) -> str:
    storage = EncryptedCookieStorage(
        config["cookie_key"],
        cookie_name=config["cookie_name"],
        domain=None,
        secure=False,
        max_age=int(config["session_max_age_seconds"]),
        path="/",
    )
    session = Session(None, data=None, new=True, max_age=storage.max_age)
    session["user_id"] = user_id
    session["login_at"] = int(time.time())
    response = web.Response()
    storage.save_cookie(
        response,
        storage._fernet.encrypt(  # noqa: SLF001
            storage._encoder(  # noqa: SLF001
                {"created": session.created, "session": dict(session)}
            ).encode("utf-8")
        ).decode("utf-8"),
        max_age=session.max_age,
    )
    return response.cookies[config["cookie_name"]].value


def _site_page_urls(app: web.Application, fixtures: _SmokeFixtures) -> list[tuple[str, str]]:
    values = fixtures.route_values()
    urls: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for resource in app.router.resources():
        name = getattr(resource, "name", None)
        info = resource.get_info()
        path = info.get("path")
        if name in EXCLUDED_ROUTE_NAMES or path in EXCLUDED_PATHS:
            continue
        if name is None and (path is None or path.startswith("/api-docs")):
            continue
        if info.get("prefix") or info.get("directory"):
            continue

        for route in resource:
            if route.method not in {"GET", "*"}:
                continue
            if name is None:
                if path is None:
                    continue
                url = path
                route_label = path
            else:
                route_params: dict[str, str] = {}
                skip_route = False
                for key in ROUTE_PARAMS.get(name, ()):
                    value = values[key]
                    if value is None:
                        skip_route = True
                        break
                    route_params[key] = value
                if skip_route:
                    continue
                url = str(app.router[name].url_for(**route_params))
                url += ROUTE_QUERY_STRINGS.get(name, "")
                route_label = name

            item = (route_label, url)
            if item not in seen:
                seen.add(item)
                urls.append(item)

    return urls


@pytest.mark.asyncio
async def test_logged_in_site_pages_do_not_return_500(
    aiohttp_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_mod, "validate_multiprocess_setup", lambda: None)
    monkeypatch.setattr(app_mod, "redis_from_url", lambda _url: _FakeRedis())
    monkeypatch.setitem(config, "enable_https_middleware", False)
    monkeypatch.setitem(config, "cookie_domain", None)
    monkeypatch.setitem(config, "cookie_secure", False)

    fixtures = _load_smoke_fixtures()
    _delete_smoke_chats()
    app = await app_mod.create_app()
    app.on_startup.clear()
    client = await aiohttp_client(app)
    client.session.cookie_jar.update_cookies(
        {config["cookie_name"]: _logged_in_cookie(fixtures.user_id)},
        response_url=client.make_url("/"),
    )

    checked: list[str] = []
    failures: list[str] = []
    try:
        for route_name, url in _site_page_urls(app, fixtures):
            response = await client.get(
                url,
                allow_redirects=route_name not in NO_FOLLOW_REDIRECTS,
            )
            body = await response.text()
            checked.append(f"{route_name} {url} -> {response.status}")
            if response.status >= 500:
                failures.append(f"{route_name} {url} -> {response.status}\n{body[:1000]}")
    finally:
        _delete_smoke_chats()

    assert failures == [], "\n\n".join(failures)
    assert checked
