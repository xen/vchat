from __future__ import annotations

import json as pyjson
from types import SimpleNamespace
import os
import subprocess
import sys

import pytest
import yaml
from aiohttp import web
from yarl import URL

from vchat.settings import REDIS_KEY
from jobs.indexing import documents as indexing_documents
from vchat.views import metrics
from vchat import settings
from vchat import utils
from vchat.views.chat.meta import merge_chat_meta, validate_source_page_url
from vchat.views.chat.ai import (
    get_provider,
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
        app={
            REDIS_KEY: redis,
        },
        headers={"X-Forwarded-For": "10.0.0.1, 10.0.0.2"},
        remote="127.0.0.1",
    )
    monkeypatch.setattr(utils.cfg, "trusted_proxy_cidrs", ["127.0.0.1/32"])
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


def test_client_ip_ignores_forwarded_headers_from_untrusted_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(utils.cfg, "trusted_proxy_cidrs", ["10.0.0.0/24"])
    req = _Req(
        app={},
        headers={"X-Forwarded-For": "203.0.113.10, 10.0.0.2"},
        remote="127.0.0.1",
    )

    assert utils.get_client_ip(req) == "127.0.0.1"


def test_client_ip_accepts_forwarded_headers_from_trusted_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(utils.cfg, "trusted_proxy_cidrs", ["127.0.0.1/32"])
    req = _Req(
        app={},
        headers={"X-Forwarded-For": "203.0.113.10, 10.0.0.2"},
        remote="127.0.0.1",
    )

    assert utils.get_client_ip(req) == "203.0.113.10"


def test_client_ip_ignores_invalid_forwarded_header_from_trusted_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(utils.cfg, "trusted_proxy_cidrs", ["127.0.0.1/32"])
    req = _Req(
        app={},
        headers={"X-Forwarded-For": "not-an-ip"},
        remote="127.0.0.1",
    )

    assert utils.get_client_ip(req) == "127.0.0.1"


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
    async def _protected(_request):
        return web.Response(text="ok")

    req = _Req(app=_App(), path="/stats")
    req["user"] = None
    with pytest.raises(web.HTTPFound) as exc:
        await _protected(req)
    assert "next=%2Fstats" in exc.value.location


def test_make_full_url_and_meta_decorator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(utils.cfg, "public_url", "https://local.vchat.com")

    class _App(dict):
        def __init__(self):
            super().__init__()
            self.router = {"doc": _Route("/doc/{doc_id}")}

    req = _Req(app=_App())
    url = utils.make_full_url(req, "doc", doc_id=12, query_={"a": "1"})
    assert str(url) == "https://local.vchat.com/doc/12%3Fa=1"


def test_validate_source_page_url_accepts_only_http_urls() -> None:
    assert (
        validate_source_page_url("https://example.com/page?x=1#top")
        == "https://example.com/page?x=1#top"
    )
    assert validate_source_page_url("http://example.com") == "http://example.com"
    assert validate_source_page_url("javascript:sendall_cookies_to_evil_host()") is None
    assert validate_source_page_url("//example.com/path") is None
    assert validate_source_page_url("/relative/path") is None


def test_merge_chat_meta_stores_validated_source_page_url() -> None:
    req = _Req(
        app={},
        headers={"User-Agent": "Mozilla/5.0 Chrome/124.0"},
    )
    req.transport = None

    meta = merge_chat_meta(
        {},
        req,
        {"source_page_url": "https://navigator.vbudushee.ru/demo?age=8-10"},
    )
    assert meta["source_page_url"] == "https://navigator.vbudushee.ru/demo?age=8-10"

    next_meta = merge_chat_meta(
        meta,
        req,
        {"source_page_url": "javascript:sendall_cookies_to_evil_host()"},
    )
    assert (
        next_meta["source_page_url"]
        == "https://navigator.vbudushee.ru/demo?age=8-10"
    )


def test_merge_chat_meta_does_not_trust_forwarded_ip_from_untrusted_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(utils.cfg, "trusted_proxy_cidrs", [])
    req = _Req(
        app={},
        headers={"X-Forwarded-For": "203.0.113.50", "User-Agent": "Mozilla/5.0"},
        remote="127.0.0.1",
    )
    req.transport = None

    meta = merge_chat_meta({}, req, {})

    assert meta["ip_address"] == "127.0.0.1"


def test_to_str() -> None:
    assert utils.to_str(["a", "b"]) == "ab"
    assert utils.to_str(None) == "None"


def test_ai_provider_functions() -> None:
    provider, model = resolve_ai_settings("openai", "gpt-4o-mini")
    assert provider.id == "openai"
    assert model.id == "gpt-4o-mini"
    default_provider, default_model = resolve_ai_settings(
        settings.cfg.chat_provider, settings.cfg.chat_model
    )
    assert default_provider.id == settings.cfg.chat_provider
    assert default_model.id == settings.cfg.chat_model
    assert get_provider("openai").id == "openai"


def test_settings_yaml_load_and_validation() -> None:
    import io

    assert settings.YamlLoader is yaml.CSafeLoader
    loaded = settings.yaml_load(
        io.StringIO("flag: true\nraw: !env ${DOES_NOT_EXIST:-x}\n")
    )
    assert loaded["flag"] is True
    assert loaded["raw"] == "x"


def test_settings_env_overrides_use_lowercase_internal_keys() -> None:
    script = """
import json
from vchat.settings import cfg
print(json.dumps({
    "database_uri": cfg.database_uri,
    "cookie_secure": cfg.cookie_secure,
    "mode": cfg.mode,
    "secret_key": cfg.secret_key,
    "cookie_key": cfg.cookie_key,
}))
"""
    env = {
        **os.environ,
        "DATABASE_URI": "postgresql+asyncpg://env/db",
        "COOKIE_SECURE": "false",
        "MODE": "production",
        "SECRET_KEY": "env-secret-key",
        "COOKIE_KEY": "env-cookie-key",
    }

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )

    loaded = pyjson.loads(result.stdout)
    assert loaded == {
        "database_uri": "postgresql+asyncpg://env/db",
        "cookie_secure": False,
        "mode": "production",
        "secret_key": "env-secret-key",
        "cookie_key": "env-cookie-key",
    }


def test_settings_production_rejects_default_security_keys() -> None:
    env = {**os.environ, "MODE": "production"}
    for key in ("SECRET_KEY", "COOKIE_KEY"):
        env.pop(key, None)

    result = subprocess.run(
        [sys.executable, "-c", "import vchat.settings"],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "secret_key" in result.stderr
    assert "cookie_key" in result.stderr


def test_settings_production_rejects_placeholder_security_keys() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import vchat.settings"],
        env={
            **os.environ,
            "MODE": "production",
            "SECRET_KEY": "changed-secret",
            "COOKIE_KEY": "change-me",
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "cookie_key" in result.stderr


def test_settings_rejects_invalid_typed_env_override() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import vchat.settings"],
        env={**os.environ, "RAW_CONTENT_MAX_BYTES": "not-an-int"},
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "Config validation failed" in result.stderr
    assert "raw_content_max_bytes" in result.stderr


def test_settings_rejects_invalid_embedder_instance_count() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import vchat.settings"],
        env={**os.environ, "EMBEDDING_WORKER_INSTANCES": "0"},
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "Config validation failed" in result.stderr
    assert "embedding_worker_instances" in result.stderr


def test_settings_rejects_invalid_embedding_device() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import vchat.settings"],
        env={**os.environ, "EMBEDDING_DEVICE": "metal"},
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "Config validation failed" in result.stderr
    assert "embedding_device" in result.stderr


def test_settings_stage_allows_default_security_keys() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import vchat.settings"],
        env={**os.environ, "MODE": "stage"},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0


def test_settings_production_accepts_overridden_security_keys() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import vchat.settings"],
        env={
            **os.environ,
            "MODE": "production",
            "SECRET_KEY": "changed-secret",
            "COOKIE_KEY": "changed-cookie",
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0


def test_metrics_helpers() -> None:
    assert metrics._safe_label(" GPT-4o Mini/1 ", "fallback") == "GPT-4o Mini/1"
    assert metrics._normalize_guardrail_reason("output_blocked") == "output_blocked"


def test_raw_content_limit_comes_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(indexing_documents.cfg, "raw_content_max_bytes", 3)

    stored, meta = indexing_documents.raw_content_payload(b"abcd")

    assert stored is None
    assert meta["max_size"] == 3
    assert meta["reason"] == "too_big"


def test_raw_content_payload_stores_within_configured_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(indexing_documents.cfg, "raw_content_max_bytes", 4)

    stored, meta = indexing_documents.raw_content_payload(b"abcd")

    assert stored == b"abcd"
    assert meta == {"stored": True, "size": 4}


def test_crawler_queue_collector_uses_broker_db_and_default_and_embeddings_queues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    closed: list[bool] = []

    class _Redis:
        def llen(self, queue_name: str) -> int:
            calls.append(queue_name)
            return {"celery": 7, "embeddings": 11, "crawler": 13}[queue_name]

        def scard(self, key: str) -> int:
            calls.append(key)
            return {"active_chats": 17}[key]

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(metrics.cfg, "celery_redis_uri", "redis://example/")
    monkeypatch.setattr(metrics.cfg, "celery_broker_db", 42)
    monkeypatch.setattr(metrics.cfg, "redis_uri", "redis://app/30")
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
    assert calls[1:] == [
        "celery",
        "embeddings",
        "crawler",
        "url=redis://app/30,decode=False",
        "active_chats",
    ]
    assert samples["vchat_celery_queue_size"] == 7
    assert samples["vchat_embedder_queue_size"] == 11
    assert samples["vchat_crawler_queue_size"] == 13
    assert samples["vchat_active_chats"] == 17
    assert closed == [True, True]
