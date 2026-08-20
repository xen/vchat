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

from vchat.settings import REDIS_KEY, SIGNER_KEY
from vchat.widget_state import (
    WIDGET_STATE_DISABLED,
    WIDGET_STATE_ENABLED,
    WIDGET_STATE_MISSING,
    WIDGET_STATE_TTL_SECONDS,
    widget_state_key,
)
from vchat.views.projects import views as project_views


def test_queue_source_crawl_from_ui_reapplies_rules_before_crawl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued_tasks = []
    applied = []

    class _Chain:
        def apply_async(self):
            applied.append(True)

    monkeypatch.setattr(
        project_views.reapply_source_rules_task,
        "si",
        lambda source_id: ("reapply", source_id),
    )
    monkeypatch.setattr(
        project_views.sitemap_sync_task,
        "si",
        lambda source_id: ("sitemap", source_id),
    )
    monkeypatch.setattr(
        project_views.crawl_source_task,
        "si",
        lambda source_id, **kwargs: ("crawl", source_id, kwargs),
    )

    def _chain(*tasks):
        queued_tasks.extend(tasks)
        return _Chain()

    monkeypatch.setattr(project_views, "chain", _chain)

    project_views._queue_source_crawl_from_ui(7)

    assert queued_tasks == [
        ("reapply", 7),
        ("sitemap", 7),
        ("crawl", 7, {"skip_sitemap_sync": True}),
    ]
    assert applied == [True]


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
        super().__init__(
            {
                SIGNER_KEY: _Signer(),
                REDIS_KEY: _Redis(),
            }
        )
        self.router = {
            "users": _Route("/users/"),
            "actions": _Route("/actions/{action}/{item_id}"),
            "project_files": _Route("/files"),
            "file_document": _Route("/file/{document_id}"),
            "project_integration": _Route("/integration"),
            "project_triggers": _Route("/triggers"),
            "project_llm_cache": _Route("/llm-cache"),
            "project_widget_edit": _Route("/integration/{widget_id}"),
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
        self.remote = "127.0.0.1"
        self._post_data = post_data or {}

    async def post(self):
        return self._post_data


class _Redis:
    def __init__(self):
        self.set_calls = []

    async def set(self, key, value, *, ex):
        self.set_calls.append((key, value, ex))


class _DB:
    def __init__(self, *, scalar_values=None, execute_rows=None):
        self.scalar_values = list(scalar_values or [])
        self.execute_rows = list(execute_rows or [])
        self.deleted = []
        self.added = []
        self.commits = 0
        self.rollbacks = 0

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

    async def rollback(self):
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_load_llm_cache_context_keeps_entries_loaded_for_template() -> None:
    entry = SimpleNamespace(id=5, source_scope={"chunks_count": 2})
    request = _Request(action="unused")
    db = _DB(
        scalar_values=[3, 2, 8, 5, 13],
        execute_rows=[[entry]],
    )
    request["db"] = db

    context = await project_views._load_llm_cache_context(request)

    assert context["entries"] == [entry]
    assert context["stats"] == {
        "total_entries": 3,
        "enabled_entries": 2,
        "observed_total": 8,
        "potential_saved_requests": 5,
        "potential_saved_tokens": 13,
    }
    assert db.rollbacks == 0


def _raw_project_action():
    return project_views.project_action.__wrapped__


def _raw_project_triggers():
    return project_views.project_triggers.__wrapped__.__wrapped__


def _raw(func):
    while hasattr(func, "__wrapped__"):
        func = func.__wrapped__
    return func


@pytest.mark.asyncio
async def test_project_action_rejects_missing_csrf() -> None:
    req = _Request(action="delete_source", headers={})
    req["db"] = _DB()
    req["user"] = SimpleNamespace(id=1)
    with pytest.raises(web.HTTPForbidden):
        await _raw_project_action()(req)


@pytest.mark.asyncio
async def test_project_action_user_create_respects_disabled_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _DB()
    req = _Request(
        action="user_create",
        post_data={"email": "new@example.com", "password": "long-enough-password"},
    )
    monkeypatch.setattr(project_views.cfg, "admin_user_create_enabled", False)
    req["db"] = db
    req["user"] = SimpleNamespace(id=1)

    with pytest.raises(web.HTTPForbidden):
        await _raw_project_action()(req)

    assert db.added == []
    assert db.commits == 0


@pytest.mark.asyncio
async def test_project_action_revoke_user_sessions() -> None:
    target_user = SimpleNamespace(id=7)
    db = _DB(scalar_values=[target_user])
    req = _Request(action="user_revoke_sessions", item_id="7")
    req["db"] = db
    req["user"] = SimpleNamespace(id=1)

    response = await _raw_project_action()(req)

    assert response.status == 200
    assert response.headers["HX-Refresh"] == "true"
    assert db.commits == 2
    assert db.added[-1].event_name == "user_sessions_revoke"


@pytest.mark.asyncio
async def test_project_action_revoke_all_user_sessions() -> None:
    db = _DB()
    req = _Request(action="user_revoke_all_sessions", item_id="global")
    req["db"] = db
    req["user"] = SimpleNamespace(id=1)

    response = await _raw_project_action()(req)

    assert response.status == 200
    assert response.headers["HX-Refresh"] == "true"
    assert db.commits == 2
    assert db.added[-1].event_name == "user_sessions_revoke_all"


@pytest.mark.asyncio
async def test_project_files_create_rejects_missing_csrf() -> None:
    req = _Request(action="", post_data={}, headers={})
    req["db"] = _DB()
    req["user"] = SimpleNamespace(id=1, name="Alice", email="alice@example.test")

    with pytest.raises(web.HTTPForbidden):
        await _raw(project_views.project_files)(req)


@pytest.mark.asyncio
async def test_project_files_create_accepts_form_csrf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    req = _Request(action="", post_data={"csrf_token": "ok"})
    req["db"] = _DB()
    req["user"] = SimpleNamespace(id=1, name="Alice", email="alice@example.test")
    events = []

    async def _admin(event, request):
        _ = request
        events.append(event)

    async def _update_shingles(*args, **kwargs):
        _ = args, kwargs

    monkeypatch.setattr(project_views, "admin_event", _admin)
    monkeypatch.setattr(project_views, "async_update_page_shingles", _update_shingles)

    with pytest.raises(web.HTTPFound) as exc:
        await _raw(project_views.project_files)(req)

    assert str(exc.value.location).startswith("/file/")
    assert req["db"].commits == 1
    assert events == ["file_create"]


@pytest.mark.asyncio
async def test_file_document_save_rejects_missing_csrf() -> None:
    document = SimpleNamespace(id=7, source_id=None, uri=None)
    req = _Request(action="", post_data={"content": "changed"}, headers={})
    req.match_info = {"document_id": "7"}
    req.path = "/file/7"
    req["db"] = _DB(scalar_values=[document])
    req["user"] = SimpleNamespace(id=1)

    with pytest.raises(web.HTTPForbidden):
        await _raw(project_views.file_document)(req)


@pytest.mark.asyncio
async def test_file_document_delete_accepts_form_csrf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = SimpleNamespace(id=7, source_id=None, uri=None)
    db = _DB(scalar_values=[document])
    req = _Request(action="", post_data={"csrf_token": "ok", "action": "delete"})
    req.match_info = {"document_id": "7"}
    req.path = "/file/7"
    req["db"] = db
    req["user"] = SimpleNamespace(id=1)
    events = []

    async def _admin(event, request):
        _ = request
        events.append(event)

    monkeypatch.setattr(project_views, "admin_event", _admin)

    with pytest.raises(web.HTTPFound) as exc:
        await _raw(project_views.file_document)(req)

    assert str(exc.value.location) == "/files"
    assert db.deleted == [document]
    assert db.commits == 1
    assert events == ["file_delete"]


@pytest.mark.asyncio
async def test_project_action_ignore_document_toggle() -> None:
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
async def test_project_action_delete_document() -> None:
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
        action="generate_triggers",
        item_id="global",
        post_data=MultiDict(),
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

    resp = await _raw_project_action()(req)

    assert resp.status == 200
    assert resp.headers["HX-Trigger"] == "project-triggers:refresh"
    assert queued == ["generate"]
    assert db.commits == 0


@pytest.mark.asyncio
async def test_project_integration_create_requires_form_csrf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    req = _Request(
        action="",
        post_data=MultiDict({"name": "Widget"}),
        headers={},
    )
    req["db"] = _DB()
    req["user"] = SimpleNamespace(id=1)

    async def _session(request):
        _ = request
        return {}

    monkeypatch.setattr(project_views, "get_session", _session)

    context = await _raw(project_views.project_integration)(req)

    assert "project" not in context
    assert set(context) == {"form", "widgets"}
    assert context["form"].errors


@pytest.mark.asyncio
async def test_project_integration_add_uses_initial_welcome_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    req = _Request(
        action="",
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

    async def _session(request):
        _ = request
        return {}

    original_form = project_views.forms.WidgetIntegrationAdd

    def _form_without_csrf(*args, **kwargs):
        kwargs["meta"] = {"csrf": False}
        return original_form(*args, **kwargs)

    monkeypatch.setattr(
        project_views.forms,
        "WidgetIntegrationAdd",
        _form_without_csrf,
    )
    monkeypatch.setattr(project_views, "get_session", _session)
    monkeypatch.setattr(project_views, "_assign_new_widget_code", _assign_code)
    monkeypatch.setattr(project_views, "admin_event", _event)
    monkeypatch.setattr(project_views, "flash", _flash)

    with pytest.raises(web.HTTPFound) as exc:
        await _raw(project_views.project_integration)(req)

    assert str(exc.value.location) == "/integration/7"
    assert db.added[0].agent_name == "Чат поддержки"
    assert db.added[0].welcome_messages == [
        "Добро пожаловать в чат, задавайте вопросы"
    ]
    assert db.added[0].waiting_messages == ["Готовлю ответ"]
    assert db.added[0].trigger_templates == list(
        project_views.forms.DEFAULT_TRIGGER_TEMPLATES
    )
    assert (
        db.added[0].error_message
        == "Извините, сейчас не удалось получить ответ. Попробуйте отправить сообщение позже."
    )
    assert db.added[0].footer_text == "Отправить Enter, новая строка Shift+Enter"
    assert db.added[0].system_prompt == project_views.forms.DEFAULT_SYSTEM_PROMPT
    assert db.added[0].secret
    assert db.commits == 1
    assert req.app[REDIS_KEY].set_calls == [
        (widget_state_key("new-code"), WIDGET_STATE_ENABLED, WIDGET_STATE_TTL_SECONDS)
    ]
    assert events == ["widget_create"]
    assert flashes == [("Код виджета создан", "success")]


@pytest.mark.asyncio
async def test_project_widget_edit_saves_footer_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    widget = SimpleNamespace(
        id=7,
        name="Old",
        code="abc",
        agent_name="",
        is_enabled=False,
        welcome_messages=[],
        waiting_messages=[],
        error_message="",
        footer_text="",
        system_prompt="",
        suggestions_enabled=False,
        suggestions_prompt="",
        trigger_templates=[],
        pinned_messages=[],
        updated_at=None,
    )
    req = _Request(
        action="",
        post_data=MultiDict(
            [
                ("name", "Widget"),
                ("agent_name", "Agent"),
                ("welcome_messages-0", "Hello"),
                ("welcome_messages-1", "<strong>Second</strong>"),
                ("waiting_messages-0", "Готовлю ответ"),
                ("waiting_messages-1", "Проверяю источники"),
                ("trigger_templates-0", "Первый {title}?"),
                ("trigger_templates-1", "Второй {title}?"),
                ("error_message", "<strong>Сбой</strong><script>x</script>"),
                ("pinned_messages-0-text", "<strong>Pinned</strong>"),
                ("pinned_messages-0-color", "primary"),
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
    req.match_info = {"widget_id": "7"}
    db = _DB(scalar_values=[widget])
    req["db"] = db
    req["user"] = SimpleNamespace(id=1)
    events = []
    flashes = []

    async def _event(name, _request):
        events.append(name)

    async def _flash(_request, message, category="success"):
        flashes.append((message, category))

    async def _session(request):
        _ = request
        return {}

    original_form = project_views.forms.WidgetIntegrationEdit

    def _form_without_csrf(*args, **kwargs):
        kwargs["meta"] = {"csrf": False}
        return original_form(*args, **kwargs)

    monkeypatch.setattr(
        project_views.forms,
        "WidgetIntegrationEdit",
        _form_without_csrf,
    )
    monkeypatch.setattr(project_views, "get_session", _session)
    monkeypatch.setattr(project_views, "admin_event", _event)
    monkeypatch.setattr(project_views, "flash", _flash)

    with pytest.raises(web.HTTPFound) as exc:
        await _raw(project_views.project_widget_edit)(req)

    assert str(exc.value.location) == "/integration/7"
    assert widget.name == "Widget"
    assert widget.agent_name == "Agent"
    assert widget.is_enabled is False
    assert widget.welcome_messages == ["Hello", "<strong>Second</strong>"]
    assert widget.waiting_messages == ["Готовлю ответ", "Проверяю источники"]
    assert widget.trigger_templates == ["Первый {title}?", "Второй {title}?"]
    assert widget.error_message == "<strong>Сбой</strong>"
    assert widget.pinned_messages == [
        {"text": "<strong>Pinned</strong>", "color": "primary"}
    ]
    assert widget.footer_text.startswith('<a href="https://vbudushee.ru/faq/"')
    assert "script" not in widget.footer_text
    assert not hasattr(widget, "contact_url")
    assert db.commits == 1
    assert req.app[REDIS_KEY].set_calls == []
    assert events == ["widget_update"]
    assert flashes == [("Код виджета обновлен", "success")]


@pytest.mark.asyncio
async def test_project_widget_edit_invalid_post_returns_context_without_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = SimpleNamespace(
        id=7,
        name="Old",
        code="abc",
        agent_name="Agent",
        is_enabled=True,
        welcome_messages=["Hello"],
        waiting_messages=["Готовлю ответ"],
        error_message="Ошибка",
        footer_text="Footer",
        system_prompt="Prompt",
        suggestions_enabled=False,
        suggestions_prompt="Suggestions",
        trigger_templates=["Default {title}?"],
        pinned_messages=[],
        updated_at=None,
    )
    req = _Request(
        action="",
        post_data=MultiDict(
            [
                ("name", ""),
                ("agent_name", "Agent"),
                ("welcome_messages-0", "Hello"),
                ("waiting_messages-0", "Готовлю ответ"),
                ("trigger_templates-0", "Default {title}?"),
                ("error_message", "Ошибка"),
                ("footer_text", "Footer"),
                ("system_prompt", "Prompt"),
                ("suggestions_prompt", "Suggestions"),
            ]
        ),
    )
    req.match_info = {"widget_id": "7"}
    db = _DB(scalar_values=[item])
    req["db"] = db
    req["user"] = SimpleNamespace(id=1)

    async def _session(request):
        _ = request
        return {}

    original_form = project_views.forms.WidgetIntegrationEdit

    def _form_without_csrf(*args, **kwargs):
        kwargs["meta"] = {"csrf": False}
        return original_form(*args, **kwargs)

    def _render_template(*args, **kwargs):
        _ = args, kwargs
        raise AssertionError("project_widget_edit must return context dict")

    monkeypatch.setattr(
        project_views.forms,
        "WidgetIntegrationEdit",
        _form_without_csrf,
    )
    monkeypatch.setattr(project_views, "get_session", _session)
    monkeypatch.setattr(
        project_views.aiohttp_jinja2,
        "render_template",
        _render_template,
    )

    context = await _raw(project_views.project_widget_edit)(req)

    assert context["item"] is item
    assert "form" in context
    assert context["form"].errors["name"] == ["Название обязательно"]
    assert db.commits == 0
    assert db.rollbacks == 0


@pytest.mark.asyncio
async def test_project_action_widget_reset_code_generates_new_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    widget = SimpleNamespace(id=7, code="old-code", is_enabled=False, updated_at=None)
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

    assert resp.status == 200
    assert resp.headers["HX-Refresh"] == "true"
    assert widget.code == "new-code"
    assert widget.updated_at is not None
    assert db.commits == 1
    assert req.app[REDIS_KEY].set_calls == [
        (widget_state_key("old-code"), WIDGET_STATE_MISSING, WIDGET_STATE_TTL_SECONDS),
        (widget_state_key("new-code"), WIDGET_STATE_DISABLED, WIDGET_STATE_TTL_SECONDS),
    ]
    assert events == ["widget_reset_code"]
    assert flashes == [("Код виджета сброшен", "success")]


@pytest.mark.asyncio
async def test_project_action_widget_reset_secret_generates_new_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    widget = SimpleNamespace(id=7, code="widget-code", secret="old-secret")
    req = _Request(action="widget_reset_secret", item_id="7", post_data=MultiDict())
    db = _DB(scalar_values=[widget])
    req["db"] = db
    req["user"] = SimpleNamespace(id=1)
    events = []
    flashes = []

    async def _event(name, _request):
        events.append(name)

    async def _flash(_request, message, category="success"):
        flashes.append((message, category))

    monkeypatch.setattr(project_views, "_new_widget_secret", lambda: "new-secret")
    monkeypatch.setattr(project_views, "admin_event", _event)
    monkeypatch.setattr(project_views, "flash", _flash)

    response = await _raw_project_action()(req)

    assert response.text == "ok"
    assert response.headers["HX-Refresh"] == "true"
    assert widget.secret == "new-secret"
    assert widget.updated_at is not None
    assert db.commits == 1
    assert req.app[REDIS_KEY].set_calls == []
    assert events == ["widget_reset_secret"]
    assert flashes == [("Секрет виджета сброшен", "success")]


@pytest.mark.asyncio
async def test_project_action_widget_disable_updates_state_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    widget = SimpleNamespace(
        id=7,
        code="widget-code",
        is_enabled=True,
        updated_at=None,
    )
    req = _Request(action="widget_disable", item_id="7", post_data=MultiDict())
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

    assert resp.status == 200
    assert resp.headers["HX-Refresh"] == "true"
    assert widget.is_enabled is False
    assert widget.updated_at is not None
    assert db.commits == 1
    assert req.app[REDIS_KEY].set_calls == [
        (widget_state_key("widget-code"), WIDGET_STATE_DISABLED, WIDGET_STATE_TTL_SECONDS)
    ]
    assert events == ["widget_disable"]
    assert flashes == [("Виджет отключен", "success")]


@pytest.mark.asyncio
async def test_project_action_widget_enable_updates_state_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    widget = SimpleNamespace(
        id=7,
        code="widget-code",
        is_enabled=False,
        updated_at=None,
    )
    req = _Request(action="widget_enable", item_id="7", post_data=MultiDict())
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

    assert resp.status == 200
    assert resp.headers["HX-Refresh"] == "true"
    assert widget.is_enabled is True
    assert widget.updated_at is not None
    assert db.commits == 1
    assert req.app[REDIS_KEY].set_calls == [
        (widget_state_key("widget-code"), WIDGET_STATE_ENABLED, WIDGET_STATE_TTL_SECONDS)
    ]
    assert events == ["widget_enable"]
    assert flashes == [("Виджет включен", "success")]


@pytest.mark.asyncio
async def test_project_action_widget_delete_returns_plain_action_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    widget = SimpleNamespace(id=7, code="widget-code")
    req = _Request(action="widget_delete", item_id="7", post_data=MultiDict())
    db = _DB(scalar_values=[widget])
    req["db"] = db
    req["user"] = SimpleNamespace(id=1)
    events = []

    async def _event(name, _request):
        events.append(name)

    monkeypatch.setattr(project_views, "admin_event", _event)

    resp = await _raw_project_action()(req)

    assert resp.status == 200
    assert "HX-Trigger" not in resp.headers
    assert "HX-Refresh" not in resp.headers
    assert db.deleted == [widget]
    assert db.commits == 1
    assert req.app[REDIS_KEY].set_calls == [
        (widget_state_key("widget-code"), WIDGET_STATE_MISSING, WIDGET_STATE_TTL_SECONDS)
    ]
    assert events == ["widget_delete"]


@pytest.mark.asyncio
async def test_project_action_llm_cache_disable_enable_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_entry = SimpleNamespace(
        id=5,
        is_enabled=True,
        disabled_reason=None,
        updated_at=None,
    )
    events = []
    flashes = []

    async def _event(name, _request):
        events.append(name)

    async def _flash(_request, message, category="success"):
        flashes.append((message, category))

    monkeypatch.setattr(project_views, "admin_event", _event)
    monkeypatch.setattr(project_views, "flash", _flash)

    disable_req = _Request(action="llm_cache_disable", item_id="5")
    disable_req["db"] = _DB(scalar_values=[cache_entry])
    disable_req["user"] = SimpleNamespace(id=1)

    disable_resp = await _raw_project_action()(disable_req)

    assert disable_resp.status == 200
    assert disable_resp.headers["HX-Refresh"] == "true"
    assert cache_entry.is_enabled is False
    assert cache_entry.disabled_reason == "manual"
    assert cache_entry.updated_at is not None
    assert disable_req["db"].commits == 1

    enable_req = _Request(action="llm_cache_enable", item_id="5")
    enable_req["db"] = _DB(scalar_values=[cache_entry])
    enable_req["user"] = SimpleNamespace(id=1)

    enable_resp = await _raw_project_action()(enable_req)

    assert enable_resp.status == 200
    assert enable_resp.headers["HX-Refresh"] == "true"
    assert cache_entry.is_enabled is True
    assert cache_entry.disabled_reason is None
    assert enable_req["db"].commits == 1

    delete_req = _Request(action="llm_cache_delete", item_id="5")
    delete_req["db"] = _DB(scalar_values=[cache_entry])
    delete_req["user"] = SimpleNamespace(id=1)

    delete_resp = await _raw_project_action()(delete_req)

    assert delete_resp.status == 200
    assert delete_resp.headers["HX-Refresh"] == "true"
    assert delete_req["db"].deleted == [cache_entry]
    assert delete_req["db"].commits == 1
    assert events == [
        "llm_cache_disable",
        "llm_cache_enable",
        "llm_cache_delete",
    ]
    assert flashes == [
        ("Запись LLM-кеша отключена", "success"),
        ("Запись LLM-кеша включена", "success"),
        ("Запись LLM-кеша удалена", "success"),
    ]


@pytest.mark.asyncio
async def test_project_action_llm_cache_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    req = _Request(action="llm_cache_clear", item_id="global")
    req["db"] = _DB()
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

    assert resp.status == 200
    assert resp.headers["HX-Refresh"] == "true"
    assert req["db"].commits == 1
    assert events == ["llm_cache_clear"]
    assert flashes == [("Реестр LLM-кеша очищен", "success")]


@pytest.mark.asyncio
async def test_project_triggers_requires_signed_csrf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    req = _Request(action="", post_data=MultiDict({"action": "generate"}), headers={})
    req.match_info = {"action": "generate_triggers", "item_id": "global"}
    req["db"] = _DB()
    req["user"] = SimpleNamespace(id=1)

    async def _session(request):
        _ = request
        return {}

    monkeypatch.setattr(project_views, "get_session", _session)

    with pytest.raises(web.HTTPForbidden):
        await _raw_project_action()(req)


@pytest.mark.asyncio
async def test_project_triggers_rejects_bad_csrf_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    req = _Request(
        action="generate_triggers",
        item_id="global",
        post_data=MultiDict(),
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
        await _raw_project_action()(req)


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
    document.patch_meta = lambda **kwargs: document.meta.update(kwargs)
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
async def test_project_documents_csv_neutralizes_formula_cells() -> None:
    from vchat.views.projects.page_status import PageStatus

    req = _Request(action="noop")
    req["db"] = _DB(
        execute_rows=[
            [
                (
                    7,
                    "=HYPERLINK(\"https://evil.test\")",
                    "+https://example.com/a",
                    PageStatus.ready,
                    None,
                    "@Source",
                    "\tSource URI",
                    123,
                    2,
                ),
                (
                    8,
                    "\rTitle",
                    "-https://example.com/b",
                    PageStatus.ready,
                    None,
                    "\nSource",
                    "https://example.com",
                    0,
                    0,
                ),
            ]
        ]
    )
    req["user"] = SimpleNamespace(id=1)

    raw = project_views.project_documents_csv.__wrapped__
    resp = await raw(req)
    payload = _csv_rows(resp.text)

    assert payload[0]["title"].startswith("'=")
    assert payload[0]["uri"].startswith("'+")
    assert payload[0]["source"].startswith("'@")
    assert payload[1]["uri"].startswith("'-")
    assert payload[1]["source"].startswith("'\n")
    assert payload[0]["id"] == "7"
    assert payload[0]["size_bytes"] == "123"
    assert project_views._neutralize_csv_cell("\rTitle") == "'\rTitle"


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
