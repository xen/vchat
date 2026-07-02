from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from vchat.settings import CONFIG_KEY, REDIS_KEY
import vchat.middlewares as mw
from vchat.routes import setup_routes, to_path
from vchat.views import health


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
async def test_reject_trace_middleware_blocks_trace() -> None:
    req = make_mocked_request("TRACE", "/")

    async def _handler(_request):
        return web.Response(text="ok")

    with pytest.raises(web.HTTPMethodNotAllowed):
        await mw.reject_trace_middleware(req, _handler)


@pytest.mark.asyncio
async def test_debug_access_control_header_middleware() -> None:
    req = make_mocked_request("GET", "/")

    async def _handler(_request):
        return web.Response(text="ok")

    resp = await mw.debug_access_control_header_middleware(req, _handler)
    assert resp.headers["Access-Control-Allow-Origin"] == "*"
    assert "x-request-id" in resp.headers["Access-Control-Allow-Headers"]


@pytest.mark.asyncio
async def test_request_id_middleware_reuses_header() -> None:
    req = make_mocked_request("GET", "/", headers={"X-Request-ID": "req-123"})

    async def _handler(request):
        assert request["request_id"] == "req-123"
        return web.Response(text="ok")

    resp = await mw.request_id_middleware(req, _handler)
    assert resp.headers["X-Request-ID"] == "req-123"


@pytest.mark.asyncio
async def test_request_id_middleware_generates_invalid_missing_header() -> None:
    req = make_mocked_request("GET", "/", headers={"X-Request-ID": "bad header"})

    async def _handler(request):
        assert request["request_id"] != "bad header"
        assert len(request["request_id"]) == 32
        return web.Response(text="ok")

    resp = await mw.request_id_middleware(req, _handler)
    assert len(resp.headers["X-Request-ID"]) == 32


@pytest.mark.asyncio
async def test_db_session_middleware(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class FakeSession:
        def __init__(self) -> None:
            self.rollbacks = 0
            self._in_transaction = True

        async def rollback(self) -> None:
            self.rollbacks += 1
            self._in_transaction = False

        def in_transaction(self) -> bool:
            return self._in_transaction

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
    assert session.rollbacks == 1
    assert (
        "DB transaction left open while rendering GET /a; rolling back" in caplog.text
    )

    session._in_transaction = True

    async def _err(_request):
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        await mw.db_session_middleware(req, _err)
    assert session.rollbacks == 2


@pytest.mark.asyncio
async def test_auth_flash_and_force_https(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeExecResult:
        def first(self):
            return SimpleNamespace(
                id=10,
                email="u@example",
                name="User",
                is_active=True,
                auth_user_session_id=20,
                last_seen_at=datetime.now(timezone.utc),
            )

    class FakeDB:
        def __init__(self) -> None:
            self.commits = 0
            self.rollbacks = 0
            self._in_transaction = False
            self.statements = []

        async def execute(self, stmt):
            self.statements.append(stmt)
            return FakeExecResult()

        def in_transaction(self) -> bool:
            return self._in_transaction

        async def commit(self) -> None:
            self.commits += 1
            self._in_transaction = False

        async def rollback(self) -> None:
            self.rollbacks += 1
            self._in_transaction = False

    class FakeAuthSession(dict):
        invalidated = False

        def invalidate(self):
            self.invalidated = True

    session = FakeAuthSession(user_id=10, session_id="session-10", login_at=100)

    async def _get_session(_request):
        return session

    monkeypatch.setattr(mw, "get_session", _get_session)
    monkeypatch.setattr(mw.time, "time", lambda: 120)

    req = make_mocked_request("GET", "/x")
    req.app.get.return_value = {"auth_session_time": 0}
    req["db"] = FakeDB()

    async def _handler(request):
        return web.Response(text=getattr(request.get("user"), "email", "none"))

    resp = await mw.auth_middleware(req, _handler)
    assert resp.text == "u@example"
    assert isinstance(req["db"].statements[0], sa.sql.Select)
    assert req["db"].commits == 1
    assert req["db"].rollbacks == 0

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
        "session_max_age_seconds": 30 * 24 * 60 * 60,
    }
    mws = mw.get_middlewares(cfg)
    assert mws

    app = web.Application()
    setup_routes(app)
    assert app.router["health_live"].url_for().human_repr() == "/health/live"
    assert app.router["health_ready"].url_for().human_repr() == "/health/ready"
    assert app.router["login"].url_for().human_repr() == "/login/"
    assert app.router["logout"].url_for().human_repr() == "/logout/"
    with pytest.raises(KeyError):
        app.router["data"]
    assert (
        app.router["actions"].url_for(action="x", item_id="1").human_repr()
        == "/actions/x/1"
    )

    assert to_path(app.router["login"].url_for()) == "/login/"
    assert to_path(app.router["users"].url_for(), has_trailing_slash=False) == "/users"


class HealthDB:
    def __init__(self, *, fail: bool = False) -> None:
        self.executed = False
        self.fail = fail

    async def execute(self, _query) -> None:
        self.executed = True
        if self.fail:
            raise RuntimeError("db down")


class HealthRedis:
    def __init__(self, *, fail: bool = False) -> None:
        self.pinged = False
        self.fail = fail

    async def ping(self) -> None:
        self.pinged = True
        if self.fail:
            raise RuntimeError("redis down")


class HealthBrokerRedis:
    def __init__(self) -> None:
        self.closed = False

    async def ping(self) -> None:
        return None

    async def llen(self, queue_name: str) -> int:
        return {"celery": 1, "crawler": 0, "embeddings": 2}[queue_name]

    async def aclose(self) -> None:
        self.closed = True


class HealthRequest(dict):
    def __init__(self, redis: HealthRedis, db: HealthDB) -> None:
        super().__init__(db=db)
        self.app = {REDIS_KEY: redis}


def _patch_ready_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    worker_queues: dict[str, list[str]] | None = None,
) -> None:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    monkeypatch.setitem(health.config, "chat_provider", "openai")
    monkeypatch.setitem(health.config, "chat_model", "gpt-4o-mini")
    monkeypatch.setitem(health.config, "openai_api_key", "test-key")
    monkeypatch.setitem(health.config, "embedding_model_dir", str(model_dir))
    monkeypatch.setitem(health.config, "celery_redis_uri", "redis://localhost/")
    monkeypatch.setitem(health.config, "celery_broker_db", 31)
    monkeypatch.setattr(health, "redis_from_url", lambda *_args, **_kwargs: HealthBrokerRedis())
    monkeypatch.setattr(health, "resolve_embedding_device", lambda: "cpu")
    monkeypatch.setattr(health.chat_ctx, "_embed_model", object())
    monkeypatch.setattr(health.chat_ctx, "_rerank_model", object())
    monkeypatch.setattr(
        health,
        "_inspect_celery_workers",
        lambda _timeout: worker_queues
        or {"worker-default": ["celery"], "worker-embedder": ["embeddings"]},
    )


@pytest.mark.asyncio
async def test_health_handlers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    live_response = await health.live(None)  # type: ignore[arg-type]
    assert live_response.status == 200

    _patch_ready_dependencies(monkeypatch, tmp_path)

    db = HealthDB()
    redis = HealthRedis()
    ready_response = await health.ready(HealthRequest(redis, db))  # type: ignore[arg-type]
    payload = json.loads(ready_response.text)

    assert ready_response.status == 200
    assert payload["status"] == "ok"
    assert payload["checks"]["database"]["status"] == "ok"
    assert payload["checks"]["redis"]["status"] == "ok"
    assert payload["checks"]["celery_broker"]["queues"]["embeddings"] == 2
    assert payload["checks"]["embedder"]["workers"] == ["worker-embedder"]
    assert payload["checks"]["llm"] == {
        "status": "ok",
        "provider": "openai",
        "model": "gpt-4o-mini",
    }
    assert db.executed is True
    assert redis.pinged is True


@pytest.mark.asyncio
async def test_health_ready_returns_503_without_embedder_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_ready_dependencies(
        monkeypatch,
        tmp_path,
        worker_queues={"worker-default": ["celery"]},
    )

    response = await health.ready(HealthRequest(HealthRedis(), HealthDB()))  # type: ignore[arg-type]
    payload = json.loads(response.text)

    assert response.status == 503
    assert payload["status"] == "degraded"
    assert payload["checks"]["embedder"]["status"] == "failed"
    assert payload["checks"]["embedder"]["required_queue"] == "embeddings"


@pytest.mark.asyncio
async def test_health_ready_returns_503_for_invalid_llm_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_ready_dependencies(monkeypatch, tmp_path)
    monkeypatch.setitem(health.config, "chat_provider", "missing-provider")

    response = await health.ready(HealthRequest(HealthRedis(), HealthDB()))  # type: ignore[arg-type]
    payload = json.loads(response.text)

    assert response.status == 503
    assert payload["status"] == "degraded"
    assert payload["checks"]["llm"]["status"] == "failed"
    assert payload["checks"]["llm"]["provider"] == "missing-provider"


@pytest.mark.asyncio
async def test_health_ready_returns_503_for_database_and_redis_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_ready_dependencies(monkeypatch, tmp_path)

    response = await health.ready(
        HealthRequest(HealthRedis(fail=True), HealthDB(fail=True))  # type: ignore[arg-type]
    )
    payload = json.loads(response.text)

    assert response.status == 503
    assert payload["status"] == "degraded"
    assert payload["checks"]["database"]["status"] == "failed"
    assert payload["checks"]["redis"]["status"] == "failed"


def test_error_base_template_defines_home_link() -> None:
    templates_dir = Path(__file__).resolve().parents[1] / "vchat" / "templates"
    template = (templates_dir / "error.html").read_text()
    assert 'href="/"' in template
    assert "underline" in template
