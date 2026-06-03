from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp import web
from yarl import URL

from vchat.models.source_config import CrawlerRule, SourceConfig
from vchat.source_settings import DEFAULT_IGNORED_PARAMS
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
        self.headers = {}
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
        self.added = []

    async def scalar(self, stmt):
        _ = stmt
        if self.scalar_values:
            return self.scalar_values.pop(0)
        return None

    def add(self, value):
        if getattr(value, "id", None) is None:
            value.id = len(self.added) + 1
        self.added.append(value)

    async def commit(self):
        self.commits += 1


class _App(dict):
    def __init__(self, routes: dict[str, _Route]):
        super().__init__()
        self.router = routes


def _raw(func):
    while hasattr(func, "__wrapped__"):
        func = func.__wrapped__
    return func


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
        return {"user_id": 1}

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
        return {"user_id": 1}

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
        blocked_reason=None,
        blocked_message=None,
        reindex_cron="0 3 * * 1",
        config={"rules": [{"type": "contains", "value": "x"}]},
        updated_at=None,
    )
    db = _DB(scalar_values=[source])
    req = _Req(
        method="POST",
        path="/source/10/settings",
        post_data=SimpleNamespace(
            getall=lambda key, default=None: (
                ["param"] * len(DEFAULT_IGNORED_PARAMS) + ["contains"]
                if key == "rule_type[]"
                else [
                    *DEFAULT_IGNORED_PARAMS,
                    "/private",
                ]
            ),
        ),
    )
    req["db"] = db
    req["user"] = SimpleNamespace(id=1)

    async def _session(_request):
        return {"user_id": 1}

    class _Form:
        type = SimpleNamespace(data="site")
        title = SimpleNamespace(data="New")
        reindex_cron = SimpleNamespace(data="")
        url = SimpleNamespace(data="https://example.local")
        user_agent = SimpleNamespace(data="")
        concurrent_requests = SimpleNamespace(data=5)
        download_delay = SimpleNamespace(data=1)
        download_timeout = SimpleNamespace(data=20)
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
    delayed = []

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
        project_views.reapply_source_rules_task,
        "delay",
        lambda source_id: delayed.append(source_id),
    )
    monkeypatch.setattr(
        project_views, "_project_context", lambda _r: SimpleNamespace(id="global")
    )

    with pytest.raises(web.HTTPFound):
        await _raw(project_views.project_source_settings)(req)
    assert db.commits == 1
    assert source.uri == "https://example.local"
    assert source.reindex_cron == "manual"
    assert source.config.crawler_download_delay == 1
    assert source.config.crawler_download_timeout == 20
    assert source.config.rules == [
        *(
            CrawlerRule(type="param", value=param)
            for param in DEFAULT_IGNORED_PARAMS
        ),
        CrawlerRule(type="contains", value="/private"),
    ]
    assert events == ["source_update"]
    assert delayed == [10]


@pytest.mark.asyncio
async def test_add_source_includes_default_ignored_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _PostData(dict):
        def getall(self, key, default=None):
            if key == "rule_type[]":
                return ["regex"]
            if key == "rule_value[]":
                return ["^https://example.local/private"]
            return default or []

    db = _DB()
    req = _Req(method="POST", post_data=_PostData(url="https://example.local"))
    req["db"] = db
    req["user"] = SimpleNamespace(id=1)
    req.headers["X-CSRFToken"] = "token"
    req.app[project_views.SIGNER_KEY] = SimpleNamespace(loads=lambda token, max_age: 1)
    req.match_info["action"] = "add_source"
    req.match_info["item_id"] = "global"

    async def _session(_request):
        return {"user_id": 1}

    class _Form:
        url = SimpleNamespace(data="https://example.local")
        reindex_cron = SimpleNamespace(data="")

        def validate(self):
            return True

    events = []
    delayed = []
    delayed = []

    async def _event(name, _request):
        events.append(name)

    monkeypatch.setattr(project_views, "get_session", _session)
    monkeypatch.setattr(project_views.forms, "SourceForm", lambda *args, **kwargs: _Form())
    monkeypatch.setattr(project_views, "admin_event", _event)
    async def _not_blocked(request, db_session, source):
        _ = request, source
        await db_session.commit()
        return False

    monkeypatch.setattr(project_views, "_check_source_blocking_and_commit", _not_blocked)
    monkeypatch.setattr(
        project_views.crawl_source_task,
        "delay",
        lambda source_id: delayed.append(source_id),
    )

    response = await _raw(project_views.project_action)(req)
    assert response.status == 200
    assert response.headers["HX-Refresh"] == "true"
    assert db.commits == 1
    assert len(db.added) == 1

    source = db.added[0]
    assert source.title == "example.local"
    assert source.reindex_cron == "manual"
    assert source.config == SourceConfig(
        rules=[
            *(
                CrawlerRule(type="param", value=param)
                for param in DEFAULT_IGNORED_PARAMS
            ),
            CrawlerRule(type="regex", value="^https://example.local/private"),
        ]
    )
    assert events == ["source_create"]
    assert delayed == [source.id]


@pytest.mark.asyncio
async def test_add_source_persists_blocked_source_without_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _PostData(dict):
        def getall(self, key, default=None):
            return default or []

    db = _DB()
    req = _Req(method="POST", post_data=_PostData(url="https://blocked.example"))
    req["db"] = db
    req["user"] = SimpleNamespace(id=1)
    req.headers["X-CSRFToken"] = "token"
    req.app[project_views.SIGNER_KEY] = SimpleNamespace(loads=lambda token, max_age: 1)
    req.match_info["action"] = "add_source"
    req.match_info["item_id"] = "global"

    async def _session(_request):
        return {"user_id": 1}

    class _Form:
        url = SimpleNamespace(data="https://blocked.example")
        reindex_cron = SimpleNamespace(data="")

        def validate(self):
            return True

    events = []
    delayed = []

    async def _event(name, _request):
        events.append(name)

    monkeypatch.setattr(project_views, "get_session", _session)
    monkeypatch.setattr(project_views.forms, "SourceForm", lambda *args, **kwargs: _Form())
    monkeypatch.setattr(project_views, "admin_event", _event)
    async def _blocked(request, db_session, source):
        _ = request, db_session, source
        return True

    monkeypatch.setattr(project_views, "_check_source_blocking_and_commit", _blocked)
    monkeypatch.setattr(
        project_views.crawl_source_task,
        "delay",
        lambda source_id: delayed.append(source_id),
    )

    response = await _raw(project_views.project_action)(req)
    assert response.status == 200
    assert response.headers["HX-Refresh"] == "true"
    assert db.commits == 0
    assert len(db.added) == 1
    assert events == ["source_create"]
    assert delayed == []


@pytest.mark.asyncio
async def test_delete_source_rule_removes_rule_and_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = SimpleNamespace(
        id=10,
        config=SourceConfig(
            rules=[
                *(
                    CrawlerRule(type="param", value=param)
                    for param in DEFAULT_IGNORED_PARAMS
                ),
                CrawlerRule(type="regex", value="^https://example.local/private"),
            ]
        ),
        updated_at=None,
    )
    db = _DB(scalar_values=[source])
    req = _Req(
        method="POST",
        post_data={"rule_index": "0"},
    )
    req["db"] = db
    req["user"] = SimpleNamespace(id=1)
    req.headers["X-CSRFToken"] = "token"
    req.app[project_views.SIGNER_KEY] = SimpleNamespace(loads=lambda token, max_age: 1)
    req.match_info["action"] = "delete_source_rule"
    req.match_info["item_id"] = "10"

    events = []
    delayed = []

    async def _event(name, _request):
        events.append(name)

    monkeypatch.setattr(project_views, "admin_event", _event)
    monkeypatch.setattr(
        project_views.reapply_source_rules_task,
        "delay",
        lambda source_id: delayed.append(source_id),
    )

    response = await _raw(project_views.project_action)(req)
    assert response.status == 200
    assert db.commits == 1
    assert source.config.rules == [
        *(
            CrawlerRule(type="param", value=param)
            for param in DEFAULT_IGNORED_PARAMS[1:]
        ),
        CrawlerRule(type="regex", value="^https://example.local/private"),
    ]
    assert delayed == [10]
    assert source.updated_at is not None
    assert events == ["source_update"]
