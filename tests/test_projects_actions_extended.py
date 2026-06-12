from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiohttp import web
from itsdangerous import BadSignature
from multidict import MultiDict
from yarl import URL

from vchat.settings import CONFIG_KEY, SIGNER_KEY
from vchat.views.projects import views as project_views


def _csv_rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


class _Signer:
    def loads(self, token, max_age=86400):
        _ = token, max_age
        return 1


class _BadSigner:
    def loads(self, token, max_age=86400):
        _ = token, max_age
        raise BadSignature("bad")


class _Route:
    def __init__(self, path: str):
        self.path = path

    def url_for(self, **kwargs):
        return URL(self.path.format(**kwargs) if kwargs else self.path)


class _App(dict):
    def __init__(self):
        super().__init__({SIGNER_KEY: _Signer(), CONFIG_KEY: {"secret_key": b"k" * 32}})
        self.router = {
            "users": _Route("/users/"),
            "actions": _Route("/actions/{action}/{item_id}"),
            "project_integration": _Route("/integration"),
            "project_triggers": _Route("/triggers"),
            "project_widget_edit": _Route("/integration/widgets/{widget_id}"),
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

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1


def _raw_project_action():
    return project_views.project_action.__wrapped__


def _raw_project_triggers():
    return project_views.project_triggers.__wrapped__.__wrapped__


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
    from vchat.views.projects.page_status import PageStatus, PageStatusError

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
        headers={"X-CSRFToken": "ok"},
    )
    db = _DB()
    req["db"] = db
    req["user"] = SimpleNamespace(id=1)
    queued = []

    async def _session(request):
        _ = request
        return {}

    async def _flash(*args, **kwargs):
        _ = args, kwargs

    monkeypatch.setattr(project_views, "get_session", _session)
    monkeypatch.setattr(project_views, "flash", _flash)
    monkeypatch.setattr(
        project_views.generate_missing_triggers_task,
        "delay",
        lambda: queued.append("generate"),
    )

    resp = await _raw_project_triggers()(req)

    assert resp.status == 204
    assert resp.headers["HX-Redirect"] == "/triggers"
    assert queued == ["generate"]
    assert db.commits == 0


@pytest.mark.asyncio
async def test_project_action_widget_create_requires_signed_header() -> None:
    req = _Request(
        action="widget_create",
        item_id="global",
        post_data=MultiDict({"name": "Widget"}),
        headers={},
    )
    req["db"] = _DB()
    req["user"] = SimpleNamespace(id=1)

    with pytest.raises(web.HTTPForbidden):
        await _raw_project_action()(req)


@pytest.mark.asyncio
async def test_project_action_widget_create_uses_default_welcome_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    req = _Request(
        action="widget_create",
        item_id="global",
        post_data=MultiDict({"name": "Widget"}),
    )
    db = _DB()
    req["db"] = db
    req["user"] = SimpleNamespace(id=1)
    events = []
    flashes = []

    async def _event(name, _request):
        events.append(name)

    async def _flash(_request, message, category="success"):
        flashes.append((message, category))

    async def _assign_code(_db_session, widget):
        widget.id = 7
        widget.code = "new-code"

    monkeypatch.setattr(project_views, "_assign_new_widget_code", _assign_code)
    monkeypatch.setattr(project_views, "admin_event", _event)
    monkeypatch.setattr(project_views, "flash", _flash)

    resp = await _raw_project_action()(req)

    assert resp.status == 204
    assert resp.headers["HX-Redirect"] == "/integration/widgets/7"
    assert db.added[0].agent_name == "Чат поддержки"
    assert db.added[0].welcome_messages == [
        "Здравствуйте! Напишите ваш вопрос."
    ]
    assert db.added[0].waiting_messages == ["Готовлю ответ"]
    assert db.added[0].footer_text == "Отправить Enter, новая строка Shift+Enter"
    assert db.added[0].system_prompt == project_views.forms.DEFAULT_SYSTEM_PROMPT
    assert db.commits == 1
    assert events == ["widget_create"]
    assert flashes == [("Код виджета создан", "success")]


@pytest.mark.asyncio
async def test_project_action_widget_update_saves_footer_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    widget = SimpleNamespace(
        id=7,
        name="Old",
        agent_name="",
        welcome_messages=[],
        waiting_messages=[],
        footer_text="",
        system_prompt="",
        suggestions_enabled=False,
        suggestions_prompt="",
        pinned_messages=[],
        updated_at=None,
    )
    req = _Request(
        action="widget_update",
        item_id="7",
        post_data=MultiDict(
            [
                ("name", "Widget"),
                ("agent_name", "Agent"),
                ("welcome_text[]", "Hello"),
                ("welcome_text[]", "<strong>Second</strong>"),
                ("waiting_text[]", "Готовлю ответ"),
                ("waiting_text[]", "<script>x</script>Проверяю источники"),
                (
                    "footer_text",
                    '<a href="https://vbudushee.ru/faq/">Пользовательское соглашение</a><script>x</script>',
                ),
                ("system_prompt", "Prompt"),
                ("suggestions_enabled", "1"),
                ("suggestions_prompt", "Suggestions"),
            ]
        ),
    )
    db = _DB(scalar_values=[widget])
    req["db"] = db
    req["user"] = SimpleNamespace(id=1)
    events = []
    flashes = []

    async def _event(name, _request):
        events.append(name)

    async def _flash(_request, message, category="success"):
        flashes.append((message, category))

    monkeypatch.setattr(project_views, "admin_event", _event)
    monkeypatch.setattr(project_views, "flash", _flash)

    resp = await _raw_project_action()(req)

    assert resp.status == 204
    assert resp.headers["HX-Redirect"] == "/integration/widgets/7"
    assert widget.name == "Widget"
    assert widget.welcome_messages == ["Hello", "<strong>Second</strong>"]
    assert widget.waiting_messages == ["Готовлю ответ", "Проверяю источники"]
    assert widget.footer_text.startswith('<a href="https://vbudushee.ru/faq/"')
    assert "script" not in widget.footer_text
    assert not hasattr(widget, "contact_url")
    assert db.commits == 1
    assert events == ["widget_update"]
    assert flashes == [("Код виджета обновлен", "success")]


@pytest.mark.asyncio
async def test_project_action_widget_reset_code_generates_new_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    widget = SimpleNamespace(id=7, code="old-code", updated_at=None)
    req = _Request(action="widget_reset_code", item_id="7", post_data=MultiDict())
    db = _DB(scalar_values=[widget, None])
    req["db"] = db
    req["user"] = SimpleNamespace(id=1)
    events = []
    flashes = []

    async def _event(name, _request):
        events.append(name)

    async def _flash(_request, message, category="success"):
        flashes.append((message, category))

    monkeypatch.setattr(project_views, "_new_widget_code", lambda: "new-code")
    monkeypatch.setattr(project_views, "admin_event", _event)
    monkeypatch.setattr(project_views, "flash", _flash)

    resp = await _raw_project_action()(req)

    assert resp.status == 204
    assert resp.headers["HX-Redirect"] == "/integration/widgets/7"
    assert widget.code == "new-code"
    assert widget.updated_at is not None
    assert db.commits == 1
    assert events == ["widget_reset_code"]
    assert flashes == [("Код виджета сброшен", "success")]


@pytest.mark.asyncio
async def test_project_action_widget_delete_redirects_to_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    widget = SimpleNamespace(id=7)
    req = _Request(action="widget_delete", item_id="7", post_data=MultiDict())
    db = _DB(scalar_values=[widget])
    req["db"] = db
    req["user"] = SimpleNamespace(id=1)
    events = []

    async def _event(name, _request):
        events.append(name)

    monkeypatch.setattr(project_views, "admin_event", _event)

    resp = await _raw_project_action()(req)

    assert resp.status == 204
    assert resp.headers["HX-Redirect"] == "/integration"
    assert "HX-Refresh" not in resp.headers
    assert db.deleted == [widget]
    assert db.commits == 1
    assert events == ["widget_delete"]


@pytest.mark.asyncio
async def test_project_triggers_requires_signed_csrf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    req = _Request(action="", post_data=MultiDict({"action": "generate"}), headers={})
    req["db"] = _DB()
    req["user"] = SimpleNamespace(id=1)

    async def _session(request):
        _ = request
        return {}

    monkeypatch.setattr(project_views, "get_session", _session)

    with pytest.raises(web.HTTPForbidden):
        await _raw_project_triggers()(req)


@pytest.mark.asyncio
async def test_project_triggers_rejects_bad_csrf_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    req = _Request(
        action="",
        post_data=MultiDict({"action": "generate"}),
        headers={"X-CSRFToken": "bad"},
    )
    req.app[SIGNER_KEY] = _BadSigner()
    req["db"] = _DB()
    req["user"] = SimpleNamespace(id=1)

    async def _session(request):
        _ = request
        return {}

    monkeypatch.setattr(project_views, "get_session", _session)

    with pytest.raises(web.HTTPForbidden):
        await _raw_project_triggers()(req)


@pytest.mark.asyncio
async def test_project_action_api_client_reset_secret_uses_signed_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SimpleNamespace(
        id=3,
        name="Client",
        client_id="cid",
        encrypted_secret="old",
    )
    req = _Request(
        action="api_client_update",
        item_id="3",
        post_data=MultiDict(
            {
                "name": "Client",
                "reset_secret": "1",
            }
        ),
        headers={"X-CSRFToken": "ok"},
    )
    db = _DB(scalar_values=[client])
    req["db"] = db
    req["user"] = SimpleNamespace(id=1)

    async def _session(request):
        _ = request
        return {}

    async def _context(*args, **kwargs):
        _ = args, kwargs
        return {}

    def _render_template(*args, **kwargs):
        _ = args, kwargs
        return web.Response(text="ok")

    async def _admin_event(*args, **kwargs):
        _ = args, kwargs

    monkeypatch.setattr(project_views, "get_session", _session)
    monkeypatch.setattr(project_views, "_api_client_list_context", _context)
    monkeypatch.setattr(project_views, "admin_event", _admin_event)
    monkeypatch.setattr(project_views, "encrypt_client_secret", lambda *args: "enc")
    monkeypatch.setattr(project_views.aiohttp_jinja2, "render_template", _render_template)

    resp = await _raw_project_action()(req)

    assert resp.status == 200
    assert db.commits == 1
    assert client.encrypted_secret == "enc"


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
async def test_project_documents_csv_serializes_rows() -> None:
    from vchat.views.projects.page_status import PageStatus

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
    raw = project_views.project_documents_csv.__wrapped__
    resp = await raw(req)
    assert resp.status == 200
    assert resp.content_type == "text/csv"
    payload = _csv_rows(resp.text)
    assert payload[0]["id"] == "5"
    assert payload[0]["source"] == "Source A"
    assert payload[0]["status"] == "ready"
    assert payload[0]["is_ignored"] == "0"
    assert "meta" not in payload[0]
    assert payload[0]["uri"] == "https://example.com/a"
    assert "created_at" not in payload[0]
    assert "updated_at" not in payload[0]
    assert "document_type" not in payload[0]


@pytest.mark.asyncio
async def test_project_documents_csv_marks_excluded_as_ignored() -> None:
    from vchat.views.projects.page_status import PageStatus, PageStatusError

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

    raw = project_views.project_documents_csv.__wrapped__
    resp = await raw(req)
    assert resp.status == 200
    payload = _csv_rows(resp.text)
    assert payload[0]["status_error"] == "low_content"
    assert payload[0]["is_ignored"] == "0"


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
