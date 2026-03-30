from __future__ import annotations

from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from vchat.app_keys import CONFIG_KEY, REDIS_KEY
import vchat.middlewares as mw
from vchat.routes import setup_routes, to_path


@pytest.mark.asyncio
async def test_meta_and_handle_error(monkeypatch: pytest.MonkeyPatch) -> None:
    req = make_mocked_request("GET", "/x")

    async def _h(request):
        assert "meta" in request
        return web.Response(text="ok")

    resp = await mw.meta_middleware(req, _h)
    assert resp.status == 200

    def _render(_tpl, _request, _ctx, status=200):
        return web.Response(text="err", status=status)

    monkeypatch.setattr(mw.aiohttp_jinja2, "render_template", _render)
    err = await mw.handle_error(req, code=404)
    assert err.status == 404


@pytest.mark.asyncio
async def test_error_middleware_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    req = make_mocked_request("GET", "/missing")

    async def _handler_http(_request):
        raise web.HTTPNotFound()

    async def _handler_exc(_request):
        raise RuntimeError("boom")

    async def _fake_handle_error(_request, code=404):
        return web.Response(text=f"e:{code}", status=code)

    monkeypatch.setattr(mw, "handle_error", _fake_handle_error)

    resp1 = await mw.error_middleware(req, _handler_http)
    assert resp1.status == 404

    resp2 = await mw.error_middleware(req, _handler_exc)
    assert resp2.status == 500


@pytest.mark.asyncio
async def test_debug_access_control_header_middleware() -> None:
    req = make_mocked_request("GET", "/")

    async def _handler(_request):
        return web.Response(text="ok")

    resp = await mw.debug_access_control_header_middleware(req, _handler)
    assert resp.headers["Access-Control-Allow-Origin"] == "*"


@pytest.mark.asyncio
async def test_db_session_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeSession:
        def __init__(self) -> None:
            self.rollbacks = 0

        async def rollback(self) -> None:
            self.rollbacks += 1

    session = FakeSession()

    class Factory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(mw, "async_session_factory", Factory())

    req = make_mocked_request("GET", "/a")

    async def _ok(request):
        assert "db" in request
        return web.Response(text="ok")

    ok_resp = await mw.db_session_middleware(req, _ok)
    assert ok_resp.status == 200

    async def _err(_request):
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        await mw.db_session_middleware(req, _err)
    assert session.rollbacks == 1


@pytest.mark.asyncio
async def test_auth_flash_and_force_https(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeScalar:
        def __init__(self, user):
            self._user = user

        def first(self):
            return self._user

    class FakeExecResult:
        def __init__(self, user):
            self._user = user

        def scalars(self):
            return FakeScalar(self._user)

    class FakeDB:
        async def execute(self, stmt):
            assert isinstance(stmt, sa.sql.Select)
            return FakeExecResult(SimpleNamespace(id=10, email="u@example"))

    class FakeAuthSession(dict):
        invalidated = False

        def invalidate(self):
            self.invalidated = True

    session = FakeAuthSession(staff_id=10)

    async def _get_session(_request):
        return session

    monkeypatch.setattr(mw, "get_session", _get_session)

    req = make_mocked_request("GET", "/x")
    req["db"] = FakeDB()

    async def _handler(request):
        return web.Response(text=getattr(request.get("user"), "email", "none"))

    resp = await mw.auth_middleware(req, _handler)
    assert resp.text == "u@example"

    class FakeRedis:
        async def lrange(self, *_args):
            return [b"success|ok"]

        async def delete(self, *_args):
            return None

    req2 = make_mocked_request("GET", "/x")
    req2["user"] = SimpleNamespace(id=77)
    req2.app[REDIS_KEY] = FakeRedis()

    async def _h2(request):
        assert request["flash_messages"][0].status == "success"
        return web.Response(text="ok")

    resp2 = await mw.flash_middleware(req2, _h2)
    assert resp2.status == 200

    req3 = make_mocked_request("GET", "/x")
    req3.app[CONFIG_KEY] = {"public_url": "https://local.vchat.com"}

    async def _h3(_request):
        r = web.HTTPFound("http://example.com/path")
        return r

    resp3 = await mw.force_https_location_middleware(req3, _h3)
    assert resp3.headers["Location"].startswith("https://")


def test_get_middlewares_and_routes() -> None:
    cfg = {
        "cookie_key": b"12345678901234567890123456789012",
        "cookie_name": "s",
        "cookie_domain": ".example.com",
        "cookie_secure": False,
        "enable_https_middleware": False,
    }
    mws = mw.get_middlewares(cfg)
    assert mws

    app = web.Application()
    setup_routes(app)
    assert app.router["login"].url_for().human_repr() == "/login/"
    assert app.router["logout"].url_for().human_repr() == "/logout/"
    assert app.router["actions"].url_for(action="x", item_id="1").human_repr() == "/actions/x/1"

    assert to_path(app.router["login"].url_for()) == "/login/"
    assert to_path(app.router["users"].url_for(), has_trailing_slash=False) == "/users"
