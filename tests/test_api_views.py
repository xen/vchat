from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from aiohttp import web

from vchat.settings import CONFIG_KEY, REDIS_KEY
from vchat.settings import config
from vchat.views.api import views as api_views
from jobs.documents.content import content_sha256
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


class _FakeDocument:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    @property
    def hash_value(self):
        return self._hash

    @hash_value.setter
    def hash_value(self, value):
        self._hash = content_sha256(value)


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


def test_host_helpers() -> None:
    assert api_views._normalize_host(" ExAmPle.COM. ") == "example.com"
    assert api_views._extract_host("https://Sub.Example.com/path") == "sub.example.com"
    assert api_views._extract_host("mailto:a@b.c") is None

    hosts = {"example.com", "docs.example.org"}
    assert api_views._is_host_allowed("example.com", hosts) is True
    assert api_views._is_host_allowed("sub.example.com", hosts) is True
    assert api_views._is_host_allowed("evil.com", hosts) is False

    rows = [(10, "example.com"), (11, "docs.example.org")]
    assert api_views._pick_source_for_host("example.com", rows) == 10
    assert api_views._pick_source_for_host("sub.docs.example.org", rows) == 11
    assert api_views._pick_source_for_host("nope.local", rows) is None


@pytest.mark.asyncio
async def test_authenticate_update_request_accepts_valid_signature() -> None:
    secret = "secret-value"
    client = SimpleNamespace(
        client_id="vchat_test",
        encrypted_secret=api_views.encrypt_client_secret(secret, config["secret_key"]),
        is_active=True,
    )
    db = _FakeDB(scalar_value=client)
    req = _FakeRequest(
        db,
        app={
            CONFIG_KEY: config,
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
            CONFIG_KEY: config,
            REDIS_KEY: _FakeRedis(),
        },
    )
    payload = _signed_payload("secret-value", timestamp="1")
    resp = await api_views._authenticate_update_request(req, payload)
    assert resp.status == 401
    assert "Timestamp is too old" in resp.text


@pytest.mark.asyncio
async def test_authenticate_update_request_rejects_reused_nonce() -> None:
    secret = "secret-value"
    client = SimpleNamespace(
        client_id="vchat_test",
        encrypted_secret=api_views.encrypt_client_secret(secret, config["secret_key"]),
        is_active=True,
    )
    req = _FakeRequest(
        _FakeDB(scalar_value=client),
        app={
            CONFIG_KEY: config,
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
        encrypted_secret=api_views.encrypt_client_secret(secret, config["secret_key"]),
        is_active=True,
    )
    req = _FakeRequest(
        _FakeDB(scalar_value=client),
        app={
            CONFIG_KEY: config,
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
    req = _FakeRequest(db=db)
    result = await api_views._get_source_hosts(req)
    assert result == [(1, "example.com")]


@pytest.mark.asyncio
async def test_get_source_hosts_uses_client_sources_only() -> None:
    req = _FakeRequest(
        db=_FakeDB(rows=[(1, "https://allowed.com/docs"), (2, "not a url")])
    )
    client = SimpleNamespace(id=7)

    result = await api_views._get_source_hosts(req, client)

    assert result == [(1, "allowed.com")]


@pytest.mark.asyncio
async def test_upsert_document_creates_and_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeDB(scalar_value=None)
    req = _FakeRequest(db=db)

    async def _extract_content(url: str):
        assert urlparse(url).scheme in {"http", "https"}
        return (
            "content",
            {"content_type": "text/html"},
            "Doc title",
            b"<html>content</html>",
            "text/html",
        )

    delayed = []

    def _schedule(doc_id):
        delayed.append(doc_id)
        return True

    monkeypatch.setattr(api_views, "_extract_content", _extract_content)
    monkeypatch.setattr(api_views, "schedule_index_document", _schedule)
    monkeypatch.setattr(api_views, "guess_document_type", lambda url, ct: "html")

    status, _doc_id = await api_views._upsert_document(
        req,
        source_id=1,
        url="https://example.com/a",
        discover_source="vchat_test",
    )
    assert status == "indexed"
    assert db.added
    created_doc = db.added[0]
    assert created_doc.title == "Doc title"
    assert created_doc.raw_content == b"<html>content</html>"
    assert created_doc.raw_content_size == len(b"<html>content</html>")
    assert created_doc.raw_content_type == "text/html"
    assert created_doc.meta["doc_type"] == "html"
    assert created_doc.discover_by == "api"
    assert created_doc.discover_source == "vchat_test"
    assert created_doc.status == PageStatus.parsing
    assert not hasattr(created_doc, "index_status")
    assert db.commits == 1
    assert db.refresh_count == 1
    assert delayed


@pytest.mark.asyncio
async def test_upsert_document_skips_reindex_for_unchanged_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _FakeDocument(
        id=12,
        source_id=1,
        uri="https://example.com/a",
        content="same",
        _hash=content_sha256("same"),
        meta={"doc_type": "html"},
        title="Doc title",
    )
    db = _FakeDB(scalar_value=document)
    req = _FakeRequest(db=db)

    async def _extract_content(url: str):
        assert urlparse(url).scheme in {"http", "https"}
        return (
            "same",
            {"content_type": "text/html"},
            "Doc title",
            b"<html>same</html>",
            "text/html",
        )

    delayed = []

    async def _has_chunks(db, doc_id):
        _ = db, doc_id
        return True

    monkeypatch.setattr(api_views, "_extract_content", _extract_content)
    monkeypatch.setattr(
        api_views,
        "schedule_index_document",
        lambda doc_id: delayed.append(doc_id) or True,
    )
    monkeypatch.setattr(api_views, "guess_document_type", lambda url, ct: "html")
    monkeypatch.setattr(api_views, "async_document_has_chunks", _has_chunks)

    status, doc_id = await api_views._upsert_document(
        req, source_id=1, url="https://example.com/a"
    )
    assert status == "indexed"
    assert doc_id == 12
    assert document.status == PageStatus.ready
    assert not hasattr(document, "index_status")
    assert db.commits == 1
    assert db.refresh_count == 1
    assert delayed == []


@pytest.mark.asyncio
async def test_upsert_document_skips_reindex_for_near_duplicate_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_content = "\n".join(
        [
            "# Title",
            "Body line 1",
            "Body line 2",
            "Body line 3",
            "Published: 2026-05-29",
        ]
    )
    new_content = "\n".join(
        [
            "# Title",
            "Body line 1",
            "Body line 2",
            "Body line 3",
            "Published: 2026-05-30",
        ]
    )
    document = _FakeDocument(
        id=13,
        source_id=1,
        uri="https://example.com/a",
        content=previous_content,
        _hash=content_sha256(previous_content),
        meta={"doc_type": "html"},
        title="Doc title",
        status="indexed",
        language="",
        length=len(previous_content),
    )
    db = _FakeDB(scalar_value=document)
    req = _FakeRequest(db=db)

    async def _extract_content(url: str):
        assert urlparse(url).scheme in {"http", "https"}
        return (
            new_content,
            {"content_type": "text/html"},
            "Doc title",
            b"<html>new</html>",
            "text/html",
        )

    delayed = []

    async def _has_chunks(db, doc_id):
        _ = db, doc_id
        return True

    monkeypatch.setattr(api_views, "_extract_content", _extract_content)
    monkeypatch.setattr(
        api_views,
        "schedule_index_document",
        lambda doc_id: delayed.append(doc_id) or True,
    )
    monkeypatch.setattr(api_views, "guess_document_type", lambda url, ct: "html")
    monkeypatch.setattr(api_views, "async_document_has_chunks", _has_chunks)

    status, doc_id = await api_views._upsert_document(
        req, source_id=1, url="https://example.com/a"
    )
    assert status == "indexed"
    assert doc_id == 13
    assert document.content == new_content
    assert document.hash_value == content_sha256(new_content)
    assert document.status == PageStatus.ready
    assert not hasattr(document, "index_status")
    assert delayed == []


@pytest.mark.asyncio
async def test_delete_document_by_url(monkeypatch: pytest.MonkeyPatch) -> None:
    docs = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
    db = _FakeDB(rows=docs)
    req = _FakeRequest(db=db)
    deleted = await api_views._delete_document_by_url(req, "https://example.com/x")
    assert deleted == 2
    assert len(db.deleted) == 2
    assert db.commits == 1


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
async def test_update_document_domain_forbidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeDB()
    req = _FakeRequest(db, query={"url": "https://forbidden.example.com/a"})
    _auth_ok(monkeypatch)

    async def _hosts(request, client):
        _ = request, client
        return [(1, "allowed.com")]

    monkeypatch.setattr(api_views, "_get_source_hosts", _hosts)
    resp = await api_views.update_document(req)
    assert resp.status == 403
    assert "Domain is not allowed" in resp.text


@pytest.mark.asyncio
async def test_update_document_404_deletes(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeDB()
    req = _FakeRequest(db, query={"url": "https://allowed.com/a"})
    _auth_ok(monkeypatch)

    async def _hosts(request, client):
        _ = request, client
        return [(1, "allowed.com")]

    async def _state(url):
        _ = url
        return 404, None, 404

    monkeypatch.setattr(api_views, "_get_source_hosts", _hosts)
    monkeypatch.setattr(api_views, "_resolve_url_state", _state)
    called = []

    async def _delete(request, url):
        called.append((request, url))
        return 1

    monkeypatch.setattr(api_views, "_delete_document_by_url", _delete)
    resp = await api_views.update_document(req)
    payload = _json(resp)
    assert resp.status == 200
    assert payload["action"] == "deleted"
    assert called


@pytest.mark.asyncio
async def test_update_document_redirect_forbidden_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeDB()
    req = _FakeRequest(db, query={"url": "https://allowed.com/a"})
    _auth_ok(monkeypatch)

    async def _hosts(request, client):
        _ = request, client
        return [(1, "allowed.com")]

    async def _state(url):
        _ = url
        return 302, "https://evil.com/redirected", 200

    monkeypatch.setattr(api_views, "_get_source_hosts", _hosts)
    monkeypatch.setattr(api_views, "_resolve_url_state", _state)
    resp = await api_views.update_document(req)
    assert resp.status == 403


def test_redirect_target_policy_stays_within_current_domain() -> None:
    assert api_views._redirect_target_allowed(
        "https://allowed.com/a",
        "https://allowed.com/b",
    )
    assert api_views._redirect_target_allowed(
        "https://allowed.com/a",
        "https://www.allowed.com/b",
    )
    assert api_views._redirect_target_allowed(
        "https://docs.allowed.com/a",
        "https://allowed.com/b",
    )
    assert not api_views._redirect_target_allowed(
        "https://allowed.com/a",
        "https://evil.com/b",
    )
    assert not api_views._redirect_target_allowed(
        "https://allowed.com/a",
        "javascript:alert(1)",
    )


class _FakeFetchContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, _size: int):
        for chunk in self._chunks:
            yield chunk


class _FakeFetchResponse:
    def __init__(
        self,
        *,
        url: str,
        status: int,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
        chunks: list[bytes] | None = None,
    ) -> None:
        self.url = url
        self.status = status
        self.headers = headers or {}
        self.charset = "utf-8"
        self.content = _FakeFetchContent(chunks if chunks is not None else [body])

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        _ = exc_type, exc, tb
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise AssertionError(f"unexpected status {self.status}")


class _FakeFetchClient:
    def __init__(self, responses: dict[str, _FakeFetchResponse]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, bool]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        _ = exc_type, exc, tb
        return False

    def get(self, url, *, allow_redirects, headers):
        _ = headers
        self.requests.append((url, allow_redirects))
        return self.responses[url]


@pytest.mark.asyncio
async def test_fetch_url_content_follows_same_domain_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeFetchClient(
        {
            "https://allowed.com/a": _FakeFetchResponse(
                url="https://allowed.com/a",
                status=302,
                headers={"Location": "/b"},
            ),
            "https://allowed.com/b": _FakeFetchResponse(
                url="https://allowed.com/b",
                status=200,
                headers={"Content-Type": "text/html"},
                body=b"<html>ok</html>",
            ),
        }
    )
    monkeypatch.setattr(api_views, "ClientSession", lambda timeout: client)

    body, content_type, raw_body, _headers = await api_views._fetch_url_content(
        "https://allowed.com/a"
    )

    assert body == "<html>ok</html>"
    assert content_type == "text/html"
    assert raw_body == b"<html>ok</html>"
    assert client.requests == [
        ("https://allowed.com/a", False),
        ("https://allowed.com/b", False),
    ]


@pytest.mark.asyncio
async def test_fetch_url_content_blocks_cross_domain_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeFetchClient(
        {
            "https://allowed.com/a": _FakeFetchResponse(
                url="https://allowed.com/a",
                status=302,
                headers={"Location": "https://evil.com/private"},
            ),
            "https://evil.com/private": _FakeFetchResponse(
                url="https://evil.com/private",
                status=200,
                body=b"secret",
            ),
        }
    )
    monkeypatch.setattr(api_views, "ClientSession", lambda timeout: client)

    with pytest.raises(web.HTTPForbidden):
        await api_views._fetch_url_content("https://allowed.com/a")

    assert client.requests == [("https://allowed.com/a", False)]


@pytest.mark.asyncio
async def test_fetch_url_content_rejects_oversized_content_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeFetchClient(
        {
            "https://allowed.com/a": _FakeFetchResponse(
                url="https://allowed.com/a",
                status=200,
                headers={"Content-Length": "6"},
                body=b"ok",
            ),
        }
    )
    monkeypatch.setattr(api_views, "ClientSession", lambda timeout: client)

    with pytest.raises(web.HTTPRequestEntityTooLarge):
        await api_views._fetch_url_content("https://allowed.com/a", max_bytes=5)


@pytest.mark.asyncio
async def test_fetch_url_content_rejects_chunked_body_over_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeFetchClient(
        {
            "https://allowed.com/a": _FakeFetchResponse(
                url="https://allowed.com/a",
                status=200,
                chunks=[b"123", b"456"],
            ),
        }
    )
    monkeypatch.setattr(api_views, "ClientSession", lambda timeout: client)

    with pytest.raises(web.HTTPRequestEntityTooLarge):
        await api_views._fetch_url_content("https://allowed.com/a", max_bytes=5)


@pytest.mark.asyncio
async def test_resolve_url_state_blocks_cross_domain_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeFetchClient(
        {
            "https://allowed.com/a": _FakeFetchResponse(
                url="https://allowed.com/a",
                status=302,
                headers={"Location": "https://evil.com/private"},
            ),
            "https://evil.com/private": _FakeFetchResponse(
                url="https://evil.com/private",
                status=200,
                body=b"secret",
            ),
        }
    )
    monkeypatch.setattr(api_views, "ClientSession", lambda timeout: client)

    with pytest.raises(web.HTTPForbidden):
        await api_views._resolve_url_state("https://allowed.com/a")

    assert client.requests == [("https://allowed.com/a", False)]


@pytest.mark.asyncio
async def test_update_document_redirect_replace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeDB()
    req = _FakeRequest(db, query={"url": "https://allowed.com/a"})
    _auth_ok(monkeypatch)

    async def _hosts(request, client):
        _ = request, client
        return [(1, "allowed.com")]

    async def _state(url):
        _ = url
        return 301, "https://allowed.com/new", 200

    monkeypatch.setattr(api_views, "_get_source_hosts", _hosts)
    monkeypatch.setattr(api_views, "_resolve_url_state", _state)
    deleted = []
    upserted = []

    async def _delete(request, url):
        deleted.append(url)
        return 1

    async def _upsert(request, source_id, url, **kwargs):
        upserted.append((source_id, url, kwargs))
        return ("indexed", 10)

    monkeypatch.setattr(api_views, "_delete_document_by_url", _delete)
    monkeypatch.setattr(api_views, "_upsert_document", _upsert)
    resp = await api_views.update_document(req)
    payload = _json(resp)
    assert payload["action"] == "replaced"
    assert deleted == ["https://allowed.com/a"]
    assert upserted == [
        (1, "https://allowed.com/new", {"discover_source": "vchat_test"})
    ]


@pytest.mark.asyncio
async def test_update_document_success_indexed(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeDB()
    req = _FakeRequest(db, query={"url": "https://allowed.com/a"})
    _auth_ok(monkeypatch)

    async def _hosts(request, client):
        _ = request, client
        return [(55, "allowed.com")]

    async def _state(url):
        _ = url
        return 200, None, 200

    monkeypatch.setattr(api_views, "_get_source_hosts", _hosts)
    monkeypatch.setattr(api_views, "_resolve_url_state", _state)
    calls = []

    async def _upsert(request, source_id, url, **kwargs):
        calls.append((source_id, url, kwargs))
        return ("indexed", 999)

    monkeypatch.setattr(api_views, "_upsert_document", _upsert)
    resp = await api_views.update_document(req)
    payload = _json(resp)
    assert payload["action"] == "indexed"
    assert calls == [
        (55, "https://allowed.com/a", {"discover_source": "vchat_test"})
    ]
