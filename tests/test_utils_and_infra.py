from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp import web

from vchat import settings, utils
from vchat.views import metrics
from vchat.utils import json_response
from jobs.documents import types as document_types
from vchat.views.projects.page_status import PageStatus
from vchat.views.chat import ai as ai_providers


def test_document_types_guess_and_labels() -> None:
    assert document_types.guess_document_type("https://x/a.md") == "markdown"
    assert document_types.guess_document_type("https://x/a.tar.gz") == "other"
    assert (
        document_types.guess_document_type(content_type="text/html; charset=utf-8")
        == "html"
    )
    assert document_types.guess_document_type(content_type="audio/mpeg") == "audio"
    assert (
        document_types.guess_document_type(content_type="application/vnd.custom+json")
        == "code"
    )


def test_json_response_uses_msgspec_compatible_body() -> None:
    response = json_response({"status": PageStatus.ready}, status=202)

    assert response.status == 202
    assert response.content_type == "application/json"
    assert response.body == b'{"status":"ready"}'
    assert response.text == '{"status":"ready"}'


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
    monkeypatch.setattr(settings.cfg, "openai_api_key", "k")
    monkeypatch.setattr(settings.cfg, "openai_base_url", "https://example.test/v1")
    providers = ai_providers.list_ai_providers()
    assert providers
    openai = ai_providers.get_provider("openai")
    assert openai.request_meta()["api_key"] == "k"
    assert openai.request_meta()["base_url"] == "https://example.test/v1"
    model = openai.get_model("gpt-4o-mini")
    assert model.id
    resolved_provider, resolved_model = ai_providers.resolve_ai_settings(
        "openai", model.id
    )
    assert resolved_provider.id == "openai"
    assert resolved_model.id == model.id


@pytest.mark.asyncio
async def test_metrics_record_and_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    req_counter = metrics.CHAT_REQUESTS_TOTAL.labels(
        provider="openai", model="gpt-4o-mini", status="ok", guardrail="true"
    )
    tok_counter = metrics.CHAT_TOKENS_TOTAL.labels(
        provider="openai", model="gpt-4o-mini"
    )
    grd_counter = metrics.CHAT_GUARDRAIL_EVENTS_TOTAL.labels(
        provider="openai", model="gpt-4o-mini", reason="unknown"
    )
    duration_histogram = metrics.CHAT_RESPONSE_DURATION_SECONDS.labels(
        provider="openai", model="gpt-4o-mini", status="ok", guardrail="true"
    )
    chunks_histogram = metrics.CHAT_CONTEXT_CHUNKS.labels(
        provider="openai", model="gpt-4o-mini", status="ok"
    )
    before_req = req_counter._value.get()
    before_tok = tok_counter._value.get()
    before_grd = grd_counter._value.get()
    before_duration_sum = duration_histogram._sum.get()
    before_chunks_sum = chunks_histogram._sum.get()

    metrics.record_chat_request(
        provider="openai",
        model="gpt-4o-mini",
        tokens=33,
        status="ok",
        guardrail_reasons={"something_unlisted"},
        duration_seconds=1.25,
        context_chunks=3,
    )

    assert req_counter._value.get() == before_req + 1
    assert tok_counter._value.get() == before_tok + 33
    assert grd_counter._value.get() == before_grd + 1
    assert duration_histogram._sum.get() == before_duration_sum + 1.25
    assert chunks_histogram._sum.get() == before_chunks_sum + 3

    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    assert metrics._is_multiprocess_enabled() is False

    response = await metrics.metrics_handler(None)
    assert response.status == 200
    assert b"vchat_chat_requests_total" in response.body
    assert b"vchat_chat_response_duration_seconds" in response.body
    assert b"vchat_chat_context_chunks" in response.body
    assert b"vchat_request_embedding_queue_wait_seconds" in response.body
    assert b"vchat_request_embedding_encode_seconds" in response.body
    assert b"vchat_request_embedding_inflight" in response.body


def test_utils_json_to_str_and_meta() -> None:
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


@pytest.mark.asyncio
async def test_flash_admin_event_login_required_and_make_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    monkeypatch.setattr(utils.cfg, "public_url", "https://local.vchat.com")

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
    async def _protected(_request):
        return web.Response(text="ok")

    with pytest.raises(web.HTTPFound) as exc_info:
        await _protected(r)
    resp = exc_info.value
    assert "next=%2Fprivate" in str(resp.location)

    full = utils.make_full_url(r, "x", id=5, query_={"a": "b"})
    assert "a=b" in str(full)


def test_protect_and_paginator() -> None:
    token = utils.protect({"a": 1}, salt="s")
    assert utils.serializer.loads(token, b"s") == {"a": 1}

    token_timed = utils.protect_timed("v", salt="x")
    assert utils.serializer_timed.loads(token_timed, salt=b"x") == "v"

    pagination = utils.paginator(101, page=3, per_page=10)
    assert pagination["page"] == 3
    assert pagination["total_pages"] == 11
    assert pagination["range_start"] == 21
    assert pagination["range_end"] == 30
    assert pagination["pages"][0]["number"] == 1

    pagination_with_links = utils.paginator(
        200,
        page=10,
        per_page=10,
        query_factory=lambda number: {"page": str(number)},
        href_factory=lambda number: f"/items?page={number}",
    )
    assert {"number": None} in pagination_with_links["pages"]
    current = next(
        item for item in pagination_with_links["pages"] if item.get("number") == 10
    )
    assert current["query"] == {"page": "10"}
    assert current["href"] == "/items?page=10"
