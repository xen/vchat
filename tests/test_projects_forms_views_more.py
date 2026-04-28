from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp import web
from yarl import URL

from vchat.views.projects import views as project_views


class _Route:
    def __init__(self, path: str):
        self._path = path

    def url_for(self, **kwargs):
        _ = kwargs
        return URL(self._path)


class _Req(dict):
    def __init__(self, *, method="GET", post_data=None, path="/x", app=None):
        super().__init__()
        self.method = method
        self._post_data = post_data or {}
        self.path = path
        self.app = app or _App(
            {"project_edit": _Route("/edit"), "users": _Route("/users/")}
        )
        self.match_info = {"source_id": "10", "action": "", "item_id": "10"}

    async def post(self):
        return self._post_data

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class _DB:
    def __init__(self, *, scalar_values=None):
        self.scalar_values = list(scalar_values or [])
        self.commits = 0

    async def scalar(self, stmt):
        _ = stmt
        if self.scalar_values:
            return self.scalar_values.pop(0)
        return None

    async def commit(self):
        self.commits += 1


class _App(dict):
    def __init__(self, routes: dict[str, _Route]):
        super().__init__()
        self.router = routes


def _raw(func):
    return func.__wrapped__.__wrapped__.__wrapped__


@pytest.mark.asyncio
async def test_project_edit_get_builds_form_with_initial_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _DB()
    req = _Req(method="GET")
    req["db"] = db
    req["user"] = SimpleNamespace(id=1)

    captured = {}

    async def _session(_request):
        return {"staff_id": 1}

    def _workspace_form(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(validate=lambda: False)

    monkeypatch.setattr(project_views, "get_session", _session)
    monkeypatch.setattr(project_views.forms, "WorkspaceForm", _workspace_form)
    monkeypatch.setattr(
        project_views,
        "_project_context",
        lambda _r: SimpleNamespace(
            title="T",
            system_prompt="SP",
            agent_style="AS",
            provider="openai",
            model="gpt-4o-mini",
            config={"agent_name": "Bot", "welcome_message": "Hi"},
        ),
    )

    payload = await _raw(project_views.project_edit)(req)
    assert "form" in payload
    assert captured["data"]["title"] == "T"


@pytest.mark.asyncio
async def test_project_edit_post_validates_and_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _DB()
    req = _Req(method="POST", post_data={"title": "X"})
    req["db"] = db
    req["user"] = SimpleNamespace(id=1)
    req.app = _App({"project_edit": _Route("/edit")})

    async def _session(_request):
        return {"staff_id": 1}

    class _Form:
        title = SimpleNamespace(data="T")
        system_prompt = SimpleNamespace(data="SP")
        agent_style = SimpleNamespace(data="AS")
        provider = SimpleNamespace(data="openai")
        model = SimpleNamespace(data="gpt-4o-mini")
        agent_name = SimpleNamespace(data="Agent")
        welcome_message = SimpleNamespace(data="Welcome")

        def validate(self):
            return True

    calls = {"updates": None, "flash": []}

    async def _apply(*args):
        calls["updates"] = args[-1]

    async def _flash(_request, message, category="success"):
        calls["flash"].append((message, category))

    monkeypatch.setattr(project_views, "get_session", _session)
    monkeypatch.setattr(project_views.forms, "WorkspaceForm", lambda **kwargs: _Form())
    monkeypatch.setattr(
        project_views,
        "_project_context",
        lambda _r: SimpleNamespace(
            title="", system_prompt="", agent_style="", provider="", model="", config={}
        ),
    )
    monkeypatch.setattr(project_views, "apply_settings_updates", _apply)
    monkeypatch.setattr(project_views, "flash", _flash)

    with pytest.raises(web.HTTPFound):
        await _raw(project_views.project_edit)(req)
    assert db.commits == 1
    assert calls["updates"]["project.title"] == "T"


@pytest.mark.asyncio
async def test_project_source_settings_not_found() -> None:
    req = _Req(method="GET")
    req["db"] = _DB(scalar_values=[None])
    req["user"] = SimpleNamespace(id=1)
    with pytest.raises(web.HTTPNotFound):
        await _raw(project_views.project_source_settings)(req)


@pytest.mark.asyncio
async def test_project_source_settings_post_site_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = SimpleNamespace(
        id=10,
        type="site",
        title="Old",
        uri="https://old",
        reindex_cron="0 3 * * 1",
        config={"rules": [{"type": "contains", "value": "x"}]},
        updated_at=None,
    )
    db = _DB(scalar_values=[source])
    req = _Req(
        method="POST",
        path="/source/10/settings",
        post_data=SimpleNamespace(
            getall=lambda key, default=None: ["contains"]
            if key == "rule_type[]"
            else ["/private"],
        ),
    )
    req["db"] = db
    req["user"] = SimpleNamespace(id=1)

    async def _session(_request):
        return {"staff_id": 1}

    class _Form:
        type = SimpleNamespace(data="site")
        title = SimpleNamespace(data="New")
        reindex_cron = SimpleNamespace(data="")
        url = SimpleNamespace(data="https://example.local")
        user_agent = SimpleNamespace(data="")
        concurrent_requests = SimpleNamespace(data=5)
        download_delay = SimpleNamespace(data=0.2)
        download_timeout = SimpleNamespace(data=20.0)
        aws_access_key_id = SimpleNamespace(data="")
        aws_secret_access_key = SimpleNamespace(data="")
        bucket_name = SimpleNamespace(data="")
        endpoint_url = SimpleNamespace(data="")
        region = SimpleNamespace(data="")
        prefix = SimpleNamespace(data="")
        google_drive_folder_id = SimpleNamespace(data="")
        google_drive_folder_name = SimpleNamespace(data="")

        def validate(self):
            return True

    events = []
    flashes = []

    async def _event(name, _request):
        events.append(name)

    async def _flash(_request, msg, category="success"):
        flashes.append((msg, category))

    monkeypatch.setattr(project_views, "get_session", _session)
    monkeypatch.setattr(
        project_views.forms, "SourceSettingsForm", lambda **kwargs: _Form()
    )
    monkeypatch.setattr(project_views, "admin_event", _event)
    monkeypatch.setattr(project_views, "flash", _flash)
    monkeypatch.setattr(
        project_views, "_project_context", lambda _r: SimpleNamespace(id="global")
    )

    with pytest.raises(web.HTTPFound):
        await _raw(project_views.project_source_settings)(req)
    assert db.commits == 1
    assert source.uri == "https://example.local"
    assert source.reindex_cron == "manual"
    assert source.config.get("crawler_download_timeout") == 20.0
    assert "rules" in source.config
    assert events == ["source_update"]


@pytest.mark.asyncio
async def test_project_topics_post_triggers_delete_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    req = _Req(method="POST", post_data={"topics": "", "intents": ""}, path="/topics")
    req["db"] = _DB()
    req["user"] = SimpleNamespace(id=1)

    async def _session(_request):
        return {"staff_id": 1}

    class _Form:
        topics = SimpleNamespace(data="")
        intents = SimpleNamespace(data="")

        def validate(self):
            return True

    events = []
    flashes = []

    async def _apply(*args):
        return None

    async def _event(name, _request):
        events.append(name)

    async def _flash(_request, msg, category="success"):
        flashes.append((msg, category))

    monkeypatch.setattr(project_views, "get_session", _session)
    monkeypatch.setattr(project_views.forms, "TopicsForm", lambda **kwargs: _Form())
    monkeypatch.setattr(project_views, "_get_topics", lambda _r: ["A"])
    monkeypatch.setattr(project_views, "_get_intents", lambda _r: ["I"])
    monkeypatch.setattr(project_views, "apply_settings_updates", _apply)
    monkeypatch.setattr(project_views, "admin_event", _event)
    monkeypatch.setattr(project_views, "flash", _flash)
    monkeypatch.setattr(
        project_views, "_project_context", lambda _r: SimpleNamespace(id="global")
    )

    with pytest.raises(web.HTTPFound):
        await _raw(project_views.project_topics)(req)
    assert req["db"].commits == 1
    assert events == ["topics_delete"]
