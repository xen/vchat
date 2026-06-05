from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiohttp import web
from multidict import MultiDict
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
            "project_triggers": _Route("/triggers"),
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


def _raw_project_triggers():
    view = project_views.project_triggers
    while hasattr(view, "__wrapped__"):
        view = view.__wrapped__
    return view


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
    from vchat.page_status import PageStatus, PageStatusError

    doc = SimpleNamespace(id=10, status=PageStatus.crawler, status_error=None)
    req = _Request(action="ignore_document", item_id="10", post_data={})
    req["db"] = _DB(scalar_values=[doc])
    req["user"] = SimpleNamespace(id=1)

    resp = await _raw_project_action()(req)
    assert resp.status == 200
    assert doc.status_error == PageStatusError.excluded_ignored
    assert json.loads(resp.text)["is_ignored"] is True
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
    req["db"] = _DB(execute_rows=[[11, 12]])
    req["user"] = SimpleNamespace(id=1)

    called = []

    async def _flash(request, msg, cat="success"):
        _ = request, msg, cat
        called.append("flash")

    monkeypatch.setattr(project_views, "flash", _flash)
    monkeypatch.setattr(
        project_views,
        "_queue_source_crawl_from_ui",
        lambda source_id: called.append(f"crawl:{source_id}"),
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
        "crawl:11" in called
        and "crawl:12" in called
        and "refresh_project_index" in called
        and "index_project" in called
    )


@pytest.mark.asyncio
async def test_project_triggers_generate_htmx_queues_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    req = _Request(
        action="",
        post_data=MultiDict({"action": "generate"}),
        headers={"X-CSRFToken": "ok", "HX-Request": "true"},
    )
    db = _DB()
    req["db"] = db
    req["user"] = SimpleNamespace(id=1)
    queued = []

    async def _session(request):
        _ = request
        return {}

    monkeypatch.setattr(project_views, "get_session", _session)
    monkeypatch.setattr(
        project_views.generate_missing_triggers_task,
        "delay",
        lambda: queued.append("generate"),
    )

    resp = await _raw_project_triggers()(req)

    assert resp.status == 204
    assert queued == ["generate"]
    assert db.commits == 0


@pytest.mark.asyncio
async def test_project_action_crawl_source_runs_sitemap_discovery_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = SimpleNamespace(id=7)
    req = _Request(action="crawl_source", item_id="7")
    req["db"] = _DB(scalar_values=[source])
    req["user"] = SimpleNamespace(id=1)

    called = []

    async def _flash(request, msg, cat="success"):
        _ = request, msg, cat
        called.append("flash")

    monkeypatch.setattr(project_views, "flash", _flash)

    async def _not_blocked(request, db_session, source):
        _ = request, db_session, source
        return False

    monkeypatch.setattr(
        project_views, "_check_source_blocking_and_commit", _not_blocked
    )
    monkeypatch.setattr(
        project_views,
        "_queue_source_crawl_from_ui",
        lambda source_id: called.append(f"crawl:{source_id}"),
    )

    resp = await _raw_project_action()(req)
    assert resp.status == 200
    assert called == ["crawl:7", "flash"]


@pytest.mark.asyncio
async def test_project_action_crawl_source_does_not_enqueue_blocked_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = SimpleNamespace(id=7, uri="https://blocked.example")
    req = _Request(action="crawl_source", item_id="7")
    req["db"] = _DB(scalar_values=[source])
    req["user"] = SimpleNamespace(id=1)

    called = []

    async def _flash(request, msg, cat="success"):
        _ = request, msg, cat
        called.append("flash")

    monkeypatch.setattr(project_views, "flash", _flash)

    async def _blocked(request, db_session, source):
        _ = request, db_session, source
        return True

    monkeypatch.setattr(project_views, "_check_source_blocking_and_commit", _blocked)
    monkeypatch.setattr(
        project_views,
        "_queue_source_crawl_from_ui",
        lambda source_id: called.append(f"crawl:{source_id}"),
    )

    resp = await _raw_project_action()(req)
    assert resp.status == 200
    assert called == []


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
    resp = await _raw_project_action()(req)
    assert resp.status == 200
    assert db.deleted == [doc]
    assert events == ["file_delete"]


@pytest.mark.asyncio
async def test_project_documents_json_serializes_rows() -> None:
    from vchat.page_status import PageStatus

    source = SimpleNamespace(id=2, title="Source A", uri="https://example.com")
    req = _Request(action="noop")
    req["db"] = _DB(
        execute_rows=[
            [
                (
                    5,
                    "Doc A",
                    "https://example.com/a",
                    PageStatus.ready,
                    None,
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
    payload = json.loads(resp.text)
    assert payload[0]["id"] == "5"
    assert payload[0]["source"] == "Source A"
    assert '"meta"' not in resp.text
    assert payload[0]["uri"] == "https://example.com/a"
    assert '"created_at"' not in resp.text
    assert '"updated_at"' not in resp.text
    assert '"document_type"' not in resp.text


@pytest.mark.asyncio
async def test_project_documents_json_marks_excluded_as_ignored() -> None:
    from vchat.page_status import PageStatus, PageStatusError

    source = SimpleNamespace(id=2, title="Source A", uri="https://example.com")
    req = _Request(action="noop")
    req["db"] = _DB(
        execute_rows=[
            [
                (
                    6,
                    "Thin page",
                    "https://example.com/thin",
                    PageStatus.crawler,
                    PageStatusError.low_content,
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
    payload = json.loads(resp.text)
    assert payload[0]["status_error"] == "low_content"
    assert payload[0]["is_ignored"] is False


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
    payload = json.loads(resp.text)
    assert payload[0]["id"] == "8"
    assert payload[0]["document_type"] == "pdf"
