from __future__ import annotations

import pytest
from aiohttp import web
from types import SimpleNamespace
from yarl import URL

from vchat.i18n import _
from vchat import utils as vchat_utils
from vchat.views import frontend
from vchat.views.admin import views as admin_views


class _FakeRouterItem:
    def __init__(self, path: str) -> None:
        self._path = path

    def url_for(self, **kwargs):
        _ = kwargs
        return URL(self._path)


class _FakeRouter(dict):
    def __getitem__(self, key):
        return super().__getitem__(key)


class _FakeRequest(dict):
    @property
    def app(self):
        return self["app"]

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class _FakeDb:
    def __init__(self, scalar_values: list[int], events: list[object] | None = None):
        self._scalar_values = scalar_values
        self._events = events or []

    async def execute(self, stmt):
        _ = stmt
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self._events))

    async def scalar(self, stmt):
        _ = stmt
        return self._scalar_values.pop(0) if self._scalar_values else 0


@pytest.mark.asyncio
async def test_frontend_healthcheck_redirects_to_project_view() -> None:
    request = _FakeRequest({
        "db": SimpleNamespace(execute=lambda *_a, **_k: _AsyncNoop()),
        "app": SimpleNamespace(router=_FakeRouter({"project_view": _FakeRouterItem("/")})),
    })
    with pytest.raises(web.HTTPFound) as exc_info:
        await frontend.healthcheck(request)  # type: ignore[arg-type]
    response = exc_info.value
    assert response.status == 302
    assert str(response.location) == "/"


@pytest.mark.asyncio
async def test_frontend_widget_js_renders_with_widget_path(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def _render(template, request, context):
        captured["template"] = template
        captured["context"] = context
        return "ok"

    monkeypatch.setattr(frontend.aiohttp_jinja2, "render_template", _render)
    request = SimpleNamespace(app=SimpleNamespace(router=_FakeRouter({"public_widget_chat": _FakeRouterItem("/chat/widget")})))
    result = await frontend.widget_js(request)
    assert result == "ok"
    assert captured["template"] == "js/widget.js"
    assert captured["context"]["widget_chat_path"] == "/chat/widget"


def test_i18n_translation_with_kwargs_and_fallbacks() -> None:
    assert _("Error {code}", code=500) == "Ошибка {code}"
    assert _("Update task started for %(title)s", title="Doc") == "Задача обновления запущена для Doc"
    assert _("Unknown untranslated string") == "Unknown untranslated string"


@pytest.mark.asyncio
async def test_admin_event_list_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
    db = _FakeDb([2], events=events)
    async def _fake_session(_request):
        return {"user_id": 1}

    monkeypatch.setattr(vchat_utils, "get_session", _fake_session)
    request = _FakeRequest({
        "db": db,
        "user": SimpleNamespace(id=1),
        "auth_session": {"user_id": 1},
        "rel_url": SimpleNamespace(query={"page": "1"}),
        "path": "/events/",
        "app": SimpleNamespace(router=_FakeRouter({"login": _FakeRouterItem("/login/")})),
    })
    raw_event_list = admin_views.event_list.__wrapped__.__wrapped__.__wrapped__
    context = await raw_event_list(request)  # type: ignore[arg-type]
    assert context["page"] == 1
    assert context["total_items"] == 2
    assert len(context["events"]) == 2


@pytest.mark.asyncio
async def test_admin_user_list_builds_context(monkeypatch: pytest.MonkeyPatch) -> None:
    users = [SimpleNamespace(id=1), SimpleNamespace(id=2)]

    async def _fake_get_users(_db):
        return users

    async def _fake_get_session(_request):
        return {"user_id": 1}

    monkeypatch.setattr(admin_views, "_get_users", _fake_get_users)
    monkeypatch.setattr(admin_views, "get_session", _fake_get_session)
    monkeypatch.setattr(vchat_utils, "get_session", _fake_get_session)
    monkeypatch.setattr(admin_views, "CreateUserForm", lambda meta=None: {"meta": meta})

    request = _FakeRequest({
        "db": SimpleNamespace(),
        "user": SimpleNamespace(id=1),
        "auth_session": {"user_id": 1},
        "path": "/users/",
        "app": SimpleNamespace(router=_FakeRouter({"login": _FakeRouterItem("/login/")})),
    })
    raw_user_list = admin_views.user_list.__wrapped__.__wrapped__.__wrapped__
    context = await raw_user_list(request)  # type: ignore[arg-type]
    assert context["total_users"] == 2
    assert context["current_user_id"] == 1
    assert context["add_form"]["meta"] is not None


class _AsyncNoop:
    def __await__(self):
        async def _coro():
            return None

        return _coro().__await__()
