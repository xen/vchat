from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiohttp import web
from yarl import URL

from vchat.app_keys import SIGNER_KEY
from vchat.views.projects import views as project_views


class _Signer:
    def loads(self, token, max_age=86400):
        _ = token, max_age
        return 1


class _Route:
    def __init__(self, path: str):
        self.path = path

    def url_for(self, **kwargs):
        return URL(self.path.format(**kwargs) if kwargs else self.path)


class _App(dict):
    def __init__(self):
        super().__init__({SIGNER_KEY: _Signer()})
        self.router = {
            "users": _Route("/users/"),
            "actions": _Route("/actions/{action}/{item_id}"),
        }


class _Request(dict):
    def __init__(
        self,
        *,
        action: str,
        item_id: str = "1",
        method="POST",
        post_data=None,
        headers=None,
    ):
        super().__init__()
        self.app = _App()
        self.match_info = {"action": action, "item_id": item_id}
        self.method = method
        self.headers = headers if headers is not None else {"X-CSRFToken": "ok"}
        self._post_data = post_data or {}

    async def post(self):
        return self._post_data


class _DB:
    def __init__(self, *, scalar_values=None, execute_rows=None):
        self.scalar_values = list(scalar_values or [])
        self.execute_rows = list(execute_rows or [])
        self.deleted = []
        self.added = []
        self.commits = 0

    async def scalar(self, stmt):
        _ = stmt
        if self.scalar_values:
            return self.scalar_values.pop(0)
        return None

    async def execute(self, stmt):
        _ = stmt
        rows = self.execute_rows.pop(0) if self.execute_rows else []
        return SimpleNamespace(
            all=lambda: rows,
            scalars=lambda: SimpleNamespace(all=lambda: rows),
        )

    async def delete(self, obj):
        self.deleted.append(obj)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def _raw_project_action():
    return project_views.project_action.__wrapped__


@pytest.mark.asyncio
async def test_project_action_rejects_missing_csrf() -> None:
    req = _Request(action="delete_source", headers={})
    req["db"] = _DB()
    req["user"] = SimpleNamespace(id=1)
    with pytest.raises(web.HTTPForbidden):
        await _raw_project_action()(req)


@pytest.mark.asyncio
async def test_project_action_ignore_document_toggle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = SimpleNamespace(id=10, is_ignored=False)
    req = _Request(action="ignore_document", item_id="10", post_data={})
    req["db"] = _DB(scalar_values=[doc])
    req["user"] = SimpleNamespace(id=1)

    resp = await _raw_project_action()(req)
    assert resp.status == 200
    assert doc.is_ignored is True
    assert resp.headers["HX-Trigger"] == "project-documents:refresh"


@pytest.mark.asyncio
async def test_project_action_delete_document(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = SimpleNamespace(id=11)
    req = _Request(action="delete_document", item_id="11")
    db = _DB(scalar_values=[doc])
    req["db"] = db
    req["user"] = SimpleNamespace(id=1)

    resp = await _raw_project_action()(req)
    assert resp.status == 200
    assert db.deleted == [doc]
    assert db.commits == 1


@pytest.mark.asyncio
async def test_project_action_delete_file_not_found() -> None:
    req = _Request(action="delete_file", item_id="999")
    req["db"] = _DB(scalar_values=[None])
    req["user"] = SimpleNamespace(id=1)

    with pytest.raises(web.HTTPNotFound):
        await _raw_project_action()(req)


@pytest.mark.asyncio
async def test_project_action_unknown_action() -> None:
    req = _Request(action="unknown", item_id="1")
    req["db"] = _DB()
    req["user"] = SimpleNamespace(id=1)

    with pytest.raises(web.HTTPBadRequest):
        await _raw_project_action()(req)


@pytest.mark.asyncio
async def test_project_action_delete_source_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = SimpleNamespace(id=3)
    req = _Request(action="delete_source", item_id="3")
    db = _DB(scalar_values=[source])
    req["db"] = db
    req["user"] = SimpleNamespace(id=1)
    events = []

    async def _admin(event, request):
        _ = request
        events.append(event)

    monkeypatch.setattr(project_views, "admin_event", _admin)
    resp = await _raw_project_action()(req)
    assert resp.status == 200
    assert db.deleted == [source]
    assert events == ["source_delete"]


@pytest.mark.asyncio
async def test_project_action_background_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    req = _Request(action="crawl_all", item_id="global")
    req["db"] = _DB()
    req["user"] = SimpleNamespace(id=1)

    called = []

    async def _flash(request, msg, cat="success"):
        _ = request, msg, cat
        called.append("flash")

    monkeypatch.setattr(project_views, "flash", _flash)
    monkeypatch.setattr(
        project_views.crawl_all_sources_task,
        "delay",
        lambda: called.append("crawl_all"),
    )
    monkeypatch.setattr(
        project_views,
        "schedule_refresh_project_index",
        lambda: called.append("refresh_project_index"),
    )
    monkeypatch.setattr(
        project_views.index_project, "delay", lambda: called.append("index_project")
    )

    resp1 = await _raw_project_action()(req)
    assert resp1.status == 200

    req.match_info["action"] = "refresh_project_index"
    resp2 = await _raw_project_action()(req)
    assert resp2.status == 200

    req.match_info["action"] = "index_project"
    resp3 = await _raw_project_action()(req)
    assert resp3.status == 200
    assert (
        "crawl_all" in called
        and "refresh_project_index" in called
        and "index_project" in called
    )


@pytest.mark.asyncio
async def test_project_action_refresh_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = SimpleNamespace(
        id=17,
        source_id=3,
        uri="https://example.com/page",
        meta={"foo": "bar"},
        updated_at=None,
    )
    req = _Request(action="refresh_page", item_id="17")
    db = _DB(scalar_values=[document])
    req["db"] = db
    req["user"] = SimpleNamespace(id=1)

    called = []
    events = []

    async def _flash(request, msg, cat="success"):
        _ = request, msg, cat
        called.append("flash")

    async def _admin(event, request):
        _ = request
        events.append(event)

    monkeypatch.setattr(project_views, "flash", _flash)
    monkeypatch.setattr(project_views, "admin_event", _admin)
    monkeypatch.setattr(
        project_views.crawl_page_task,
        "delay",
        lambda page_id: called.append(("crawl_page", page_id)),
    )

    resp = await _raw_project_action()(req)
    assert resp.status == 200
    assert document.meta["force_reprocess_once"] is True
    assert db.commits == 1
    assert ("crawl_page", 17) in called
    assert "flash" in called
    assert events == ["page_refresh_request"]


@pytest.mark.asyncio
async def test_project_action_refresh_page_rejects_non_source_page() -> None:
    document = SimpleNamespace(id=17, source_id=None, uri=None, meta={})
    req = _Request(action="refresh_page", item_id="17")
    req["db"] = _DB(scalar_values=[document])
    req["user"] = SimpleNamespace(id=1)

    with pytest.raises(web.HTTPBadRequest):
        await _raw_project_action()(req)


@pytest.mark.asyncio
async def test_project_action_rebuild_uploads(monkeypatch: pytest.MonkeyPatch) -> None:
    req = _Request(action="rebuild_uploads", item_id="global")
    db = _DB(execute_rows=[[1, 2, 3]])
    req["db"] = db
    req["user"] = SimpleNamespace(id=1)

    delayed = []

    async def _flash(request, msg, cat="success"):
        _ = request, msg, cat

    monkeypatch.setattr(project_views, "flash", _flash)
    monkeypatch.setattr(
        project_views.crawl_file_task,
        "delay",
        lambda document_id: delayed.append(document_id),
    )

    resp = await _raw_project_action()(req)
    assert resp.status == 200
    assert delayed == [1, 2, 3]


@pytest.mark.asyncio
async def test_project_action_delete_file_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = SimpleNamespace(id=99, uri="/tmp/fake.bin")
    req = _Request(action="delete_file", item_id="99")
    db = _DB(scalar_values=[doc])
    req["db"] = db
    req["user"] = SimpleNamespace(id=1)

    events = []

    async def _admin(event, request):
        _ = request
        events.append(event)

    monkeypatch.setattr(project_views, "admin_event", _admin)
    monkeypatch.setattr(project_views.os.path, "exists", lambda p: False)
    resp = await _raw_project_action()(req)
    assert resp.status == 200
    assert db.deleted == [doc]
    assert events == ["file_delete"]


@pytest.mark.asyncio
async def test_project_documents_json_serializes_rows() -> None:
    source = SimpleNamespace(id=2, title="Source A", uri="https://example.com")
    doc = SimpleNamespace(
        id=5,
        title="Doc A",
        uri="https://example.com/a",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        status="indexed",
        index_status="indexed",
        is_ignored=False,
        meta={"doc_type": "html"},
    )
    req = _Request(action="noop")
    req["db"] = _DB(
        execute_rows=[
            [
                (
                    doc.id,
                    doc.title,
                    doc.uri,
                    doc.status,
                    doc.index_status,
                    doc.is_ignored,
                    source.title,
                    source.uri,
                    123,
                    2,
                )
            ]
        ]
    )
    req["user"] = SimpleNamespace(id=1)
    raw = project_views.project_documents_json.__wrapped__
    resp = await raw(req)
    assert resp.status == 200
    assert '"id": "5"' in resp.text
    assert '"source": "Source A"' in resp.text
    assert '"meta"' not in resp.text
    assert '"uri": "https://example.com/a"' in resp.text
    assert '"created_at"' not in resp.text
    assert '"updated_at"' not in resp.text
    assert '"document_type"' not in resp.text


@pytest.mark.asyncio
async def test_project_documents_json_marks_low_content_as_ignored() -> None:
    source = SimpleNamespace(id=2, title="Source A", uri="https://example.com")
    now = datetime.now(timezone.utc)
    req = _Request(action="noop")
    req["db"] = _DB(
        execute_rows=[
            [
                (
                    6,
                    "Thin page",
                    "https://example.com/thin",
                    "low_content",
                    None,
                    False,
                    source.title,
                    source.uri,
                    119,
                    0,
                )
            ]
        ]
    )
    req["user"] = SimpleNamespace(id=1)

    raw = project_views.project_documents_json.__wrapped__
    resp = await raw(req)
    assert resp.status == 200
    assert '"status": "low_content"' in resp.text
    assert '"is_ignored": true' in resp.text


@pytest.mark.asyncio
async def test_project_files_json_serializes_rows() -> None:
    doc = SimpleNamespace(
        id=8,
        title="file.pdf",
        created_at=datetime.now(timezone.utc),
        meta={"filename": "file.pdf", "doc_type": "pdf"},
    )
    req = _Request(action="noop")
    req["db"] = _DB(execute_rows=[[(doc, 444, 3)]])
    req["user"] = SimpleNamespace(id=1)

    raw = project_views.project_files_json.__wrapped__
    resp = await raw(req)
    assert resp.status == 200
    assert '"id": "8"' in resp.text
    assert '"document_type": "pdf"' in resp.text
