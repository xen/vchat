from __future__ import annotations

import base64
import json as pyjson
import asyncio
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web

from vchat import ai_providers, document_types, metrics, settings, utils


def test_document_types_guess_and_labels() -> None:
    assert document_types.guess_document_type("https://x/a.md") == "markdown"
    assert document_types.guess_document_type("https://x/a.tar.gz") == "other"
    assert document_types.guess_document_type(content_type="text/html; charset=utf-8") == "html"
    assert document_types.guess_document_type(content_type="audio/mpeg") == "audio"
    assert document_types.guess_document_type(content_type="application/vnd.custom+json") == "code"
    assert document_types.get_document_type_label("office") == "Office document"
    assert document_types.get_document_type_label("") == "Other"
    assert document_types.get_document_type_label("custom_type") == "Custom Type"


@pytest.mark.asyncio
async def test_client_session_and_api_client() -> None:
    mod = __import__("vchat.aiohttp_client", fromlist=["ApiClient", "client_session"])
    session = mod.client_session(client_timeout=1)
    try:
        client = mod.ApiClient(session)
        assert client.session is session
        assert int(session.timeout.total) == 1
    finally:
        await session.close()


def test_yaml_load_converts_bool_like_strings() -> None:
    payload = """
flag_yes: yes
flag_no: no
plain: hello
"""
    loaded = settings.yaml_load(payload)
    assert loaded["flag_yes"] is True
    assert loaded["flag_no"] is False
    assert loaded["plain"] == "hello"


def test_ai_providers_and_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(settings.config, "openai_api_key", "k")
    monkeypatch.setitem(settings.config, "openai_base_url", "https://example.test/v1")
    providers = ai_providers.list_ai_providers()
    assert providers
    openai = ai_providers.get_provider("openai")
    assert openai.request_meta()["api_key"] == "k"
    assert openai.request_meta()["base_url"] == "https://example.test/v1"
    model = openai.get_model(None)
    assert model.id
    resolved_provider, resolved_model = ai_providers.resolve_ai_settings("openai", model.id)
    assert resolved_provider.id == "openai"
    assert resolved_model.id == model.id
    assert ai_providers.is_provider_available("openai") is True
    assert ai_providers.is_model_available("openai", model.id) is True


@pytest.mark.asyncio
async def test_metrics_record_and_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    req_counter = metrics.CHAT_REQUESTS_TOTAL.labels(
        provider="openai", model="gpt-4o-mini", status="ok", guardrail="true"
    )
    tok_counter = metrics.CHAT_TOKENS_TOTAL.labels(provider="openai", model="gpt-4o-mini")
    grd_counter = metrics.CHAT_GUARDRAIL_EVENTS_TOTAL.labels(
        provider="openai", model="gpt-4o-mini", reason="unknown"
    )
    before_req = req_counter._value.get()
    before_tok = tok_counter._value.get()
    before_grd = grd_counter._value.get()

    metrics.record_chat_request(
        provider="openai",
        model="gpt-4o-mini",
        tokens=33,
        status="ok",
        guardrail_reasons={"something_unlisted"},
    )

    assert req_counter._value.get() == before_req + 1
    assert tok_counter._value.get() == before_tok + 33
    assert grd_counter._value.get() == before_grd + 1

    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    assert metrics._is_multiprocess_enabled() is False

    response = await metrics.metrics_handler(None)
    assert response.status == 200
    assert b"vchat_chat_requests_total" in response.body


def test_utils_json_to_str_meta_and_convert() -> None:
    payload = {"a": 1, "b": [1, 2]}
    dumped = utils.json.dumps(payload)
    assert isinstance(dumped, str)
    assert utils.json.loads(dumped) == payload
    assert utils.to_str("x") == "x"
    assert utils.to_str(["a", "b"]) == "ab"
    assert utils.to_str(3) == "3"

    m = utils.Meta()
    m.update(title="T", description=["d", "e"])
    assert m.title == "T"
    assert m.description == "de"
    assert "Meta title='T'" in repr(m)

    html, meta = utils.convert_to_html("# H")
    assert "<h1" in html
    assert isinstance(meta, dict)


@pytest.mark.asyncio
async def test_flash_admin_event_login_required_and_make_url(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeRedis:
        def __init__(self) -> None:
            self.ops: list[tuple] = []

        async def rpush(self, key: str, payload: str) -> None:
            self.ops.append(("rpush", key, payload))

        async def expire(self, key: str, ttl: int) -> None:
            self.ops.append(("expire", key, ttl))

        async def publish(self, ch: str, payload: str) -> None:
            self.ops.append(("publish", ch, payload))

    redis = FakeRedis()
    class Req(dict):
        def __init__(self):
            super().__init__(user=SimpleNamespace(id=7, email="a@b.c"))
            self.app = {utils.REDIS_KEY: redis}

    req = Req()
    await utils.flash(req, "hello|bad", category="info")
    assert any(op[0] == "publish" for op in redis.ops)

    class FakeDB:
        def __init__(self) -> None:
            self.items = []
            self.commits = 0

        def add(self, item):
            self.items.append(item)

        async def commit(self) -> None:
            self.commits += 1

    class FakeRequest(dict):
        headers = {"X-Forwarded-For": "1.2.3.4, 10.0.0.1"}
        remote = "127.0.0.1"

    req2 = FakeRequest(db=FakeDB(), user=SimpleNamespace(id=1, email="u@x"))
    await utils.admin_event(" user_created ", req2)
    assert req2["db"].commits == 1
    assert req2["db"].items

    app = web.Application()

    async def _h(_):
        return web.Response(text="ok")

    app.router.add_get("/login/", _h, name="login")
    app.router.add_get("/x/{id}", _h, name="x")
    app[utils.CONFIG_KEY] = {"public_url": "https://local.vchat.com"}

    class Req(dict):
        path = "/private"

        def __init__(self, app):
            super().__init__(app=app)
            self.app = app

    r = Req(app=app)

    async def _session(_request):
        return {}

    monkeypatch.setattr(utils, "get_session", _session)

    @utils.login_required()
    async def _protected(request):
        return web.Response(text="ok")

    resp = await _protected(r)
    assert isinstance(resp, web.HTTPFound)
    assert "next=%2Fprivate" in str(resp.location)

    full = utils.make_full_url(r, "x", id=5, query_={"a": "b"})
    assert "a=b" in str(full)


def test_protect_dummyjar_and_paginator(monkeypatch: pytest.MonkeyPatch) -> None:
    token = utils.protect({"a": 1}, salt="s")
    assert utils.serializer.loads(token, b"s") == {"a": 1}

    token_timed = utils.protect_timed("v", salt="x")
    assert utils.serializer_timed.loads(token_timed, salt=b"x") == "v"

    class _ConcreteDummyJar(utils.DummyJar):
        @property
        def quote_cookie(self):  # pragma: no cover - required by abstract base
            return True

    loop = asyncio.new_event_loop()
    try:
        jar = _ConcreteDummyJar(loop=loop)
    finally:
        loop.close()
    assert len(jar) == 0
    with pytest.raises(StopIteration):
        list(jar)

    class Req:
        path = "/items"
        query = {"offset": "0", "limit": "10", "q": "abc"}

    def _render_string(_tpl, _request, ctx):
        assert "paginator" in ctx
        return "<nav>ok</nav>"

    monkeypatch.setattr(utils.aiohttp_jinja2, "render_string", _render_string)
    html = utils.paginator(101, Req())
    assert "<nav>ok</nav>" in str(html)


@pytest.mark.asyncio
async def test_run_command_and_run_task(monkeypatch: pytest.MonkeyPatch) -> None:
    code, stdout, stderr = await utils.run_command("printf hi")
    assert code == 0
    assert stdout == "hi"
    assert stderr == ""

    class FakeRedis:
        def __init__(self):
            self.items = []

        async def lpush(self, q: str, payload: str):
            self.items.append((q, payload))

    fake_redis = FakeRedis()
    monkeypatch.setattr(utils, "json", pyjson)
    monkeypatch.setattr(utils, "redis", fake_redis)
    monkeypatch.setattr(utils, "CELERY_DEFAULT_QUEUE", "q")

    task_id = await utils.run_task("jobs.embedder.tasks.index", msg_id=12)
    assert isinstance(task_id, str)
    assert fake_redis.items

    queue_name, payload = fake_redis.items[0]
    assert queue_name == "q"
    envelope = pyjson.loads(payload)
    body = base64.b64decode(envelope["body"]).decode("utf-8")
    decoded = pyjson.loads(body)
    assert decoded[1]["msg_id"] == 12
