from __future__ import annotations

from types import SimpleNamespace
import json as pyjson

import pytest
from aiohttp import web
from yarl import URL

from vchat.app_keys import CONFIG_KEY, REDIS_KEY
from vchat import metrics
from vchat import settings
from vchat import utils
from vchat.ai_providers import (
    get_default_model_id,
    get_model_choices,
    get_models_for_provider,
    get_provider,
    is_model_available,
    is_provider_available,
    resolve_ai_settings,
)


class _Route:
    def __init__(self, path: str):
        self.path = path

    def url_for(self, **kwargs):
        value = self.path.format(**kwargs) if kwargs else self.path
        return URL(value)


class _Redis:
    def __init__(self):
        self.calls = []
        self.queue = []

    async def rpush(self, key, payload):
        self.calls.append(("rpush", key))
        self.queue.append(payload)

    async def expire(self, key, ttl):
        self.calls.append(("expire", key, ttl))

    async def publish(self, channel, payload):
        self.calls.append(("publish", channel))

    async def lpush(self, key, payload):
        self.calls.append(("lpush", key))
        self.queue.append(payload)


class _Req(dict):
    def __init__(self, *, app, path="/", headers=None, remote="127.0.0.1"):
        super().__init__()
        self.app = app
        self.path = path
        self.headers = headers or {}
        self.remote = remote


@pytest.mark.asyncio
async def test_flash_and_admin_event(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = _Redis()
    db_added = []

    class _DB:
        def add(self, obj):
            db_added.append(obj)

        async def commit(self):
            return None

    req = _Req(
        app={REDIS_KEY: redis},
        headers={"X-Forwarded-For": "10.0.0.1, 10.0.0.2"},
        remote="127.0.0.1",
    )
    req["user"] = SimpleNamespace(id=1, email="admin@example.com")
    req["db"] = _DB()

    await utils.flash(req, "Saved|ok", "success")
    assert any(call[0] == "rpush" for call in redis.calls)
    assert "Savedok" in redis.queue[0]

    await utils.admin_event("user_update", req)
    assert db_added
    assert db_added[0].event_name == "user_update"
    assert db_added[0].ip_address == "10.0.0.1"

    req.path = "/files/10"
    await utils.admin_event("file_update", req)
    assert db_added[1].event_name == "file_update @ /files/10"


@pytest.mark.asyncio
async def test_login_required_redirects_when_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _App(dict):
        def __init__(self):
            super().__init__()
            self.router = {"login": _Route("/login/")}

    async def _get_session(_request):
        return {}

    monkeypatch.setattr(utils, "get_session", _get_session)

    @utils.login_required()
    async def _protected(request):
        return web.Response(text="ok")

    req = _Req(app=_App(), path="/stats")
    req["user"] = None
    resp = await _protected(req)
    assert isinstance(resp, web.HTTPFound)
    assert "next=%2Fstats" in resp.location


def test_make_full_url_and_meta_decorator() -> None:
    class _App(dict):
        def __init__(self):
            super().__init__({CONFIG_KEY: {"public_url": "https://local.vchat.com"}})
            self.router = {"doc": _Route("/doc/{doc_id}")}

    req = _Req(app=_App())
    url = utils.make_full_url(req, "doc", doc_id=12, query_={"a": "1"})
    assert str(url) == "https://local.vchat.com/doc/12%3Fa=1"


@pytest.mark.asyncio
async def test_run_task_enqueues_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = _Redis()
    monkeypatch.setattr(utils, "redis", redis)
    monkeypatch.setattr(utils, "json", pyjson)
    task_id = await utils.run_task("jobs.embedder.tasks.index_document", doc_id=77)
    assert task_id
    assert any(call[0] == "lpush" for call in redis.calls)
    assert "index_document" in redis.queue[0]


def test_convert_to_html_and_to_str() -> None:
    html, meta = utils.convert_to_html("# Title\n\nText")
    assert "<h1>Title</h1>" in html
    assert isinstance(meta, dict)
    assert utils.to_str(["a", "b"]) == "ab"
    assert utils.to_str(None) == "None"


def test_ai_provider_functions() -> None:
    provider, model = resolve_ai_settings("openai", "gpt-4o-mini")
    assert provider.id == "openai"
    assert model.id == "gpt-4o-mini"
    assert get_provider("openai").id == "openai"
    assert is_provider_available("openai") is True
    assert is_model_available("openai", "gpt-4o-mini") is True
    assert get_models_for_provider("openai")
    assert get_model_choices("openai")
    assert get_default_model_id("openai")


def test_settings_yaml_load_and_validation() -> None:
    import io

    loaded = settings.yaml_load(
        io.StringIO("flag: true\nraw: !env ${DOES_NOT_EXIST:-x}\n")
    )
    assert loaded["flag"] is True
    assert loaded["raw"] == "x"


def test_metrics_helpers() -> None:
    assert metrics._safe_label(" GPT-4o Mini/1 ", "fallback") == "GPT-4o Mini/1"
    assert metrics._normalize_guardrail_reason("output_blocked") == "output_blocked"


def test_crawler_queue_collector_uses_broker_db_and_default_and_embeddings_queues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    closed: list[bool] = []

    class _Redis:
        def llen(self, queue_name: str) -> int:
            calls.append(queue_name)
            return {"celery": 7, "embeddings": 11}[queue_name]

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setitem(metrics.config, "celery_redis_uri", "redis://example/")
    monkeypatch.setitem(metrics.config, "celery_broker_db", 42)
    monkeypatch.setattr(
        metrics.redis_lib.Redis,
        "from_url",
        lambda url, decode_responses=False: (
            calls.append(f"url={url},decode={decode_responses}") or _Redis()
        ),
    )

    families = list(metrics.CrawlerQueueCollector().collect())
    samples = {
        family.name: family.samples[0].value
        for family in families
    }

    assert calls[0] == "url=redis://example/42,decode=False"
    assert calls[1:] == ["celery", "embeddings"]
    assert samples["vchat_celery_queue_size"] == 7
    assert samples["vchat_embedder_queue_size"] == 11
    assert closed == [True]
