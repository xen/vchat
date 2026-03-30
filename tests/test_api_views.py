from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from aiohttp import web

from vchat.views.api import views as api_views


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

    async def execute(self, stmt):
        _ = stmt
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

    async def refresh(self, obj):
        _ = obj
        self.refresh_count += 1


class _FakeRequest(dict):
    def __init__(self, db, query=None):
        super().__init__()
        self["db"] = db
        self.query = query or {}


def _json(resp: web.Response) -> dict:
    import json

    return json.loads(resp.text)


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
async def test_get_source_hosts_filters_invalid_urls() -> None:
    db = _FakeDB(rows=[(1, "https://example.com/sitemap.xml"), (2, "not a url"), (3, None)])
    req = _FakeRequest(db=db)
    result = await api_views._get_source_hosts(req)
    assert result == [(1, "example.com")]


@pytest.mark.asyncio
async def test_upsert_document_creates_and_indexes(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeDB(scalar_value=None)
    req = _FakeRequest(db=db)

    async def _extract_content(url: str):
        assert urlparse(url).scheme in {"http", "https"}
        return "content", {"content_type": "text/html"}, "Doc title"

    delayed = []

    class _Delay:
        @staticmethod
        def delay(doc_id):
            delayed.append(doc_id)

    monkeypatch.setattr(api_views, "_extract_content", _extract_content)
    monkeypatch.setattr(api_views, "index_document", _Delay)
    monkeypatch.setattr(api_views, "guess_document_type", lambda url, ct: "html")

    status, _doc_id = await api_views._upsert_document(req, source_id=1, url="https://example.com/a")
    assert status == "indexed"
    assert db.added
    created_doc = db.added[0]
    assert created_doc.title == "Doc title"
    assert created_doc.meta["doc_type"] == "html"
    assert db.commits == 1
    assert db.refresh_count == 1
    assert delayed


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
async def test_update_document_missing_or_invalid_url() -> None:
    db = _FakeDB()
    assert (await api_views.update_document(_FakeRequest(db, query={}))).status == 400
    assert (await api_views.update_document(_FakeRequest(db, query={"url": "ftp://example.com"}))).status == 400


@pytest.mark.asyncio
async def test_update_document_domain_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeDB()
    req = _FakeRequest(db, query={"url": "https://forbidden.example.com/a"})

    async def _hosts(request):
        _ = request
        return [(1, "allowed.com")]

    monkeypatch.setattr(api_views, "_get_source_hosts", _hosts)
    resp = await api_views.update_document(req)
    assert resp.status == 403
    assert "Domain is not allowed" in resp.text


@pytest.mark.asyncio
async def test_update_document_404_deletes(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeDB()
    req = _FakeRequest(db, query={"url": "https://allowed.com/a"})

    async def _hosts(request):
        _ = request
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
async def test_update_document_redirect_forbidden_target(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeDB()
    req = _FakeRequest(db, query={"url": "https://allowed.com/a"})

    async def _hosts(request):
        _ = request
        return [(1, "allowed.com")]

    async def _state(url):
        _ = url
        return 302, "https://evil.com/redirected", 200

    monkeypatch.setattr(api_views, "_get_source_hosts", _hosts)
    monkeypatch.setattr(api_views, "_resolve_url_state", _state)
    resp = await api_views.update_document(req)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_update_document_redirect_replace(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeDB()
    req = _FakeRequest(db, query={"url": "https://allowed.com/a"})

    async def _hosts(request):
        _ = request
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

    async def _upsert(request, source_id, url):
        upserted.append((source_id, url))
        return ("indexed", 10)

    monkeypatch.setattr(api_views, "_delete_document_by_url", _delete)
    monkeypatch.setattr(api_views, "_upsert_document", _upsert)
    resp = await api_views.update_document(req)
    payload = _json(resp)
    assert payload["action"] == "replaced"
    assert deleted == ["https://allowed.com/a"]
    assert upserted == [(1, "https://allowed.com/new")]


@pytest.mark.asyncio
async def test_update_document_success_indexed(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeDB()
    req = _FakeRequest(db, query={"url": "https://allowed.com/a"})

    async def _hosts(request):
        _ = request
        return [(55, "allowed.com")]

    async def _state(url):
        _ = url
        return 200, None, 200

    monkeypatch.setattr(api_views, "_get_source_hosts", _hosts)
    monkeypatch.setattr(api_views, "_resolve_url_state", _state)
    calls = []

    async def _upsert(request, source_id, url):
        calls.append((source_id, url))
        return ("indexed", 999)

    monkeypatch.setattr(api_views, "_upsert_document", _upsert)
    resp = await api_views.update_document(req)
    payload = _json(resp)
    assert payload["action"] == "indexed"
    assert calls == [(55, "https://allowed.com/a")]
