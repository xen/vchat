from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp import web
from yarl import URL

from vchat.app_keys import CONFIG_KEY, REDIS_KEY
import vchat.middlewares as mdw
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
async def test_flash_and_force_https_middlewares() -> None:
    class _Redis:
        async def lrange(self, key, _start, _end):
            assert key == "message_1"
            return [b"success|Saved", b"error|Failed"]

        async def delete(self, key):
            assert key == "message_1"

    app = _App(
        {REDIS_KEY: _Redis(), CONFIG_KEY: {"public_url": "https://local.vchat.com"}}
    )
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
    class _ExecuteResult:
        def first(self):
            return SimpleNamespace(
                id=7,
                email="u@example.com",
                name="User Seven",
                is_active=True,
            )

    class _DB:
        async def execute(self, stmt):
            _ = stmt
            return _ExecuteResult()

        def in_transaction(self):
            return False

    class _Session(dict):
        def invalidate(self):
            self["invalidated"] = True

    async def _get_session(_request):
        return _Session(user_id=7)

    monkeypatch.setattr(mdw, "get_session", _get_session)

    request = _Request(path="/dashboard")
    db = _DB()
    request["db"] = db

    async def _handler(req):
        return web.Response(text=req["user"].email)

    resp = await mdw.auth_middleware(request, _handler)
    assert resp.text == "u@example.com"


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
        async def execute(self, stmt):
            _ = stmt
            return _Record()

    class _Redis:
        async def exists(self, _key):
            return 0

        async def set(self, *args, **kwargs):
            _ = args, kwargs
            return True

    login_router = {"index": _Route("/"), "login": _Route("/login/")}
    request = _Request(
        method="POST",
        app=_App({CONFIG_KEY: {}, REDIS_KEY: _Redis()}, router=login_router),
        post_data={"email": "user@example.com", "password": "pass"},
        query={},
    )
    db = _DB()
    request["db"] = db

    async def _get_session(_request):
        return {}

    async def _new_session(_request):
        return {}

    async def _admin_event(name, req):
        _ = name, req

    monkeypatch.setattr(auth_views.forms, "LoginForm", _Form)
    monkeypatch.setattr(auth_views, "get_session", _get_session)
    monkeypatch.setattr(auth_views, "new_session", _new_session)
    monkeypatch.setattr(auth_views.password_context, "verify", lambda raw, hashed: True)
    monkeypatch.setattr(auth_views, "admin_event", _admin_event)

    # strip decorators (meta + template)
    login_fn = auth_views.login.__wrapped__.__wrapped__
    login_resp = await login_fn(request)
    assert isinstance(login_resp, web.HTTPFound)
    assert str(login_resp.location) == "/"
    assert request["user"].id == 5

    class _LogoutSession(dict):
        def invalidate(self):
            self["done"] = True

    async def _logout_session(_request):
        return _LogoutSession()

    monkeypatch.setattr(auth_views, "get_session", _logout_session)
    logout_fn = auth_views.logout.__wrapped__.__wrapped__
    request2 = _Request(
        method="GET", app=_App({CONFIG_KEY: {}}, router={"login": _Route("/login/")})
    )
    request2["user"] = SimpleNamespace(id=5)
    logout_resp = await logout_fn(request2)
    assert isinstance(logout_resp, web.HTTPFound)
    assert str(logout_resp.location) == "/login/"


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

    delays = []

    async def _sleep(seconds):
        delays.append(seconds)

    async def _get_session(_request):
        return {}

    request = _Request(
        method="POST",
        app=_App(
            {CONFIG_KEY: {}, REDIS_KEY: _Redis()},
            router={"index": _Route("/"), "login": _Route("/login/")},
        ),
        post_data={"email": "user@example.com", "password": "wrong"},
    )
    request["db"] = _DB()

    monkeypatch.setattr(auth_views.forms, "LoginForm", _Form)
    monkeypatch.setattr(auth_views, "get_session", _get_session)
    monkeypatch.setattr(auth_views.asyncio, "sleep", _sleep)
    monkeypatch.setattr(
        auth_views.password_context, "verify", lambda raw, hashed: False
    )

    login_fn = auth_views.login.__wrapped__.__wrapped__
    payload = await login_fn(request)
    assert isinstance(payload, dict)
    assert delays == [3]
    assert payload["form"].email.errors


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

    request = _Request(
        method="POST",
        app=_App(
            {CONFIG_KEY: {}, REDIS_KEY: _Redis()},
            router={"index": _Route("/"), "login": _Route("/login/")},
        ),
        post_data={"email": "user@example.com", "password": "wrong"},
    )
    request["db"] = _DB()

    monkeypatch.setattr(auth_views.forms, "LoginForm", _Form)
    monkeypatch.setattr(auth_views, "get_session", _get_session)
    monkeypatch.setattr(auth_views.asyncio, "sleep", _sleep)

    login_fn = auth_views.login.__wrapped__.__wrapped__
    payload = await login_fn(request)
    assert isinstance(payload, dict)
    assert delays == []
    assert payload["form"].email.errors
    assert redis_calls["exists_key"] == "auth:login_check_lock:user@example.com"
