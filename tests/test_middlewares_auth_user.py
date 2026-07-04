from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import logging

import pytest
from aiohttp import web
from multidict import MultiDict
from yarl import URL

from vchat.settings import REDIS_KEY, SIGNER_KEY
import vchat.middlewares as mdw
from vchat.views.admin import forms as admin_forms
from vchat.views.auth import forms as auth_forms
from vchat.views.auth import views as auth_views
from vchat.views.user import views as user_views


class _App(dict):
    def __init__(self, *args, router=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.router = router or {}


class _Request(dict):
    def __init__(
        self,
        *,
        path="/",
        method="GET",
        app=None,
        headers=None,
        query=None,
        post_data=None,
    ):
        super().__init__()
        self.path = path
        self.method = method
        self.app = app if app is not None else _App()
        self.headers = headers or {}
        self.remote = "127.0.0.1"
        self.rel_url = SimpleNamespace(query=query or {})
        self._post_data = post_data or {}

    async def post(self):
        return self._post_data


class _Route:
    def __init__(self, value: str):
        self.value = value

    def url_for(self, **kwargs):
        value = self.value.format(**kwargs) if kwargs else self.value
        return URL(value)


class _Signer:
    def loads(self, token, max_age=86400):
        _ = token, max_age
        return 5


@pytest.mark.asyncio
async def test_meta_middleware_and_handle_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _Request()

    async def _handler(req):
        assert "meta" in req
        return web.Response(text="ok")

    response = await mdw.meta_middleware(request, _handler)
    assert response.status == 200

    monkeypatch.setattr(
        mdw.aiohttp_jinja2,
        "render_template",
        lambda tpl, req, ctx, status=200: web.Response(
            text=f"{tpl}:{status}", status=status
        ),
    )
    err_resp = await mdw.handle_error(_Request(), 404)
    assert err_resp.status == 404
    assert "misc/404.html" in err_resp.text


@pytest.mark.asyncio
async def test_error_middleware_http_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _handler(_request):
        raise web.HTTPNotFound()

    async def _handle_error(_request, code=404):
        return web.Response(text=f"e{code}", status=code)

    monkeypatch.setattr(mdw, "handle_error", _handle_error)
    response = await mdw.error_middleware(_Request(method="GET"), _handler)
    assert response.status == 404
    assert response.text == "e404"


@pytest.mark.asyncio
async def test_error_middleware_logs_forbidden(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _handler(_request):
        raise web.HTTPForbidden()

    async def _handle_error(_request, code=403):
        return web.Response(text=f"e{code}", status=code)

    monkeypatch.setattr(mdw, "handle_error", _handle_error)
    request = _Request(path="/admin/secret", method="POST")
    request.rel_url = "/admin/secret"

    with caplog.at_level(logging.WARNING, logger=mdw.logger.name):
        response = await mdw.error_middleware(request, _handler)

    assert response.status == 403
    assert "Forbidden (POST /admin/secret)" in caplog.text


@pytest.mark.asyncio
async def test_debug_access_control_header_middleware_sets_cors_headers() -> None:
    async def _handler(_request):
        return web.Response(text="ok")

    response = await mdw.debug_access_control_header_middleware(_Request(), _handler)

    assert response.text == "ok"
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert "PATCH" in response.headers["Access-Control-Allow-Methods"]
    assert "upload-offset" in response.headers["Access-Control-Expose-Headers"]


@pytest.mark.asyncio
async def test_security_headers_middleware_sets_browser_headers() -> None:
    async def _handler(_request):
        return web.Response(text="ok")

    request = _Request(headers={"X-Forwarded-Proto": "https"})
    request.scheme = "https"
    response = await mdw.security_headers_middleware(
        request,
        _handler,
    )

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"
    assert "camera=()" in response.headers["Permissions-Policy"]
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in response.headers["Content-Security-Policy"]
    assert response.headers["Strict-Transport-Security"].startswith("max-age=")


@pytest.mark.asyncio
async def test_security_headers_middleware_does_not_disable_static_cache() -> None:
    async def _handler(_request):
        return web.Response(text="ok")

    request = _Request(path="/static/js/widget.js")
    request.scheme = "http"
    response = await mdw.security_headers_middleware(request, _handler)

    assert "Cache-Control" not in response.headers
    assert "Pragma" not in response.headers
    assert response.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.asyncio
async def test_security_headers_allow_public_widget_iframe() -> None:
    async def _handler(_request):
        return web.Response(text="ok")

    request = _Request(path="/chat/widget/widget-code")
    request.scheme = "https"
    response = await mdw.security_headers_middleware(request, _handler)

    assert "X-Frame-Options" not in response.headers
    assert "frame-ancestors" not in response.headers["Content-Security-Policy"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.asyncio
async def test_public_widget_cors_allows_trigger_resolution_for_any_origin() -> None:
    async def _handler(_request):
        return web.Response(text="ok")

    request = _Request(
        path="/api/triggers/resolve",
        headers={"Origin": "https://customer.example"},
    )
    response = await mdw.public_widget_cors_middleware(request, _handler)

    assert response.text == "ok"
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert "Access-Control-Allow-Credentials" not in response.headers
    assert response.headers["Vary"] == "Origin"


@pytest.mark.asyncio
async def test_public_widget_cors_handles_trigger_preflight_for_any_origin() -> None:
    async def _handler(_request):
        raise AssertionError("preflight must not reach the route handler")

    request = _Request(
        path="/api/triggers/resolve",
        method="OPTIONS",
        headers={
            "Origin": "https://customer.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    response = await mdw.public_widget_cors_middleware(request, _handler)

    assert response.status == 200
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert response.headers["Access-Control-Allow-Methods"] == "GET, OPTIONS"


def test_local_password_forms_enforce_asvs_length_bounds() -> None:
    legacy_login = auth_forms.Login(
        MultiDict({"email": "user@example.com", "password": "short"}),
        meta={"csrf": False},
    )
    assert legacy_login.validate()

    long_password = "p" * 128
    login = auth_forms.Login(
        MultiDict({"email": "user@example.com", "password": long_password}),
        meta={"csrf": False},
    )
    assert login.validate()

    password_change = auth_forms.PasswordChange(
        MultiDict(
            {
                "current_password": "short",
                "password": "long-enough-password",
                "confirm": "long-enough-password",
            }
        ),
        meta={"csrf": False},
    )
    assert password_change.validate()

    short_new_password = auth_forms.PasswordChange(
        MultiDict(
            {
                "current_password": "short",
                "password": "short",
                "confirm": "short",
            }
        ),
        meta={"csrf": False},
    )
    assert not short_new_password.validate()

    too_long = "p" * 129
    add_form = admin_forms.UserAdd(
        MultiDict({"email": "admin@example.com", "password": too_long}),
        meta={"csrf": False},
    )
    assert not add_form.validate()

    edit_form = admin_forms.UserPasswordEdit(
        MultiDict({"password": long_password, "confirm": long_password}),
        meta={"csrf": False},
    )
    assert edit_form.validate()


@pytest.mark.asyncio
async def test_flash_and_force_https_middlewares(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Redis:
        async def lrange(self, key, _start, _end):
            assert key == "message_1"
            return [b"success|Saved", b"error|Failed"]

        async def delete(self, key):
            assert key == "message_1"

    monkeypatch.setattr(mdw.cfg, "public_url", "https://local.vchat.com")
    app = _App({REDIS_KEY: _Redis()})
    request = _Request(app=app)
    request["user"] = SimpleNamespace(id=1)

    async def _handler(req):
        assert len(req["flash_messages"]) == 2
        return web.Response(headers={"Location": "http://local.vchat.com/login/"})

    response = await mdw.flash_middleware(request, _handler)
    assert response.status == 200

    async def _redirect_handler(_request):
        return web.HTTPFound("http://local.vchat.com/login/")

    forced = await mdw.force_https_location_middleware(request, _redirect_handler)
    assert forced.headers["Location"].startswith("https://")


@pytest.mark.asyncio
async def test_auth_middleware_sets_user(monkeypatch: pytest.MonkeyPatch) -> None:
    statements = []

    class _ExecuteResult:
        def first(self):
            return SimpleNamespace(
                id=7,
                email="u@example.com",
                name="User Seven",
                is_active=True,
                auth_user_session_id=17,
                last_seen_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            )

    class _DB:
        def __init__(self):
            self.commits = 0

        async def execute(self, stmt):
            statements.append(stmt)
            return _ExecuteResult()

        async def commit(self):
            self.commits += 1

        def in_transaction(self):
            return False

    class _Session(dict):
        def invalidate(self):
            self["invalidated"] = True

    async def _get_session(_request):
        return _Session(user_id=7, session_id="session-7", login_at=100)

    monkeypatch.setattr(mdw, "get_session", _get_session)
    monkeypatch.setattr(mdw.time, "time", lambda: 120)

    request = _Request(path="/dashboard")
    db = _DB()
    request["db"] = db

    async def _handler(req):
        return web.Response(text=req["user"].email)

    resp = await mdw.auth_middleware(request, _handler)
    assert resp.text == "u@example.com"
    assert request["auth_user_session_id"] == 17
    compiled = str(statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "users.is_active IS true" in compiled
    assert "user_session.session_id = 'session-7'" in compiled
    assert "user_session.revoked_at IS NULL" in compiled
    touch = str(statements[1].compile(compile_kwargs={"literal_binds": True}))
    assert "UPDATE user_session SET" in touch
    assert "last_seen_at=" in touch
    assert "user_session.id = 17" in touch
    assert db.commits == 1


@pytest.mark.asyncio
async def test_auth_middleware_invalidates_idle_user_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements = []

    class _ExecuteResult:
        def first(self):
            return SimpleNamespace(
                id=7,
                email="u@example.com",
                name="User Seven",
                is_active=True,
                auth_user_session_id=17,
                last_seen_at=datetime.now(timezone.utc) - timedelta(hours=5),
            )

    class _DB:
        def __init__(self):
            self.commits = 0

        async def execute(self, stmt):
            statements.append(stmt)
            return _ExecuteResult()

        async def commit(self):
            self.commits += 1

        def in_transaction(self):
            return False

    class _Session(dict):
        def invalidate(self):
            self["invalidated"] = True

    session = _Session(user_id=7, session_id="session-7", login_at=100)

    async def _get_session(_request):
        return session

    monkeypatch.setattr(mdw, "get_session", _get_session)
    monkeypatch.setattr(mdw.time, "time", lambda: 120)
    monkeypatch.setattr(mdw.cfg, "auth_session_idle_timeout_seconds", 4 * 60 * 60)

    request = _Request(path="/dashboard")
    db = _DB()
    request["db"] = db

    async def _handler(req):
        assert req["user"] is None
        return web.Response(text="anonymous")

    resp = await mdw.auth_middleware(request, _handler)
    assert resp.text == "anonymous"
    assert session["invalidated"] is True
    revoke = str(statements[1].compile(compile_kwargs={"literal_binds": True}))
    assert "UPDATE user_session SET" in revoke
    assert "revoked_reason='idle_timeout'" in revoke
    assert "user_session.id = 17" in revoke
    assert db.commits == 1


@pytest.mark.asyncio
async def test_auth_middleware_invalidates_expired_auth_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _DB:
        async def execute(self, stmt):
            _ = stmt
            raise AssertionError("expired sessions must not query users")

    class _Session(dict):
        def invalidate(self):
            self["invalidated"] = True

    session = _Session(user_id=7, session_id="session-7", login_at=100)

    async def _get_session(_request):
        return session

    monkeypatch.setattr(mdw, "get_session", _get_session)
    monkeypatch.setattr(mdw.time, "time", lambda: 161)
    monkeypatch.setattr(mdw.cfg, "auth_session_time", 60)

    request = _Request(path="/dashboard")
    request["db"] = _DB()

    async def _handler(req):
        assert req["user"] is None
        return web.Response(text="anonymous")

    resp = await mdw.auth_middleware(request, _handler)
    assert resp.text == "anonymous"
    assert session["invalidated"] is True


@pytest.mark.asyncio
async def test_auth_middleware_invalidates_missing_login_timestamp_when_ttl_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _DB:
        async def execute(self, stmt):
            _ = stmt
            raise AssertionError("expired sessions must not query users")

    class _Session(dict):
        def invalidate(self):
            self["invalidated"] = True

    session = _Session(user_id=7)

    async def _get_session(_request):
        return session

    monkeypatch.setattr(mdw, "get_session", _get_session)
    monkeypatch.setattr(mdw.cfg, "auth_session_time", 60)

    request = _Request(path="/dashboard")
    request["db"] = _DB()

    async def _handler(req):
        assert req["user"] is None
        return web.Response(text="anonymous")

    resp = await mdw.auth_middleware(request, _handler)
    assert resp.text == "anonymous"
    assert session["invalidated"] is True


@pytest.mark.asyncio
async def test_auth_middleware_invalidates_inactive_user_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements = []

    class _ExecuteResult:
        def first(self):
            return None

    class _DB:
        async def execute(self, stmt):
            statements.append(stmt)
            return _ExecuteResult()

        def in_transaction(self):
            return False

    class _Session(dict):
        def invalidate(self):
            self["invalidated"] = True

    session = _Session(user_id=7, session_id="session-7", login_at=100)

    async def _get_session(_request):
        return session

    monkeypatch.setattr(mdw, "get_session", _get_session)
    monkeypatch.setattr(mdw.time, "time", lambda: 120)

    request = _Request(path="/dashboard")
    request["db"] = _DB()

    async def _handler(req):
        assert req["user"] is None
        return web.Response(text="anonymous")

    resp = await mdw.auth_middleware(request, _handler)
    assert resp.text == "anonymous"
    assert session["invalidated"] is True
    compiled = str(statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "users.is_active IS true" in compiled
    assert "user_session.session_id = 'session-7'" in compiled


@pytest.mark.asyncio
async def test_login_and_logout(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Field:
        def __init__(self, data):
            self.data = data
            self.errors = []

    class _Form:
        def __init__(self, data, meta):
            _ = meta
            self.email = _Field(data.get("email", "user@example.com"))
            self.password = _Field(data.get("password", "pass"))
            self.csrf_token = _Field("csrf")

        def validate(self):
            return True

        def add_email_error(self, message):
            self.email.errors.append(message)

    class _Record:
        def scalar(self):
            return SimpleNamespace(
                id=5,
                is_active=True,
                password="hash",
                email="user@example.com",
                name="User",
                is_ldap=False,
            )

    class _DB:
        def __init__(self):
            self.added = []
            self.commits = 0
            self.executed = []

        async def execute(self, stmt):
            self.executed.append(stmt)
            return _Record()

        def add(self, obj):
            self.added.append(obj)

        async def flush(self):
            pass

        async def commit(self):
            self.commits += 1

    class _Redis:
        def __init__(self):
            self.expired = []

        async def exists(self, _key):
            return 0

        async def set(self, *args, **kwargs):
            _ = args, kwargs
            return True

        async def incr(self, key):
            assert key == "auth:login_failures:user@example.com"
            return 1

        async def expire(self, key, ttl):
            self.expired.append((key, ttl))
            return True

        async def delete(self, _key):
            return 1

    login_router = {"index": _Route("/"), "login": _Route("/login/")}
    request = _Request(
        method="POST",
        app=_App({REDIS_KEY: _Redis()}, router=login_router),
        post_data={"email": "user@example.com", "password": "pass"},
        query={},
    )
    db = _DB()
    request["db"] = db

    async def _get_session(_request):
        return {}

    events = []

    async def _admin_event(event, req):
        _ = req
        events.append(event)

    created_session = {}

    async def _new_session(_request):
        return created_session

    async def _admin_event(name, req):
        _ = name, req

    monkeypatch.setattr(auth_views.forms, "Login", _Form)
    monkeypatch.setattr(auth_views, "get_session", _get_session)
    monkeypatch.setattr(auth_views, "new_session", _new_session)
    monkeypatch.setattr(auth_views.password_context, "verify", lambda raw, hashed: True)
    monkeypatch.setattr(auth_views, "admin_event", _admin_event)

    # strip decorators (meta + template)
    login_fn = auth_views.login.__wrapped__.__wrapped__
    with pytest.raises(web.HTTPFound) as exc:
        await login_fn(request)
    assert str(exc.value.location) == "/"
    assert request["user"].id == 5
    assert created_session["user_id"] == 5
    assert created_session["session_id"] == db.added[0].session_id
    assert isinstance(created_session["login_at"], int)

    created_session.clear()
    request_next = _Request(
        method="POST",
        app=_App({REDIS_KEY: _Redis()}, router=login_router),
        post_data={"email": "user@example.com", "password": "pass"},
        query={"next": "/sessions/"},
    )
    request_next["db"] = db
    with pytest.raises(web.HTTPFound) as exc:
        await login_fn(request_next)
    assert str(exc.value.location) == "/sessions/"

    class _LogoutSession(dict):
        def invalidate(self):
            self["done"] = True

    async def _logout_session(_request):
        return _LogoutSession(user_id=5, session_id="session-5")

    monkeypatch.setattr(auth_views, "get_session", _logout_session)
    logout_fn = auth_views.logout.__wrapped__.__wrapped__
    request2 = _Request(
        method="GET", app=_App(router={"login": _Route("/login/")})
    )
    request2["user"] = SimpleNamespace(id=5)
    request2["db"] = db
    with pytest.raises(web.HTTPFound) as exc:
        await logout_fn(request2)
    assert str(exc.value.location) == "/login/"
    assert exc.value.headers["Clear-Site-Data"] == '"cache", "storage"'
    assert db.commits == 3


@pytest.mark.asyncio
async def test_sessions_action_revoke_other_requires_signed_csrf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Session(dict):
        def invalidate(self):
            self["invalidated"] = True

    class _DB:
        async def execute(self, stmt):
            _ = stmt
            raise AssertionError("missing csrf must stop before mutation")

    session = _Session(user_id=5, session_id="current-session")

    async def _get_session(_request):
        return session

    monkeypatch.setattr(auth_views, "get_session", _get_session)

    request = _Request(
        method="POST",
        app=_App(
            {SIGNER_KEY: _Signer()},
            router={"sessions": _Route("/sessions/"), "login": _Route("/login/")},
        ),
        post_data={"action": "revoke_other"},
    )
    request["db"] = _DB()
    request["user"] = SimpleNamespace(id=5, is_active=True)

    with pytest.raises(web.HTTPForbidden):
        await auth_views.sessions_action.__wrapped__.__wrapped__(request)


@pytest.mark.asyncio
async def test_sessions_action_revoke_other_keeps_current_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements = []

    class _Session(dict):
        def invalidate(self):
            self["invalidated"] = True

    class _DB:
        def __init__(self):
            self.commits = 0

        async def execute(self, stmt):
            statements.append(stmt)
            return SimpleNamespace(rowcount=2)

        async def commit(self):
            self.commits += 1

    session = _Session(user_id=5, session_id="current-session")

    async def _get_session(_request):
        return session

    events = []

    async def _admin_event(event, req):
        _ = req
        events.append(event)

    monkeypatch.setattr(auth_views, "get_session", _get_session)
    monkeypatch.setattr(auth_views, "admin_event", _admin_event)

    db = _DB()
    request = _Request(
        method="POST",
        app=_App(
            {SIGNER_KEY: _Signer()},
            router={"sessions": _Route("/sessions/"), "login": _Route("/login/")},
        ),
        post_data={"action": "revoke_other", "csrf_token": "ok"},
    )
    request["db"] = db
    request["user"] = SimpleNamespace(id=5, is_active=True)

    with pytest.raises(web.HTTPFound) as exc:
        await auth_views.sessions_action.__wrapped__.__wrapped__(request)

    assert str(exc.value.location) == "/sessions/"
    assert db.commits == 1
    assert events == ["user_session_revoke_other"]
    compiled = str(statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "user_session.user_id = 5" in compiled
    assert "user_session.session_id != 'current-session'" in compiled


@pytest.mark.asyncio
async def test_login_ldap_rejects_inactive_existing_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Field:
        def __init__(self, data):
            self.data = data
            self.errors = []

    class _Form:
        def __init__(self, data, meta):
            _ = meta
            self.email = _Field(data.get("email", "ldap@example.com"))
            self.password = _Field(data.get("password", "pass"))

        def validate(self):
            return True

        def add_email_error(self, message):
            self.email.errors.append(message)

    class _Record:
        def scalar(self):
            return SimpleNamespace(
                id=7,
                email="ldap@example.com",
                name="LDAP User",
                is_active=False,
                is_ldap=True,
            )

    class _DB:
        async def execute(self, stmt):
            _ = stmt
            return _Record()

    class _Redis:
        def __init__(self):
            self.expired = []

        async def exists(self, _key):
            return 0

        async def set(self, *args, **kwargs):
            _ = args, kwargs
            return True

        async def incr(self, key):
            assert key == "auth:login_failures:user@example.com"
            return 1

        async def expire(self, key, ttl):
            self.expired.append((key, ttl))
            return True

    async def _get_session(_request):
        return {}

    async def _authenticate(email, password):
        _ = email, password
        return {"email": "ldap@example.com", "name": "LDAP User"}

    async def _new_session(_request):
        raise AssertionError("inactive LDAP user must not receive a session")

    request = _Request(
        method="POST",
        app=_App(
            {REDIS_KEY: _Redis()},
            router={"login": _Route("/login/")},
        ),
        post_data={"email": "ldap@example.com", "password": "pass"},
    )
    request["db"] = _DB()

    monkeypatch.setattr(auth_views.forms, "Login", _Form)
    monkeypatch.setattr(auth_views, "get_session", _get_session)
    monkeypatch.setattr(auth_views, "authenticate_ldap", _authenticate)
    monkeypatch.setattr(auth_views, "new_session", _new_session)
    monkeypatch.setattr(auth_views.cfg, "auth_ldap_enabled", True)

    payload = await auth_views.login_ldap.__wrapped__.__wrapped__(request)

    assert isinstance(payload, dict)
    assert "Пользователь заблокирован" in payload["form"].email.errors


@pytest.mark.asyncio
async def test_login_ldap_rejects_existing_local_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Field:
        def __init__(self, data):
            self.data = data
            self.errors = []

    class _Form:
        def __init__(self, data, meta):
            _ = meta
            self.email = _Field(data.get("email", "local@example.com"))
            self.password = _Field(data.get("password", "pass"))

        def validate(self):
            return True

        def add_email_error(self, message):
            self.email.errors.append(message)

    class _Record:
        def scalar(self):
            return SimpleNamespace(
                id=8,
                email="local@example.com",
                name="Local User",
                is_active=True,
                is_ldap=False,
            )

    class _DB:
        async def execute(self, stmt):
            _ = stmt
            return _Record()

    class _Redis:
        async def exists(self, _key):
            return 0

        async def set(self, *args, **kwargs):
            _ = args, kwargs
            return True

    async def _get_session(_request):
        return {}

    async def _authenticate(email, password):
        _ = email, password
        return {"email": "local@example.com", "name": "LDAP User"}

    async def _new_session(_request):
        raise AssertionError("local user must not receive an LDAP session")

    request = _Request(
        method="POST",
        app=_App(
            {REDIS_KEY: _Redis()},
            router={"login": _Route("/login/")},
        ),
        post_data={"email": "local@example.com", "password": "pass"},
    )
    request["db"] = _DB()

    monkeypatch.setattr(auth_views.forms, "Login", _Form)
    monkeypatch.setattr(auth_views, "get_session", _get_session)
    monkeypatch.setattr(auth_views, "authenticate_ldap", _authenticate)
    monkeypatch.setattr(auth_views.cfg, "auth_ldap_enabled", True)
    monkeypatch.setattr(auth_views, "new_session", _new_session)

    payload = await auth_views.login_ldap.__wrapped__.__wrapped__(request)

    assert isinstance(payload, dict)
    assert (
        "Для этой учётной записи используется локальная аутентификация"
        in payload["form"].email.errors
    )


@pytest.mark.asyncio
async def test_authenticate_ldap_escapes_search_filter_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    class _Entry:
        dn = "uid=user,ou=people,dc=example,dc=com"

        def get(self, attr, default):
            assert attr == "displayName"
            _ = default
            return ["LDAP User"]

    class _Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def search(self, *, base, scope, filter_exp, attrlist):
            calls.append(("search_base", base))
            calls.append(("search_scope", scope))
            calls.append(("filter_exp", filter_exp))
            calls.append(("attrlist", attrlist))
            return [_Entry()]

    class _LDAPClient:
        def __init__(self, server, tls=False):
            calls.append(("client", (server, tls)))

        def set_credentials(self, method, *, user, password):
            calls.append(("credentials", (method, user, password)))

        def connect(self, *, is_async):
            assert is_async is True
            return _Connection()

    monkeypatch.setattr(auth_views.bonsai, "LDAPClient", _LDAPClient)
    monkeypatch.setattr(auth_views.cfg, "ldap_server", "ldap://ldap.example.com:389")
    monkeypatch.setattr(auth_views.cfg, "ldap_use_ssl", False)
    monkeypatch.setattr(auth_views.cfg, "ldap_bind_dn", "cn=service,dc=example,dc=com")
    monkeypatch.setattr(auth_views.cfg, "ldap_bind_password", "service-secret")
    monkeypatch.setattr(
        auth_views.cfg, "ldap_search_base", "ou=people,dc=example,dc=com"
    )
    monkeypatch.setattr(
        auth_views.cfg, "ldap_search_filter", "(&(mail={email})(memberOf=cn=vchat))"
    )
    monkeypatch.setattr(auth_views.cfg, "ldap_attr_name", "displayName")

    result = await auth_views.authenticate_ldap(
        "user*)(mail=*)@example.com",
        "secret",
    )

    assert result == {
        "email": "user*)(mail=*)@example.com",
        "name": "LDAP User",
    }
    escaped_filter = (
        r"(&(mail=user\2A\29\28mail=\2A\29@example.com)(memberOf=cn=vchat))"
    )
    assert ("filter_exp", escaped_filter) in calls
    assert (
        "credentials",
        ("SIMPLE", "uid=user,ou=people,dc=example,dc=com", "secret"),
    ) in calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("member_of", "expected"),
    [
        (["CN=VChat Users, OU=Groups, DC=example, DC=com"], True),
        (["CN=Other,OU=Groups,DC=example,DC=com"], False),
    ],
)
async def test_authenticate_ldap_requires_configured_group(
    monkeypatch: pytest.MonkeyPatch,
    member_of: list[str],
    expected: bool,
) -> None:
    calls: list[tuple[str, object]] = []

    class _Entry:
        dn = "uid=user,ou=people,dc=example,dc=com"

        def get(self, attr, default):
            attrs = {
                "displayName": ["LDAP User"],
                "memberOf": member_of,
            }
            return attrs.get(attr, default)

    class _Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def search(self, *, base, scope, filter_exp, attrlist):
            _ = base, scope, filter_exp
            calls.append(("attrlist", attrlist))
            return [_Entry()]

    class _LDAPClient:
        def __init__(self, server, tls=False):
            _ = server, tls

        def set_credentials(self, method, *, user, password):
            calls.append(("credentials", (method, user, password)))

        def connect(self, *, is_async):
            assert is_async is True
            return _Connection()

    monkeypatch.setattr(auth_views.bonsai, "LDAPClient", _LDAPClient)
    monkeypatch.setattr(auth_views.cfg, "ldap_server", "ldap://ldap.example.com:389")
    monkeypatch.setattr(auth_views.cfg, "ldap_use_ssl", False)
    monkeypatch.setattr(auth_views.cfg, "ldap_bind_dn", "cn=service,dc=example,dc=com")
    monkeypatch.setattr(auth_views.cfg, "ldap_bind_password", "service-secret")
    monkeypatch.setattr(
        auth_views.cfg, "ldap_search_base", "ou=people,dc=example,dc=com"
    )
    monkeypatch.setattr(auth_views.cfg, "ldap_search_filter", "(mail={email})")
    monkeypatch.setattr(auth_views.cfg, "ldap_attr_name", "displayName")
    monkeypatch.setattr(
        auth_views.cfg,
        "ldap_required_group_dn",
        "cn=vchat users,ou=groups,dc=example,dc=com",
    )
    monkeypatch.setattr(auth_views.cfg, "ldap_member_of_attr", "memberOf")

    result = await auth_views.authenticate_ldap(
        "user@example.com",
        "user-secret",
    )

    assert ("attrlist", ["displayName", "memberOf"]) in calls
    if expected:
        assert result == {"email": "user@example.com", "name": "LDAP User"}
        assert (
            "credentials",
            ("SIMPLE", "uid=user,ou=people,dc=example,dc=com", "user-secret"),
        ) in calls
    else:
        assert result is None
        assert not any(call[1][-1] == "user-secret" for call in calls)


def test_get_middlewares_uses_configured_session_max_age(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _Storage:
        def __init__(self, *args, **kwargs):
            _ = args
            captured.update(kwargs)

    def _session_middleware(storage):
        captured["storage"] = storage

        async def _middleware(request, handler):
            return await handler(request)

        return _middleware

    monkeypatch.setattr(mdw, "EncryptedCookieStorage", _Storage)
    monkeypatch.setattr(mdw, "session_middleware", _session_middleware)

    middlewares = mdw.get_middlewares(
        mdw.cfg.model_copy(
            update={
            "allowed_origins": ["https://local.vchat.com"],
            "public_url": "https://local.vchat.com",
            "cookie_key": "cookie-key",
            "cookie_name": "USER",
            "cookie_domain": ".vchat.com",
            "cookie_secure": True,
            "session_max_age_seconds": 7200,
            "enable_https_middleware": False,
            }
        )
    )

    assert middlewares
    assert captured["max_age"] == 7200
    assert captured["httponly"] is True
    assert captured["samesite"] == "Lax"


def test_get_middlewares_uses_configured_cors_origins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _cors_middleware(**kwargs):
        captured.update(kwargs)

        async def _middleware(request, handler):
            return await handler(request)

        return _middleware

    class _Storage:
        def __init__(self, *ignored_args, **ignored_kwargs):
            pass

    def _session_middleware(_ignored_storage):
        async def _middleware(request, handler):
            return await handler(request)

        return _middleware

    monkeypatch.setattr(mdw, "cors_middleware", _cors_middleware)
    monkeypatch.setattr(mdw, "EncryptedCookieStorage", _Storage)
    monkeypatch.setattr(mdw, "session_middleware", _session_middleware)

    mdw.get_middlewares(
        mdw.cfg.model_copy(
            update={
            "allowed_origins": ["https://local.vchat.com"],
            "public_url": "https://local.vchat.com",
            "cookie_key": "cookie-key",
            "cookie_name": "USER",
            "cookie_domain": ".vchat.com",
            "cookie_secure": True,
            "session_max_age_seconds": 7200,
            "enable_https_middleware": False,
            }
        )
    )

    assert captured["allow_all"] is False
    assert captured["origins"] == ("https://local.vchat.com",)


@pytest.mark.asyncio
async def test_notify_ws_sends_pending_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder = {}

    class _Redis:
        async def lrange(self, key, _start, _end):
            assert key == "flash_toast_42"
            return [b'{"type":"flash","body":"ok"}']

        async def delete(self, key):
            assert key == "flash_toast_42"

    class _FakeWS:
        def __init__(self):
            self.sent = []
            holder["ws"] = self

        async def prepare(self, request):
            _ = request
            return self

        async def send_str(self, payload):
            self.sent.append(payload)

        async def close(self):
            return None

        def __aiter__(self):
            async def _gen():
                yield SimpleNamespace(type=web.WSMsgType.ERROR)

            return _gen()

    async def _forward(ws, request):
        _ = ws, request

    monkeypatch.setattr(user_views.web, "WebSocketResponse", _FakeWS)
    monkeypatch.setattr(user_views, "_forward_notifications", _forward)

    request = _Request(app=_App({REDIS_KEY: _Redis()}))
    request["user"] = SimpleNamespace(id=42)
    # bypass @login_required wrapper
    notify_fn = user_views.notify_ws.__wrapped__
    await notify_fn(request)
    assert holder["ws"].sent == ['{"type":"flash","body":"ok"}']


@pytest.mark.asyncio
async def test_notify_ws_ignores_pending_redis_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder = {}

    class _Redis:
        async def lrange(self, key, _start, _end):
            assert key == "flash_toast_42"
            raise user_views.RedisError("down")

        async def delete(self, key):
            raise AssertionError(f"unexpected delete for {key}")

    class _FakeWS:
        def __init__(self):
            self.sent = []
            holder["ws"] = self

        async def prepare(self, request):
            _ = request
            return self

        async def send_str(self, payload):
            self.sent.append(payload)

        async def close(self):
            return None

        def __aiter__(self):
            async def _gen():
                yield SimpleNamespace(type=web.WSMsgType.ERROR)

            return _gen()

    async def _forward(ws, request):
        _ = ws, request

    monkeypatch.setattr(user_views.web, "WebSocketResponse", _FakeWS)
    monkeypatch.setattr(user_views, "_forward_notifications", _forward)

    request = _Request(app=_App({REDIS_KEY: _Redis()}))
    request["user"] = SimpleNamespace(id=42)
    notify_fn = user_views.notify_ws.__wrapped__
    await notify_fn(request)
    assert holder["ws"].sent == []


@pytest.mark.asyncio
async def test_login_wrong_password_adds_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Field:
        def __init__(self, data):
            self.data = data
            self.errors = []

    class _Form:
        def __init__(self, data, meta):
            _ = meta
            self.email = _Field(data.get("email", "user@example.com"))
            self.password = _Field(data.get("password", "wrong"))
            self.csrf_token = _Field("csrf")

        def validate(self):
            return True

        def add_email_error(self, message):
            self.email.errors.append(message)

    class _Record:
        def scalar(self):
            return SimpleNamespace(
                id=5,
                is_active=True,
                password="hash",
                email="user@example.com",
                is_ldap=False,
            )

    class _DB:
        async def execute(self, stmt):
            _ = stmt
            return _Record()

    class _Redis:
        async def exists(self, _key):
            return 0

        async def set(self, *args, **kwargs):
            _ = args, kwargs
            return True

        async def incr(self, key):
            assert key == "auth:login_failures:user@example.com"
            return 1

        async def expire(self, key, ttl):
            _ = key, ttl
            return True

    delays = []

    async def _sleep(seconds):
        delays.append(seconds)

    async def _get_session(_request):
        return {}

    events = []

    async def _admin_event(event, req):
        _ = req
        events.append(event)

    request = _Request(
        method="POST",
        app=_App(
            {REDIS_KEY: _Redis()},
            router={"index": _Route("/"), "login": _Route("/login/")},
        ),
        post_data={"email": "user@example.com", "password": "wrong"},
    )
    request["db"] = _DB()

    monkeypatch.setattr(auth_views.forms, "Login", _Form)
    monkeypatch.setattr(auth_views, "get_session", _get_session)
    monkeypatch.setattr(auth_views, "admin_event", _admin_event)
    monkeypatch.setattr(auth_views.asyncio, "sleep", _sleep)
    monkeypatch.setattr(
        auth_views.password_context, "verify", lambda raw, hashed: False
    )

    login_fn = auth_views.login.__wrapped__.__wrapped__
    payload = await login_fn(request)
    assert isinstance(payload, dict)
    assert delays == [3]
    assert events == ["user_login_failed"]
    assert payload["form"].email.errors


@pytest.mark.asyncio
async def test_login_wrong_password_sets_lockout_after_failure_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Field:
        def __init__(self, data):
            self.data = data
            self.errors = []

    class _Form:
        def __init__(self, data, meta):
            _ = meta
            self.email = _Field(data.get("email", "user@example.com"))
            self.password = _Field(data.get("password", "wrong"))
            self.csrf_token = _Field("csrf")

        def validate(self):
            return True

        def add_email_error(self, message):
            self.email.errors.append(message)

    class _Record:
        def scalar(self):
            return SimpleNamespace(
                id=5,
                is_active=True,
                password="hash",
                email="user@example.com",
                is_ldap=False,
            )

    class _DB:
        async def execute(self, stmt):
            _ = stmt
            return _Record()

    class _Redis:
        def __init__(self):
            self.set_calls = []

        async def exists(self, _key):
            return 0

        async def set(self, *args, **kwargs):
            self.set_calls.append((args, kwargs))
            return True

        async def incr(self, key):
            assert key == "auth:login_failures:user@example.com"
            return 10

        async def expire(self, key, ttl):
            _ = key, ttl
            return True

    redis = _Redis()

    async def _sleep(_seconds):
        return None

    async def _get_session(_request):
        return {}

    events = []

    async def _admin_event(event, req):
        _ = req
        events.append(event)

    request = _Request(
        method="POST",
        app=_App(
            {REDIS_KEY: redis},
            router={"index": _Route("/"), "login": _Route("/login/")},
        ),
        post_data={"email": "user@example.com", "password": "wrong"},
    )
    request["db"] = _DB()

    monkeypatch.setattr(auth_views.forms, "Login", _Form)
    monkeypatch.setattr(auth_views, "get_session", _get_session)
    monkeypatch.setattr(auth_views, "admin_event", _admin_event)
    monkeypatch.setattr(auth_views.asyncio, "sleep", _sleep)
    monkeypatch.setattr(
        auth_views.password_context, "verify", lambda raw, hashed: False
    )

    payload = await auth_views.login.__wrapped__.__wrapped__(request)

    assert isinstance(payload, dict)
    assert (
        ("auth:login_lockout:user@example.com", "1"),
        {"ex": 15 * 60},
    ) in redis.set_calls
    assert events == ["user_login_failed"]


@pytest.mark.asyncio
async def test_login_redis_lock_blocks_parallel_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Field:
        def __init__(self, data):
            self.data = data
            self.errors = []

    class _Form:
        def __init__(self, data, meta):
            _ = meta
            self.email = _Field(data.get("email", "user@example.com"))
            self.password = _Field(data.get("password", "wrong"))
            self.csrf_token = _Field("csrf")

        def validate(self):
            return True

        def add_email_error(self, message):
            self.email.errors.append(message)

    redis_calls = {}

    class _Redis:
        async def exists(self, key):
            redis_calls["exists_key"] = key
            return 1

        async def set(self, *args, **kwargs):
            _ = args, kwargs
            raise AssertionError(
                "set should not be called when lock key already exists"
            )

        async def delete(self, _key):
            raise AssertionError("delete must not be called when lock was not acquired")

    class _DB:
        async def execute(self, stmt):
            _ = stmt
            raise AssertionError("DB should not be queried when lock is already held")

    delays = []

    async def _sleep(seconds):
        delays.append(seconds)

    async def _get_session(_request):
        return {}

    events = []

    async def _admin_event(event, req):
        _ = req
        events.append(event)

    request = _Request(
        method="POST",
        app=_App(
            {REDIS_KEY: _Redis()},
            router={"index": _Route("/"), "login": _Route("/login/")},
        ),
        post_data={"email": "user@example.com", "password": "wrong"},
    )
    request["db"] = _DB()

    monkeypatch.setattr(auth_views.forms, "Login", _Form)
    monkeypatch.setattr(auth_views, "get_session", _get_session)
    monkeypatch.setattr(auth_views, "admin_event", _admin_event)
    monkeypatch.setattr(auth_views.asyncio, "sleep", _sleep)

    login_fn = auth_views.login.__wrapped__.__wrapped__
    payload = await login_fn(request)
    assert isinstance(payload, dict)
    assert delays == []
    assert payload["form"].email.errors
    assert redis_calls["exists_key"] == "auth:login_check_lock:user@example.com"
    assert events == ["user_login_rate_limited"]
