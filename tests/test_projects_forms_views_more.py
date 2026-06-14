from __future__ import annotations

import html
import re
from types import SimpleNamespace

import pytest
from aiohttp import web
from yarl import URL

from vchat.models.source_config import CrawlerRule, SourceConfig
from jobs.crawler.source_settings import DEFAULT_IGNORED_PARAMS
from vchat.views.projects import forms as project_forms
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


def test_normalize_source_origin_keeps_only_domain() -> None:
    assert (
        project_forms.normalize_source_origin(
            "https://Example.Local:8443/docs/page?x=1#section"
        )
        == "https://example.local:8443"
    )


def test_source_form_exposes_enable_triggers_checkbox() -> None:
    form = project_forms.SourceForm(meta={"csrf_context": {}})

    assert hasattr(form, "enable_triggers")
    assert form.enable_triggers.data is False
    assert form.enable_triggers.label.text == "Разрешить пользовательские триггеры"


def test_pinned_messages_from_form_keeps_three_messages() -> None:
    data = SimpleNamespace(
        getall=lambda key, default=None: (
            ["One", "", "Three", "Four"]
            if key == "pinned_text[]"
            else ["primary", "bad", "warning", "success"]
        )
    )

    messages = project_views._pinned_messages_from_form(data)

    assert messages == [
        {"text": "One", "color": "primary"},
        {"text": "Three", "color": "warning"},
    ]


def test_pinned_messages_from_form_sanitizes_rich_text_and_limits_length() -> None:
    data = SimpleNamespace(
        getall=lambda key, default=None: (
            [
                '<strong>Bold</strong> <a href="example.com">bad</a> '
                '<a href="https://example.com">ok</a><script>x</script>'
                + ("x" * 500)
            ]
            if key == "pinned_text[]"
            else ["success"]
        )
    )

    messages = project_views._pinned_messages_from_form(data)

    assert messages[0]["color"] == "success"
    assert "<strong>Bold</strong>" in messages[0]["text"]
    assert '<a href="https://example.com"' in messages[0]["text"]
    assert "script" not in messages[0]["text"]
    assert "href=\"example.com\"" not in messages[0]["text"]
    assert len(html.unescape(re.sub("<[^>]+>", "", messages[0]["text"]))) == 400


def test_widget_footer_text_from_form_sanitizes_rich_text() -> None:
    data = {
        "footer_text": (
            '<strong>Правила</strong> '
            '<a href="https://vbudushee.ru/faq/">Пользовательское соглашение</a>'
            "<br><b>Важно</b>"
            '<script>alert(1)</script>'
        )
    }

    footer_text = project_views._widget_footer_text_from_form(data)

    assert "<strong>Правила</strong>" in footer_text
    assert '<a href="https://vbudushee.ru/faq/"' in footer_text
    assert "<br>" in footer_text
    assert "<b>Важно</b>" in footer_text
    assert "script" not in footer_text
    assert "alert" not in footer_text


def test_widget_text_defaults_match_creation_contract() -> None:
    assert (
        project_views.forms.DEFAULT_WIDGET_FOOTER_TEXT
        == "Отправить Enter, новая строка Shift+Enter"
    )
    assert project_views.forms.DEFAULT_SYSTEM_PROMPT == (
        "Ты дружелюбный ИИ-ассистент. Тон общения: дружелюбный, открытый, "
        "спокойный, вдохновляющий и экспертный без высокомерия. Объясняй ясно и по "
        "делу, подсвечивай полезные следующие шаги, показывай технологию как "
        "удобный инструмент для человека. Используй только простое Markdown-"
        "форматирование: обычный текст, короткие списки, **жирный**, *курсив*, "
        "inline-code, блоки кода и ссылки. Не возвращай HTML, SVG, iframe, "
        "style/script-теги, обработчики событий или JavaScript-ссылки; если нужно "
        "обсудить HTML/JS, показывай его как обычный текст или внутри блока кода. "
        "Если данных не хватает, задай короткий уточняющий вопрос."
    )


def test_welcome_messages_from_form_sanitizes_many_rich_text_items() -> None:
    data = SimpleNamespace(
        getall=lambda key, default=None: (
            [
                '<strong>Первое</strong><script>x</script>',
                "",
                '<a href="https://vbudushee.ru/faq/">Второе</a>',
                '<b>Третье</b>' + ("x" * 2500),
            ]
            if key == "welcome_text[]"
            else []
        )
    )

    messages = project_views._welcome_messages_from_form(data)

    assert len(messages) == 3
    assert "<strong>Первое</strong>" in messages[0]
    assert "script" not in messages[0]
    assert "x" not in messages[0]
    assert '<a href="https://vbudushee.ru/faq/"' in messages[1]
    assert "<b>Третье</b>" in messages[2]
    assert len(html.unescape(re.sub("<[^>]+>", "", messages[2]))) == 2000


def test_welcome_messages_from_form_falls_back_to_default() -> None:
    data = SimpleNamespace(getall=lambda key, default=None: [""] if key else [])

    assert project_views._welcome_messages_from_form(data) == [
        project_views.DEFAULT_WIDGET_WELCOME_MESSAGE
    ]


def test_waiting_messages_from_form_sanitizes_plain_text_items() -> None:
    data = SimpleNamespace(
        getall=lambda key, default=None: (
            [
                "Готовлю ответ",
                "",
                "<strong>Проверяю источники</strong>",
                "<script>alert(1)</script>Подбираю материалы",
                "x" * 160,
            ]
            if key == "waiting_text[]"
            else []
        )
    )

    messages = project_views._waiting_messages_from_form(data)

    assert messages[:3] == [
        "Готовлю ответ",
        "Проверяю источники",
        "Подбираю материалы",
    ]
    assert "<" not in "".join(messages)
    assert "alert" not in "".join(messages)
    assert len(messages[3]) == 120


def test_waiting_messages_from_form_falls_back_to_default() -> None:
    data = SimpleNamespace(getall=lambda key, default=None: [""] if key else [])

    assert project_views._waiting_messages_from_form(data) == [
        project_views.DEFAULT_WIDGET_WAITING_MESSAGE
    ]


def test_random_welcome_message_uses_sanitized_message_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_messages = []

    def _choice(messages):
        seen_messages.append(messages)
        return messages[0]

    monkeypatch.setattr(project_views.secrets, "choice", _choice)

    message = project_views._random_welcome_message(
        ["<script>x</script>", "<strong>Второе</strong>"]
    )

    assert seen_messages == [["<strong>Второе</strong>"]]
    assert message == "<strong>Второе</strong>"


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
        config=SourceConfig.from_dict({"rules": [{"type": "contains", "value": "x"}]}),
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
        ignore_robots_txt = SimpleNamespace(data=True)
        enable_triggers = SimpleNamespace(data=True)
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

    async def _apply_source_trigger_rules(*_args):
        return 0

    monkeypatch.setattr(
        project_views, "apply_source_trigger_rules", _apply_source_trigger_rules
    )

    with pytest.raises(web.HTTPFound):
        await _raw(project_views.project_source_settings)(req)
    assert db.commits == 1
    assert source.uri == "https://example.local"
    assert source.reindex_cron == "manual"
    assert source.config.crawler_download_delay == 1
    assert source.config.crawler_download_timeout == 20
    assert source.config.ignore_robots_txt is True
    assert source.enable_triggers is True
    assert source.config.trigger_rules == [
        CrawlerRule(type="regex", value=project_views.DEFAULT_SOURCE_TRIGGER_PATTERN)
    ]
    assert source.config.rules == [
        *(CrawlerRule(type="param", value=param) for param in DEFAULT_IGNORED_PARAMS),
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
        enable_triggers = SimpleNamespace(data=False)

        def validate(self):
            return True

    events = []
    delayed = []
    delayed = []

    async def _event(name, _request):
        events.append(name)

    monkeypatch.setattr(project_views, "get_session", _session)
    monkeypatch.setattr(
        project_views.forms, "SourceForm", lambda *args, **kwargs: _Form()
    )
    monkeypatch.setattr(project_views, "admin_event", _event)

    async def _not_blocked(request, db_session, source):
        _ = request, source
        await db_session.commit()
        return False

    monkeypatch.setattr(
        project_views, "_check_source_blocking_and_commit", _not_blocked
    )
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
        enable_triggers = SimpleNamespace(data=False)

        def validate(self):
            return True

    events = []
    delayed = []

    async def _event(name, _request):
        events.append(name)

    monkeypatch.setattr(project_views, "get_session", _session)
    monkeypatch.setattr(
        project_views.forms, "SourceForm", lambda *args, **kwargs: _Form()
    )
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
