from __future__ import annotations

import json
import pytest
from aiohttp import web
from sqlalchemy.exc import IntegrityError
from types import SimpleNamespace
from yarl import URL

from vchat.settings import CONFIG_KEY, SIGNER_KEY
from vchat.settings import REDIS_KEY
from vchat.widget_state import (
    WIDGET_STATE_DISABLED,
    WIDGET_STATE_MISSING,
    WIDGET_STATE_TTL_SECONDS,
    widget_state_key,
)
from vchat.models.source_config import CrawlerRule, SourceConfig
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


class _FakeApp(dict):
    def __init__(self, *, redis=None, router=None):
        super().__init__()
        if redis is not None:
            self[REDIS_KEY] = redis
        self.router = router or _FakeRouter()


class _FakeRedis:
    def __init__(self, cached_state: str | bytes | None = None) -> None:
        self.cached_state = cached_state
        self.set_calls = []

    async def get(self, key):
        self.get_key = key
        return self.cached_state

    async def set(self, key, value, *, ex):
        self.set_calls.append((key, value, ex))


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
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: self._events)
        )

    async def scalar(self, stmt):
        _ = stmt
        return self._scalar_values.pop(0) if self._scalar_values else 0


class _FakeScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)

    def all(self):
        return self._rows


class _FakeExecuteResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _FakeScalarResult(self._rows)

    def all(self):
        return self._rows


class _TriggerResolveRequest(dict):
    def __init__(self, *, db, url, widget_page_discovery_enabled: bool = False):
        super().__init__()
        if not hasattr(db, "rollback"):
            db.rollbacks = 0

            async def _rollback():
                db.rollbacks += 1

            db.rollback = _rollback
        self["db"] = db
        self["app"] = {
            CONFIG_KEY: {
                "widget_page_discovery_enabled": widget_page_discovery_enabled,
            },
            SIGNER_KEY: SimpleNamespace(
                dumps=lambda value, salt=None: f"signed:{salt}:{value}"
            ),
        }
        self.query = {"url": url, "title": "Title"}

    @property
    def app(self):
        return self["app"]


@pytest.mark.asyncio
async def test_frontend_healthcheck_redirects_to_project_view() -> None:
    request = _FakeRequest(
        {
            "db": SimpleNamespace(execute=lambda *_a, **_k: _AsyncNoop()),
            "app": SimpleNamespace(
                router=_FakeRouter({"project_view": _FakeRouterItem("/")})
            ),
        }
    )
    with pytest.raises(web.HTTPFound) as exc_info:
        await frontend.healthcheck(request)  # type: ignore[arg-type]
    response = exc_info.value
    assert response.status == 302
    assert str(response.location) == "/"


@pytest.mark.asyncio
async def test_frontend_widget_js_renders_with_widget_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    def _render(template, request, context):
        captured["template"] = template
        captured["context"] = context
        return "ok"

    monkeypatch.setattr(frontend.aiohttp_jinja2, "render_template", _render)

    class _Db:
        async def scalar(self, stmt):
            _ = stmt
            return 1

        async def rollback(self):
            return None

    request = _FakeRequest(
        {
            "db": _Db(),
            "match_info": {"code": "widget-code"},
            "app": _FakeApp(
                redis=_FakeRedis(),
                router=_FakeRouter(
                    {
                        "public_widget_chat": _FakeRouterItem("/chat/widget"),
                        "widget_triggers_resolve": _FakeRouterItem("/widget/triggers"),
                    }
                )
            ),
        }
    )
    result = await frontend.widget_js(request)
    assert result == "ok"
    assert captured["template"] == "js/widget.js"
    assert captured["context"]["widget_chat_path"] == "/chat/widget"
    assert captured["context"]["widget_code"] == "widget-code"


@pytest.mark.asyncio
async def test_frontend_widget_js_returns_neutral_script_when_disabled() -> None:
    class _Db:
        scalar_called = False

        async def scalar(self, stmt):
            _ = stmt
            self.scalar_called = True
            return True

        async def rollback(self):
            return None

    redis = _FakeRedis(WIDGET_STATE_DISABLED.encode("utf-8"))
    db = _Db()
    request = _FakeRequest(
        {
            "db": db,
            "match_info": {"code": "widget-code"},
            "app": _FakeApp(redis=redis),
        }
    )

    response = await frontend.widget_js(request)

    assert response.status == 200
    assert response.content_type == "application/javascript"
    assert "vchat widget is disabled." in response.text
    assert "console.info" in response.text
    assert db.scalar_called is False
    assert redis.get_key == widget_state_key("widget-code")


@pytest.mark.asyncio
async def test_frontend_widget_js_caches_missing_widget_state() -> None:
    class _Db:
        rolled_back = False

        async def scalar(self, stmt):
            _ = stmt
            return None

        async def rollback(self):
            self.rolled_back = True

    redis = _FakeRedis()
    db = _Db()
    request = _FakeRequest(
        {
            "db": db,
            "match_info": {"code": "removed-code"},
            "app": _FakeApp(redis=redis),
        }
    )

    response = await frontend.widget_js(request)

    assert response.status == 200
    assert "vchat widget was removed. Please remove this embed code." in response.text
    assert "console.warn" in response.text
    assert db.rolled_back is True
    assert redis.set_calls == [
        (widget_state_key("removed-code"), WIDGET_STATE_MISSING, WIDGET_STATE_TTL_SECONDS)
    ]


@pytest.mark.asyncio
async def test_frontend_widget_js_caches_disabled_db_state() -> None:
    class _Db:
        async def scalar(self, stmt):
            _ = stmt
            return False

        async def rollback(self):
            return None

    redis = _FakeRedis()
    request = _FakeRequest(
        {
            "db": _Db(),
            "match_info": {"code": "widget-code"},
            "app": _FakeApp(redis=redis),
        }
    )

    response = await frontend.widget_js(request)

    assert response.status == 200
    assert "vchat widget is disabled." in response.text
    assert redis.set_calls == [
        (widget_state_key("widget-code"), WIDGET_STATE_DISABLED, WIDGET_STATE_TTL_SECONDS)
    ]


@pytest.mark.asyncio
async def test_demo_page_lists_widget_codes() -> None:
    class _Db:
        rolled_back = False
        results = [
            _FakeExecuteResult([(1, "Main widget", "main-widget")]),
            _FakeExecuteResult(
                [
                    SimpleNamespace(
                        id=20,
                        uri="https://example.com/docs/page",
                        title="Docs page",
                        has_triggers=True,
                        triggers=[
                            {
                                "key": "docs",
                                "text": "Ask about docs",
                                "source": "manual",
                            }
                        ],
                    )
                ]
            ),
        ]

        async def execute(self, stmt):
            _ = stmt
            return self.results.pop(0)

        async def rollback(self):
            self.rolled_back = True

    db = _Db()
    request = _FakeRequest(
        {
            "db": db,
            "query": {"code": "main-widget"},
            "app": _FakeApp(
                router=_FakeRouter(
                    {"widget_triggers_resolve": _FakeRouterItem("/widget/triggers")}
                )
            ),
        }
    )
    context = await frontend.demo_page.__wrapped__(request)
    assert context["widgets"] == [
        {"id": 1, "name": "Main widget", "code": "main-widget"}
    ]
    assert context["trigger_pages"] == [
        {
            "id": 20,
            "title": "Docs page",
            "uri": "https://example.com/docs/page",
        }
    ]
    assert context["selected_widget_code"] == "main-widget"
    assert context["selected_trigger_url"] == ""
    assert context["selected_trigger_url_is_listed"] is False
    assert db.rolled_back is True


@pytest.mark.asyncio
async def test_widget_triggers_resolve_returns_empty_for_disabled_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Db:
        async def execute(self, stmt):
            _ = stmt
            return _FakeExecuteResult(
                [
                    SimpleNamespace(
                        id=10,
                        uri="https://example.com/",
                        enable_triggers=False,
                        config=SourceConfig(),
                    )
                ]
            )

    async def _find_page_by_url(*_args):
        return None

    monkeypatch.setattr(frontend, "find_page_by_url", _find_page_by_url)

    response = await frontend.widget_triggers_resolve(
        _TriggerResolveRequest(db=_Db(), url="https://example.com/docs/page")
    )

    assert response.status == 200
    assert response.text is not None
    payload = json.loads(response.text)
    assert "source" not in payload
    assert "page_token" not in payload
    assert payload["triggers"] == []


@pytest.mark.asyncio
async def test_widget_triggers_resolve_returns_empty_when_rules_do_not_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Db:
        async def execute(self, stmt):
            _ = stmt
            return _FakeExecuteResult(
                [
                    SimpleNamespace(
                        id=10,
                        uri="https://example.com/",
                        enable_triggers=True,
                        config=SourceConfig(
                            trigger_rules=[
                                CrawlerRule(type="regex", value=r"^/docs/.*")
                            ],
                        ),
                    )
                ]
            )

    async def _find_page_by_url(*_args):
        return SimpleNamespace(
            id=20,
            title="Blog",
            source_id=10,
            has_triggers=False,
            triggers=None,
        )

    monkeypatch.setattr(frontend, "find_page_by_url", _find_page_by_url)

    response = await frontend.widget_triggers_resolve(
        _TriggerResolveRequest(db=_Db(), url="https://example.com/blog/page")
    )

    assert response.status == 200
    assert response.text is not None
    payload = json.loads(response.text)
    assert "source" not in payload
    assert "page_token" not in payload
    assert payload["triggers"] == []


@pytest.mark.asyncio
async def test_widget_triggers_resolve_signs_page_id_for_page_triggers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = SimpleNamespace(
        id=10,
        uri="https://example.com/",
        enable_triggers=True,
        config=SourceConfig(
            trigger_rules=[CrawlerRule(type="regex", value=r"^/docs/.*")]
        ),
    )

    class _Db:
        async def execute(self, unused_stmt):
            return _FakeExecuteResult([source])

    async def _find_page_by_url(*_args):
        return SimpleNamespace(
            id=20,
            title="Docs",
            source_id=10,
            has_triggers=True,
            triggers=[{"key": "abc", "text": "Ask about docs", "source": "manual"}],
        )

    monkeypatch.setattr(frontend, "find_page_by_url", _find_page_by_url)

    response = await frontend.widget_triggers_resolve(
        _TriggerResolveRequest(db=_Db(), url="https://example.com/docs/page")
    )

    assert response.status == 200
    assert response.text is not None
    payload = json.loads(response.text)
    assert "page_id" not in payload
    assert "source" not in payload
    assert payload["page_token"] == "signed:trigger_page:20"
    assert payload["triggers"] == [
        {
            "key": "abc",
            "text": "Ask about docs",
        }
    ]
    assert "page_id" not in payload["triggers"][0]
    assert "page_token" not in payload["triggers"][0]
    assert "source" not in payload["triggers"][0]


@pytest.mark.asyncio
async def test_widget_triggers_resolve_uses_empty_default_title_for_untitled_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = SimpleNamespace(
        id=10,
        uri="https://example.com/",
        enable_triggers=True,
        config=SourceConfig(
            trigger_rules=[CrawlerRule(type="regex", value=r"^/docs/.*")]
        ),
    )

    class _Db:
        async def execute(self, unused_stmt):
            return _FakeExecuteResult([source])

    async def _find_page_by_url(*_args):
        return SimpleNamespace(
            id=20,
            title=None,
            source_id=10,
            has_triggers=False,
            triggers=None,
        )

    captured = {}

    def _render_default_triggers(templates, title):
        captured["templates"] = templates
        captured["title"] = title
        return [{"key": "default", "text": title}]

    request = _TriggerResolveRequest(db=_Db(), url="https://example.com/docs/page")
    request.query["title"] = ""

    monkeypatch.setattr(frontend, "find_page_by_url", _find_page_by_url)
    monkeypatch.setattr(frontend, "load_default_trigger_templates", lambda app: ["tpl"])
    monkeypatch.setattr(frontend, "render_default_triggers", _render_default_triggers)

    response = await frontend.widget_triggers_resolve(request)

    assert response.status == 200
    assert captured == {"templates": ["tpl"], "title": ""}


@pytest.mark.asyncio
async def test_widget_triggers_resolve_discovers_matching_unknown_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = SimpleNamespace(
        id=10,
        uri="https://example.com/",
        enable_triggers=True,
        config=SourceConfig(
            trigger_rules=[CrawlerRule(type="regex", value=r"^/docs/.*")]
        ),
    )

    class _Db:
        def __init__(self):
            self.added = []
            self.commits = 0

        async def execute(self, stmt):
            _ = stmt
            return _FakeExecuteResult([source])

        def add(self, obj):
            self.added.append(obj)

        async def flush(self):
            self.added[-1].id = 99

        async def commit(self):
            self.commits += 1

    find_calls = []

    async def _find_page_by_url(*_args):
        find_calls.append(_args)
        return None

    delayed = []
    monkeypatch.setattr(frontend, "find_page_by_url", _find_page_by_url)
    monkeypatch.setattr(frontend.crawl_page_task, "delay", delayed.append)

    db = _Db()
    response = await frontend.widget_triggers_resolve(
        _TriggerResolveRequest(
            db=db,
            url="https://example.com/docs/page#part",
            widget_page_discovery_enabled=True,
        )
    )

    assert response.status == 200
    assert response.text is not None
    payload = json.loads(response.text)
    assert "page_id" not in payload
    assert "source" not in payload
    assert db.commits == 1
    assert delayed == [99]
    assert len(find_calls) == 2
    page = db.added[0]
    assert page.uri == "https://example.com/docs/page"
    assert page.source_id == 10
    assert page.has_triggers is True
    assert page.discover_by == "widget"
    assert page.discover_source == "https://example.com/docs/page"


@pytest.mark.asyncio
async def test_widget_triggers_resolve_skips_discovery_when_config_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = SimpleNamespace(
        id=10,
        uri="https://example.com/",
        enable_triggers=True,
        config=SourceConfig(
            trigger_rules=[CrawlerRule(type="regex", value=r"^/docs/.*")]
        ),
    )

    class _Db:
        async def execute(self, stmt):
            _ = stmt
            return _FakeExecuteResult([source])

        def add(self, obj):
            raise AssertionError(f"unexpected page creation: {obj!r}")

    async def _find_page_by_url(*_args):
        return None

    delayed = []
    monkeypatch.setattr(frontend, "find_page_by_url", _find_page_by_url)
    monkeypatch.setattr(frontend.crawl_page_task, "delay", delayed.append)

    response = await frontend.widget_triggers_resolve(
        _TriggerResolveRequest(db=_Db(), url="https://example.com/docs/page")
    )

    assert response.status == 200
    assert response.text is not None
    payload = json.loads(response.text)
    assert "page_id" not in payload
    assert "source" not in payload
    assert "page_token" not in payload
    assert delayed == []


@pytest.mark.asyncio
async def test_widget_triggers_resolve_handles_concurrent_discovery_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = SimpleNamespace(
        id=10,
        uri="https://example.com/",
        enable_triggers=True,
        config=SourceConfig(
            trigger_rules=[CrawlerRule(type="regex", value=r"^/docs/.*")]
        ),
    )
    existing_page = SimpleNamespace(
        id=77,
        title=None,
        source_id=10,
        has_triggers=True,
        triggers=[],
    )

    class _Db:
        def __init__(self):
            self.rollbacks = 0
            self.commits = 0

        async def execute(self, stmt):
            _ = stmt
            return _FakeExecuteResult([source])

        def add(self, obj):
            _ = obj

        async def flush(self):
            raise IntegrityError("insert page", {}, Exception("duplicate uri"))

        async def rollback(self):
            self.rollbacks += 1

        async def commit(self):
            self.commits += 1

    pages_by_call = [None, None, existing_page]

    async def _find_page_by_url(*_args):
        return pages_by_call.pop(0)

    delayed = []
    monkeypatch.setattr(frontend, "find_page_by_url", _find_page_by_url)
    monkeypatch.setattr(frontend.crawl_page_task, "delay", delayed.append)

    db = _Db()
    response = await frontend.widget_triggers_resolve(
        _TriggerResolveRequest(
            db=db,
            url="https://example.com/docs/page",
            widget_page_discovery_enabled=True,
        )
    )

    assert response.status == 200
    assert response.text is not None
    payload = json.loads(response.text)
    assert "page_id" not in payload
    assert "source" not in payload
    assert db.rollbacks == 2
    assert db.commits == 0
    assert delayed == []


@pytest.mark.asyncio
async def test_admin_event_list_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
    db = _FakeDb([2], events=events)

    async def _fake_session(_request):
        return {"user_id": 1}

    monkeypatch.setattr(vchat_utils, "get_session", _fake_session)
    request = _FakeRequest(
        {
            "db": db,
            "user": SimpleNamespace(id=1),
            "auth_session": {"user_id": 1},
            "rel_url": SimpleNamespace(query={"page": "1"}),
            "path": "/events/",
            "app": SimpleNamespace(
                router=_FakeRouter({"login": _FakeRouterItem("/login/")})
            ),
        }
    )
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
    monkeypatch.setattr(admin_views, "UserAdd", lambda meta=None: {"meta": meta})

    request = _FakeRequest(
        {
            "db": SimpleNamespace(),
            "user": SimpleNamespace(id=1),
            "auth_session": {"user_id": 1},
            "path": "/users/",
            "app": SimpleNamespace(
                router=_FakeRouter({"login": _FakeRouterItem("/login/")})
            ),
        }
    )
    raw_user_list = admin_views.user_list.__wrapped__.__wrapped__.__wrapped__
    context = await raw_user_list(request)  # type: ignore[arg-type]
    assert context["total_users"] == 2
    assert context["current_user_id"] == 1
    assert context["add_form"]["meta"] is not None


@pytest.mark.asyncio
async def test_get_api_client_sources_returns_lightweight_rows() -> None:
    class _Db:
        async def execute(self, unused_stmt):
            return _FakeExecuteResult([(7, "Docs", "https://example.com")])

    sources = await admin_views._get_api_client_sources(_Db())

    assert sources == [
        SimpleNamespace(id=7, title="Docs", uri="https://example.com")
    ]


class _AsyncNoop:
    def __await__(self):
        async def _coro():
            return None

        return _coro().__await__()
