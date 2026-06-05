from __future__ import annotations

import re
from types import SimpleNamespace

import msgspec
import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from yarl import URL

import vchat.app as app_mod
import vchat.project_settings as ps
import vchat.middlewares.cors as cors
import vchat.middlewares.https as https_mw
import vchat.middlewares.shield as shield
from vchat.app_keys import CONFIG_KEY, REDIS_KEY, SETTINGS_KEY, SIGNER_KEY


def test_project_settings_normalize_merge_and_getters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert ps._normalize_value(None) is None
    assert ps._normalize_value("x") == "x"
    assert ps._normalize_value(["a"]) == '["a"]'
    assert ps._normalize_value({"a": 1}) == '{"a":1}'
    assert ps._normalize_value(42) == "42"

    merged = ps.merge_with_defaults({"project.title": "Demo"})
    assert merged["project.title"] == "Demo"
    assert "project.model" not in merged

    app = {SETTINGS_KEY: {"a": "1", "bad_int": "x", "json": "[1]", "bad_json": "{"}}
    assert ps.get_setting(app, "a") == "1"
    assert ps.get_setting(app, "missing", "d") == "d"
    assert ps.get_setting_int(app, "a", 5) == 1
    assert ps.get_setting_int(app, "bad_int", 5) == 5
    assert ps.get_setting_json(app, "json", []) == [1]
    with pytest.raises(msgspec.DecodeError):
        ps.get_setting_json(app, "bad_json", [9])


@pytest.mark.asyncio
async def test_project_settings_load_and_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Result:
        def all(self):
            return [SimpleNamespace(key="project.title", value="Loaded")]

    class _Session:
        async def execute(self, stmt):
            _ = stmt
            return _Result()

    class _Factory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, exc_type, exc, tb):
            _ = exc_type, exc, tb
            return False

    monkeypatch.setattr(ps, "async_session_factory", _Factory())
    loaded = await ps.load_settings_map()
    assert loaded["project.title"] == "Loaded"

    app = {}
    await ps.init_settings_cache(app)
    assert app[SETTINGS_KEY]["project.title"] == "Loaded"

    async def _upsert(_session, updates):
        _ = updates
        return {"project.title": "Applied"}

    monkeypatch.setattr(ps, "upsert_settings", _upsert)
    out = await ps.apply_settings_updates(app, object(), {"project.title": "X"})
    assert out == {"project.title": "Applied"}
    assert app[SETTINGS_KEY]["project.title"] == "Applied"


@pytest.mark.asyncio
async def test_project_settings_upsert_raises_db_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Stmt:
        excluded = SimpleNamespace(value="v")

        def values(self, _items):
            return self

        def on_conflict_do_update(self, **_kwargs):
            return self

    monkeypatch.setattr(ps, "insert", lambda _settings: _Stmt())

    class _Session:
        async def execute(self, _stmt):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await ps.upsert_settings(_Session(), {"a": 1})


def test_cors_match_helpers() -> None:
    assert cors.match_path("/x", "/x") is True
    assert cors.match_path(URL("/x"), "/x") is True
    assert cors.match_path(re.compile(r"^/a"), "/abc") is True
    assert cors.match_path(123, "/x") is False
    assert cors.match_items(["/x", re.compile(r"^/y")], "/y/1") is True


@pytest.mark.asyncio
async def test_cors_middleware_branches() -> None:
    m = cors.cors_middleware(
        allow_all=True,
        urls=[re.compile(r"^/api")],
        allow_credentials=False,
        expose_headers=("X-Test",),
        max_age=60,
    )

    req_skip = make_mocked_request("GET", "/web", headers={"Origin": "https://a"})

    async def _h_ok(_request):
        return web.Response(text="ok")

    skip_resp = await m(req_skip, _h_ok)
    assert skip_resp.text == "ok"

    req_no_origin = make_mocked_request("GET", "/api/x", headers={})
    no_origin_resp = await m(req_no_origin, _h_ok)
    assert "Access-Control-Allow-Origin" not in no_origin_resp.headers

    req_options = make_mocked_request(
        "OPTIONS",
        "/api/x",
        headers={"Origin": "https://a", "Access-Control-Request-Method": "POST"},
    )
    with pytest.raises(web.HTTPOk) as exc:
        await m(req_options, _h_ok)
    assert exc.value.headers["Access-Control-Allow-Origin"] == "*"
    assert "POST" in exc.value.headers["Access-Control-Allow-Methods"]


@pytest.mark.asyncio
async def test_https_middleware() -> None:
    middleware = https_mw.https_middleware()

    async def _handler(request):
        return web.Response(text=request.scheme)

    req_http = make_mocked_request("GET", "/", headers={})
    resp_http = await middleware(req_http, _handler)
    assert resp_http.text == "http"

    req_https = make_mocked_request("GET", "/", headers={"X-Forwarded-Proto": "https"})
    resp_https = await middleware(req_https, _handler)
    assert resp_https.text == "https"


def test_shield_match_and_validation_errors() -> None:
    assert shield.match_path("/x", "/x") is True
    assert shield.match_path(URL("/x"), "/x") is True
    assert shield.match_path(re.compile(r"^/x"), "/x/1") is True
    assert shield.match_request(["/x"], "GET", "/x") is True
    assert shield.match_request({"/x": "POST"}, "POST", "/x") is True
    assert shield.match_request({"/x": ["POST", "PUT"]}, "PUT", "/x") is True

    with pytest.raises(ValueError):
        shield.shield_middleware()
    with pytest.raises(ValueError):
        shield.shield_middleware(methods=("POST",), urls=["/x"])
    with pytest.raises(ValueError):
        shield.shield_middleware(urls=["/x"], ignore=["/y"])


@pytest.mark.asyncio
async def test_shield_middleware_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    async def _shield(coro):
        calls.append("shield")
        return await coro

    monkeypatch.setattr(shield.asyncio, "shield", _shield)

    async def _handler(_request):
        return web.Response(text="ok")

    m_methods = shield.shield_middleware(methods=("POST",), ignore=["/skip"])

    req_get = make_mocked_request("GET", "/a")
    assert (await m_methods(req_get, _handler)).text == "ok"

    req_skip = make_mocked_request("POST", "/skip")
    assert (await m_methods(req_skip, _handler)).text == "ok"

    req_post = make_mocked_request("POST", "/a")
    assert (await m_methods(req_post, _handler)).text == "ok"
    assert "shield" in calls

    m_urls = shield.shield_middleware(urls={re.compile(r"^/secure"): ["POST"]})
    req_url = make_mocked_request("POST", "/secure/1")
    assert (await m_urls(req_url, _handler)).text == "ok"


@pytest.mark.asyncio
async def test_init_jinja_and_create_app(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Redis:
        async def aclose(self):
            return None

    class _Signer:
        def dumps(self, value):
            return f"sig-{value}"

    class _Env:
        def __init__(self):
            self.filters = {}

    async def _init_settings_cache(app):
        app[SETTINGS_KEY] = {"project.title": "X"}

    monkeypatch.setattr(app_mod, "validate_multiprocess_setup", lambda: None)
    monkeypatch.setattr(app_mod, "redis_from_url", lambda _url: _Redis())
    monkeypatch.setattr(app_mod, "get_middlewares", lambda _cfg: [])
    monkeypatch.setattr(app_mod, "init_settings_cache", _init_settings_cache)
    monkeypatch.setattr(app_mod.aiohttp_jinja2, "setup", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_mod.aiohttp_jinja2, "get_env", lambda app: _Env())

    import vchat.routes as routes_mod
    import vchat.settings as settings_mod

    monkeypatch.setattr(
        routes_mod,
        "setup_routes",
        lambda app: app.router.add_get(
            "/x", lambda r: web.Response(text="ok"), name="x"
        ),
    )
    monkeypatch.setattr(
        settings_mod,
        "config",
        {
            "redis_uri": "redis://localhost/1",
            "max_upload_size": 1024,
            "secret_key": "secret",
            "cookie_domain": ".example.com",
        },
    )

    app = await app_mod.create_app()
    assert app[CONFIG_KEY]["secret_key"] == "secret"
    assert REDIS_KEY in app
    assert SIGNER_KEY in app
    assert len(app.on_cleanup) >= 2

    req = {
        "app": {
            CONFIG_KEY: {"x": 1},
            SIGNER_KEY: _Signer(),
            SETTINGS_KEY: {"project.title": "Y"},
            "static_version": "123",
            "router": {"x": SimpleNamespace(url_for=lambda **kwargs: URL("/x"))},
        },
        "user": SimpleNamespace(id=7),
        "meta": "m",
        "flash_messages": ["a"],
    }

    class _Req(dict):
        @property
        def app(self):
            return self["app"]

    ctx = await app_mod.init_jinja(_Req(req))
    assert ctx["csrf_token"]() == "sig-7"
    assert ctx["project_settings"]["project.title"] == "Y"
