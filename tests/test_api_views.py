from __future__ import annotations

from types import SimpleNamespace

import msgspec
import pytest
from aiohttp import web
from pydantic import ValidationError

from vchat.settings import REDIS_KEY
from vchat.settings import cfg
from vchat.views.api import views as api_views
from vchat.views.projects.page_status import PageStatus


class _FakeScalarResult:
    def __init__(self, values):
        self._values = values

    def all(self):
        return list(self._values)


class _FakeExecuteResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)

    def scalars(self):
        return _FakeScalarResult(self._rows)


class _FakeDB:
    def __init__(self, rows=None, scalar_value=None):
        self.rows = rows or []
        self.scalar_value = scalar_value
        self.added = []
        self.deleted = []
        self.commits = 0
        self.refresh_count = 0
        self.flush_count = 0

    async def execute(self, stmt, params=None):
        _ = stmt, params
        return _FakeExecuteResult(self.rows)

    async def scalar(self, stmt):
        _ = stmt
        return self.scalar_value

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.commits += 1

    async def flush(self):
        self.flush_count += 1
        for index, obj in enumerate(self.added, start=1):
            if getattr(obj, "id", None) is None:
                obj.id = index

    async def refresh(self, obj):
        _ = obj
        self.refresh_count += 1


class _FakeRequest(dict):
    def __init__(
        self,
        db,
        query=None,
        app=None,
        content_type="application/x-www-form-urlencoded",
    ):
        super().__init__()
        self["db"] = db
        self.query = query or {}
        self.content_type = content_type
        self.app = app or {}

    async def post(self):
        return self.query


class _FakePage:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def meta_dict(self):
        return dict(self.meta or {})

    def patch_meta(self, *, remove=(), **values):
        meta = self.meta_dict()
        for key in remove:
            meta.pop(key, None)
        meta.update(values)
        self.meta = meta
        return meta


class _FakeRedis:
    def __init__(self, *, nonce_claimed=True, rate_allowed=True):
        self.nonce_claimed = nonce_claimed
        self.rate_allowed = rate_allowed
        self.set_calls = []

    async def eval(self, *_args):
        return 1 if self.rate_allowed else 0

    async def set(self, *args, **kwargs):
        self.set_calls.append((args, kwargs))
        return self.nonce_claimed


def _json(resp: web.Response) -> dict:
    import json

    return json.loads(resp.text)


def _auth_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _authenticate(_request, _data):
        return SimpleNamespace(client_id="vchat_test")

    monkeypatch.setattr(api_views, "_authenticate_update_request", _authenticate)


class _TaskRecorder:
    def __init__(self) -> None:
        self.calls = []

    def delay(self, *args):
        self.calls.append(args)


def _signed_payload(secret: str, **overrides) -> dict[str, str]:
    from datetime import datetime, timezone

    data = {
        "url": "https://allowed.com/a",
        "client_id": "vchat_test",
        "timestamp": str(int(datetime.now(timezone.utc).timestamp())),
        "nonce": "nonce-1",
    }
    data.update(overrides)
    data["signature"] = api_views.sign_update_request(secret, **data)
    return data


def test_build_update_signature_payload_uses_sorted_key_value_lines() -> None:
    assert api_views.build_update_signature_payload(
        url="https://allowed.com/a",
        client_id="vchatid-test",
        timestamp="1780640000",
        nonce="nonce-1",
    ) == "\n".join(
        [
            "client_id=vchatid-test",
            "nonce=nonce-1",
            "timestamp=1780640000",
            "url=https://allowed.com/a",
        ]
    )


def test_update_payload_contract_normalizes_and_validates_fields() -> None:
    payload = api_views.UpdatePayload.model_validate(
        _signed_payload("secret-value", url=" https://allowed.com/a ")
    )

    assert payload.url == "https://allowed.com/a"

    with pytest.raises(ValidationError):
        api_views.UpdatePayload.model_validate(_signed_payload("secret-value", url=""))

    with pytest.raises(ValidationError):
        api_views.UpdatePayload.model_validate(
            _signed_payload("secret-value", client_id="x" * 65)
        )

    bad_signature_payload = _signed_payload("secret-value")
    bad_signature_payload["signature"] = "short"
    with pytest.raises(ValidationError):
        api_views.UpdatePayload.model_validate(bad_signature_payload)

    with pytest.raises(ValidationError):
        api_views.UpdatePayload.model_validate(
            _signed_payload("secret-value", timestamp="not-int")
        )


def test_host_helpers() -> None:
    assert api_views._normalize_host(" ExAmPle.COM. ") == "example.com"
    assert api_views._extract_host("https://Sub.Example.com/path") == "sub.example.com"
    assert api_views._extract_host("mailto:a@b.c") is None

    hosts = {"example.com", "docs.example.org"}
    assert api_views._is_host_allowed("example.com", hosts) is True
    assert api_views._is_host_allowed("sub.example.com", hosts) is True
    assert api_views._is_host_allowed("evil.com", hosts) is False


@pytest.mark.asyncio
async def test_authenticate_update_request_accepts_valid_signature() -> None:
    secret = "secret-value"
    client = SimpleNamespace(
        client_id="vchat_test",
        encrypted_secret=api_views.encrypt_client_secret(secret, cfg.secret_key),
        is_active=True,
    )
    db = _FakeDB(scalar_value=client)
    req = _FakeRequest(
        db,
        app={
            REDIS_KEY: _FakeRedis(),
        },
    )
    result = await api_views._authenticate_update_request(req, _signed_payload(secret))
    assert result is client
    assert db.commits == 0
    redis = req.app[REDIS_KEY]
    assert redis.set_calls[0][1]["ex"] == 180


@pytest.mark.asyncio
async def test_authenticate_update_request_rejects_stale_timestamp() -> None:
    req = _FakeRequest(
        _FakeDB(),
        app={
            REDIS_KEY: _FakeRedis(),
        },
    )
    payload = _signed_payload("secret-value", timestamp="1")
    resp = await api_views._authenticate_update_request(req, payload)
    assert resp.status == 400
    body = msgspec.json.decode(resp.body)
    assert body == {
        "status": "error",
        "message": "Validation error",
        "errors": [{"field": "timestamp", "reason": "expired"}],
    }


@pytest.mark.asyncio
async def test_authenticate_update_request_returns_validation_details() -> None:
    req = _FakeRequest(
        _FakeDB(),
        app={
            REDIS_KEY: _FakeRedis(),
        },
    )
    payload = _signed_payload(
        "secret-value",
        url="",
        client_id="x" * 65,
        timestamp="not-int",
    )
    payload["signature"] = "short"

    resp = await api_views._authenticate_update_request(req, payload)

    assert resp.status == 400
    body = msgspec.json.decode(resp.body)
    assert body["status"] == "error"
    assert body["message"] == "Validation error"
    assert body["errors"] == [
        {"field": "url", "reason": "missing"},
        {"field": "client_id", "reason": "too_long"},
        {"field": "timestamp", "reason": "invalid"},
        {"field": "signature", "reason": "invalid"},
    ]


@pytest.mark.asyncio
async def test_authenticate_update_request_rejects_reused_nonce() -> None:
    secret = "secret-value"
    client = SimpleNamespace(
        client_id="vchat_test",
        encrypted_secret=api_views.encrypt_client_secret(secret, cfg.secret_key),
        is_active=True,
    )
    req = _FakeRequest(
        _FakeDB(scalar_value=client),
        app={
            REDIS_KEY: _FakeRedis(nonce_claimed=False),
        },
    )
    resp = await api_views._authenticate_update_request(req, _signed_payload(secret))
    assert resp.status == 401
    assert "Nonce has already been used" in resp.text


@pytest.mark.asyncio
async def test_authenticate_update_request_rejects_rate_limit() -> None:
    secret = "secret-value"
    client = SimpleNamespace(
        client_id="vchat_test",
        encrypted_secret=api_views.encrypt_client_secret(secret, cfg.secret_key),
        is_active=True,
    )
    req = _FakeRequest(
        _FakeDB(scalar_value=client),
        app={
            REDIS_KEY: _FakeRedis(rate_allowed=False),
        },
    )
    resp = await api_views._authenticate_update_request(req, _signed_payload(secret))
    assert resp.status == 429
    assert "Rate limit exceeded" in resp.text


@pytest.mark.asyncio
async def test_read_update_payload_rejects_json_content_type() -> None:
    req = _FakeRequest(
        _FakeDB(),
        query={"url": "https://allowed.com/a"},
        content_type="application/json",
    )
    with pytest.raises(web.HTTPUnsupportedMediaType):
        await api_views._read_update_payload(req)


@pytest.mark.asyncio
async def test_get_source_hosts_filters_invalid_urls() -> None:
    db = _FakeDB(
        rows=[(1, "https://example.com/sitemap.xml"), (2, "not a url"), (3, None)]
    )
    req = _FakeRequest(db)
    result = await api_views._get_source_hosts(req, SimpleNamespace(id=7))
    assert result == [(1, "example.com")]


@pytest.mark.asyncio
async def test_get_source_hosts_uses_client_sources_only() -> None:
    db = _FakeDB(rows=[(1, "https://allowed.com/docs"), (2, "not a url")])
    req = _FakeRequest(db)
    client = SimpleNamespace(id=7)

    result = await api_views._get_source_hosts(req, client)

    assert result == [(1, "allowed.com")]


@pytest.mark.asyncio
async def test_update_document_missing_or_invalid_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeDB()
    assert (await api_views.update_document(_FakeRequest(db, query={}))).status == 400
    _auth_ok(monkeypatch)
    assert (
        await api_views.update_document(
            _FakeRequest(db, query={"url": "ftp://example.com"})
        )
    ).status == 400


@pytest.mark.asyncio
async def test_update_document_creates_page_and_queues_crawl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeDB()
    req = _FakeRequest(db, query={"url": "https://allowed.com/a"})
    _auth_ok(monkeypatch)
    task = _TaskRecorder()
    monkeypatch.setattr(api_views, "crawl_page_task", task)

    async def _hosts(_request, _client):
        return [(55, "allowed.com")]

    monkeypatch.setattr(api_views, "_get_source_hosts", _hosts)

    resp = await api_views.update_document(req)
    payload = _json(resp)
    assert resp.status == 202
    assert payload["action"] == "queued"
    assert len(db.added) == 1
    created = db.added[0]
    assert created.id == 1
    assert created.uri == "https://allowed.com/a"
    assert created.source_id == 55
    assert created.status == PageStatus.crawler
    assert created.status_error is None
    assert created.discover_by == "api"
    assert created.discover_source == "vchat_test"
    assert created.meta["force_reprocess_once"] is True
    assert created._hash == ""
    assert db.commits == 1
    assert task.calls == [(1,)]


@pytest.mark.asyncio
async def test_update_document_requeues_existing_page_for_crawl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _FakePage(
        id=42,
        source_id=1,
        uri="https://allowed.com/a",
        status=PageStatus.ready,
        status_error="http_4xx",
        discover_by=None,
        discover_source=None,
        meta={"error": "old", "doc_type": "html"},
    )
    db = _FakeDB(scalar_value=page)
    req = _FakeRequest(db, query={"url": "https://allowed.com/a"})
    _auth_ok(monkeypatch)
    task = _TaskRecorder()
    monkeypatch.setattr(api_views, "crawl_page_task", task)

    async def _hosts(_request, _client):
        return [(7, "allowed.com")]

    monkeypatch.setattr(api_views, "_get_source_hosts", _hosts)

    resp = await api_views.update_document(req)

    assert resp.status == 202
    assert db.added == []
    assert page.source_id == 7
    assert page.status == PageStatus.crawler
    assert page.status_error is None
    assert page.discover_by == "api"
    assert page.discover_source == "vchat_test"
    assert page.meta == {"doc_type": "html", "force_reprocess_once": True}
    assert db.commits == 1
    assert task.calls == [(42,)]


@pytest.mark.asyncio
async def test_update_document_rejects_forbidden_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeDB()
    req = _FakeRequest(db, query={"url": "https://forbidden.example.com/a"})
    _auth_ok(monkeypatch)
    task = _TaskRecorder()
    monkeypatch.setattr(api_views, "crawl_page_task", task)

    async def _hosts(_request, _client):
        return [(1, "allowed.com")]

    monkeypatch.setattr(api_views, "_get_source_hosts", _hosts)

    resp = await api_views.update_document(req)

    assert resp.status == 403
    assert task.calls == []
