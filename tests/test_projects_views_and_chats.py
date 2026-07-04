from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import jinja2
import pytest
from aiohttp import web
from yarl import URL

from vchat.views.projects import chats as chats_views
from vchat.views.projects import forms as project_forms
from vchat.views.projects import views as project_views


def _signed_widget_user_info(secret: str, **overrides) -> str:
    payload = {
        "timestamp": int(datetime.now(timezone.utc).timestamp()),
        "user_uid": "user-123",
        "user_name": "Иван Иванов",
        "user_email": "ivan@example.com",
    }
    payload.update(overrides)
    message = project_views._widget_user_info_signature_payload(payload)
    payload["signature"] = hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class _Req:
    def __init__(self, **data):
        self._data = {}
        self.query = data.pop("query", {})
        self.match_info = data.pop("match_info", {})
        for key, value in data.items():
            self._data[key] = value

    def __getitem__(self, key):
        return self._data[key]

    def __setitem__(self, key, value):
        self._data[key] = value

    def get(self, key, default=None):
        return self._data.get(key, default)


@pytest.mark.asyncio
async def test_chats_list_returns_active_chats(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Redis:
        async def smembers(self, key):
            _ = key
            return {"chat-1"}

    class _Result:
        def scalars(self):
            class _S:
                def all(self):
                    return [SimpleNamespace(id="chat-1")]

            return _S()

    class _Db:
        async def execute(self, stmt):
            _ = stmt
            return _Result()

    class _Factory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return _Db()

        async def __aexit__(self, exc_type, exc, tb):
            _ = exc_type, exc, tb
            return False

    monkeypatch.setattr(chats_views, "redis", _Redis())
    monkeypatch.setattr(chats_views, "async_session_factory", _Factory())
    raw = chats_views.chats_list.__wrapped__.__wrapped__.__wrapped__
    payload = await raw(
        _Req(user=SimpleNamespace(id=1), app={"login": None}, path="/chats")
    )
    assert "project" not in payload
    assert payload["active_chats"]


@pytest.mark.asyncio
async def test_history_list_builds_pagination_and_filters() -> None:
    now = datetime.now(timezone.utc)
    fake_chat = SimpleNamespace(
        id="c1",
        created_at=now,
        title="t",
        user_uid="u",
        meta={},
    )

    class _RowsResult:
        def all(self):
            return [
                SimpleNamespace(
                    Chat=fake_chat,
                    upvotes=1,
                    downvotes=0,
                    guardrail_hits=1,
                    message_count=3,
                    token_count=42,
                )
            ]

    class _Db:
        async def scalar(self, stmt):
            _ = stmt
            return 1

        async def execute(self, stmt):
            _ = stmt
            return _RowsResult()

    class _Route:
        def __init__(self, path: str):
            self.path = path

        def url_for(self):
            return URL(self.path)

    class _Router(dict):
        def __getitem__(self, key):
            return super().__getitem__(key)

    class _App(dict):
        def __init__(self, *args, router=None, **kwargs):
            super().__init__(*args, **kwargs)
            self.router = router or {}

    class _HistoryReq:
        def __init__(self):
            self.query = {
                "page": "1",
                "search": "отпуск",
                "date_from": "2026/03",
                "date_to": "2026/01",
                "guardrail": "1",
                "guardrail_reason": "passport_ru",
            }
            self.app = _App(router=_Router({"project_history": _Route("/history")}))
            self._store = {"db": _Db()}

        def __getitem__(self, item):
            return self._store[item]

        def __setitem__(self, key, value):
            self._store[key] = value

    request = _HistoryReq()

    raw = chats_views.history_list.__wrapped__.__wrapped__.__wrapped__
    payload = await raw(request)
    assert "project" not in payload
    assert payload["pagination"]["total"] == 1
    assert payload["pagination"]["page"] == 1
    assert payload["guardrail_filter"] is True
    assert payload["date_from"] == "2026/01"
    assert payload["date_to"] == "2026/03"
    assert payload["chats"][0].guardrail_triggered is True
    assert payload["chats"][0].message_count == 3
    assert payload["chats"][0].token_count == 42


@pytest.mark.asyncio
async def test_history_detail_masks_pii_and_maps_guardrail_labels() -> None:
    chat = SimpleNamespace(id="chat-1", title="Demo", meta={})
    msgs = [
        SimpleNamespace(
            role="user",
            text="Мой паспорт 12 34 567890",
            full_context="",
            guardrail_reasons=None,
            guardrail_triggered=False,
            guardrail_stage=None,
        ),
        SimpleNamespace(
            role="assistant",
            text="Ответ",
            full_context="guardrail_blocked_output|passport_ru",
            guardrail_reasons=["passport_ru"],
            guardrail_triggered=True,
            guardrail_stage="output",
        ),
    ]

    class _Scalars:
        def all(self):
            return msgs

    class _Res:
        def scalars(self):
            return _Scalars()

    class _Db:
        async def scalar(self, stmt):
            _ = stmt
            return chat

        async def execute(self, stmt):
            _ = stmt
            return _Res()

    request = _Req(db=_Db(), match_info={"chat_id": "chat-1"})

    raw = chats_views.history_detail.__wrapped__.__wrapped__.__wrapped__
    payload = await raw(request)
    assert "project" not in payload
    assert payload["chat"].id == "chat-1"
    assert isinstance(payload["messages"][0].has_masked_pii, bool)
    assert payload["messages"][1].guardrail_hit is True
    assert payload["messages"][1].guardrail_rules


@pytest.mark.asyncio
async def test_history_detail_uses_used_chunks_snapshot_and_marks_deleted() -> None:
    chat = SimpleNamespace(id="chat-1", title="Demo", meta={})
    msgs = [
        SimpleNamespace(
            role="assistant",
            text="Ответ",
            full_context="",
            used_chunks=[
                {
                    "citation_id": 0,
                    "uri": "https://docs.example.com/a",
                    "page_url": "https://docs.example.com/a",
                    "title": "Doc A",
                    "source_title": "Friendly Docs",
                    "display_path": "Doc A / Section",
                    "summary": "Короткое описание страницы.",
                    "section_path": "Section",
                    "kind": "text",
                }
            ],
            guardrail_reasons=None,
            guardrail_triggered=False,
            guardrail_stage=None,
        )
    ]

    class _Scalars:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _Res:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return _Scalars(self._rows)

        def __iter__(self):
            return iter(self._rows)

    class _Db:
        def __init__(self):
            self.calls = 0

        async def scalar(self, stmt):
            _ = stmt
            return chat

        async def execute(self, stmt):
            _ = stmt
            self.calls += 1
            if self.calls == 1:
                return _Res(msgs)
            return _Res([])

    request = _Req(db=_Db(), match_info={"chat_id": "chat-1"})

    raw = chats_views.history_detail.__wrapped__.__wrapped__.__wrapped__
    payload = await raw(request)
    assert "project" not in payload
    source = payload["messages"][0].context_sources[0]
    assert source["page_url"] == "https://docs.example.com/a"
    assert source["source_title"] == "Friendly Docs"
    assert source["display_path"] == "Doc A / Section"
    assert source["summary"] == "Короткое описание страницы."
    assert source["page_deleted"] is True


@pytest.mark.asyncio
async def test_history_detail_404_when_chat_missing() -> None:
    class _Db:
        async def scalar(self, stmt):
            _ = stmt
            return None

    raw = chats_views.history_detail.__wrapped__.__wrapped__.__wrapped__
    with pytest.raises(web.HTTPNotFound):
        await raw(_Req(db=_Db(), match_info={"chat_id": "x"}))


def test_history_detail_template_renders_vote_icons_without_feedback_text() -> None:
    templates_dir = Path(__file__).resolve().parents[1] / "vchat" / "templates"
    env = jinja2.Environment(
        loader=jinja2.ChoiceLoader(
            [
                jinja2.DictLoader(
                    {"admin.html": "{% block content %}{% endblock %}"}
                ),
                jinja2.FileSystemLoader(str(templates_dir)),
            ]
        ),
        autoescape=True,
    )
    env.globals.update(
        _=lambda value: value,
        url=lambda name, **kwargs: URL(
            f"/history/{kwargs['chat_id']}"
            if name == "project_history_detail"
            else "/history"
        ),
    )
    now = datetime(2026, 5, 31, 15, 30, 43, tzinfo=timezone.utc)
    messages = [
        SimpleNamespace(
            role="assistant",
            created_at=now,
            has_masked_pii=False,
            text_display="Ответ",
            text="Ответ",
            guardrail_hit=False,
            context_sources=[],
            vote=True,
        ),
        SimpleNamespace(
            role="assistant",
            created_at=now,
            has_masked_pii=False,
            text_display="Ответ",
            text="Ответ",
            guardrail_hit=False,
            context_sources=[],
            vote=False,
        ),
    ]

    rendered = env.get_template("projects/history_detail.html").render(
        chat=SimpleNamespace(id="chat-1", title="Demo", user_uid="u", created_at=now),
        chat_meta={},
        messages=messages,
    )

    assert 'icon="lucide:thumbs-up"' in rendered
    assert 'icon="lucide:thumbs-down"' in rendered
    assert "Полезно" not in rendered
    assert "Не полезно" not in rendered
    assert "Обратная связь" not in rendered
    assert 'style="width: 90%' not in rendered
    assert "chat-footer" not in rendered
    assert "justify-self: stretch" not in rendered


def test_widget_edit_template_renders_pinned_message_color_options() -> None:
    templates_dir = Path(__file__).resolve().parents[1] / "vchat" / "templates"
    env = jinja2.Environment(
        loader=jinja2.ChoiceLoader(
            [
                jinja2.DictLoader(
                    {"admin.html": "{% block content %}{% endblock %}"}
                ),
                jinja2.FileSystemLoader(str(templates_dir)),
            ]
        ),
        autoescape=True,
    )
    env.globals.update(
        url=lambda name, **kwargs: URL(
            f"/actions/{kwargs['action']}/{kwargs['item_id']}"
            if name == "actions"
            else f"/integration/{kwargs['widget_id']}"
            if name == "project_widget_edit"
            else "/integration"
        ),
        csrf_token=lambda: "token",
    )
    template = env.get_template("projects/widget_edit.html")
    widget = SimpleNamespace(
        id=1,
        name="Widget",
        code="abc",
        secret="widget-secret",
        agent_name="Agent",
        is_enabled=True,
        welcome_messages=["Hello", "<strong>Second</strong>"],
        waiting_messages=["Готовлю ответ", "Проверяю источники"],
        error_message="<strong>Ошибка</strong>",
        footer_text='Footer <a href="https://vbudushee.ru/faq/">link</a>',
        system_prompt="Prompt",
        pinned_messages=[SimpleNamespace(text="Pinned", color="primary")],
        suggestions_enabled=True,
        suggestions_prompt="Suggestions",
        trigger_templates=["Первый {title}?", "Второй {title}?"],
        public_url="https://example.com/widget.js",
    )
    form = project_forms.WidgetIntegrationEdit(
        data={
            "name": widget.name,
            "agent_name": widget.agent_name,
            "is_enabled": widget.is_enabled,
            "welcome_messages": widget.welcome_messages,
            "waiting_messages": widget.waiting_messages,
            "error_message": widget.error_message,
            "footer_text": widget.footer_text,
            "system_prompt": widget.system_prompt,
            "pinned_messages": [{"text": "Pinned", "color": "primary"}],
            "suggestions_enabled": widget.suggestions_enabled,
            "suggestions_prompt": widget.suggestions_prompt,
            "trigger_templates": widget.trigger_templates,
        },
        meta={"csrf": False},
    )

    rendered = template.render(
        item=widget,
        form=form,
    )

    assert '<option value="neutral"' in rendered
    assert '<option value="primary"' in rendered
    assert '<option value="warning"' in rendered
    assert 'value="primary"' in rendered and "selected" in rendered
    assert 'name="contact_url"' not in rendered
    assert 'Название виджета' in rendered
    assert 'Секрет для подписи данных пользователя' in rendered
    assert 'value="widget-secret"' in rendered
    assert "widget_reset_secret" in rendered
    assert 'Название чата' not in rendered
    assert 'name="is_enabled"' not in rendered
    assert 'Элементы чата' in rendered
    assert 'Заголовок чата' in rendered
    assert 'Системные настройки' in rendered
    chat_order = [
        'Заголовок чата',
        'Приветственные сообщения',
        'Закрепленные сообщения',
        'Стандартные триггеры',
        'Подсказки после ответа',
        'Тексты ожидания',
        'Сообщение при ошибке',
        'Текст подвала',
    ]
    assert [rendered.index(label) for label in chat_order] == sorted(
        rendered.index(label) for label in chat_order
    )
    assert 'Текст подвала' in rendered
    assert 'data-footer-editor' in rendered
    assert 'name="footer_text"' in rendered
    assert 'Приветственные сообщения' in rendered
    assert 'Тексты ожидания' in rendered
    assert 'id="waiting-messages"' in rendered
    assert 'name="waiting_messages-0"' in rendered
    assert 'type="text"\n        name="waiting_messages-0"' in rendered
    assert "<textarea\n        name=\"waiting_messages-0\"" not in rendered
    assert "Проверяю источники" in rendered
    assert 'data-add-waiting-message' in rendered
    assert 'data-remove-waiting-message' in rendered
    assert 'name="error_message"' in rendered
    assert 'name="trigger_templates-0"' in rendered
    assert "Первый {title}?" in rendered
    assert 'data-add-trigger-template' in rendered
    assert 'data-remove-trigger-template' in rendered
    assert 'data-error-message-editor' in rendered
    assert 'data-error-message-html-input' in rendered
    assert 'data-max-length="2000"' in rendered
    assert '<strong>Ошибка</strong>' in rendered
    assert 'id="welcome-messages-container"' in rendered
    assert 'id="welcome-messages"' in rendered
    assert 'table widget-rich-table w-full table-fixed' in rendered
    assert 'table table-zebra widget-rich-table' not in rendered
    assert 'data-welcome-editor' in rendered
    assert 'data-welcome-message' in rendered
    assert 'name="welcome_messages-0"' in rendered
    assert "<strong>Second</strong>" in rendered
    assert 'data-add-welcome-message' in rendered
    assert 'data-remove-welcome-message' in rendered
    assert rendered.count('class="w-10 p-1 text-right align-middle"') >= 2
    assert 'class="min-w-0 p-0 pr-2"' in rendered
    assert 'data-suggestions-toggle' in rendered
    assert 'data-suggestions-prompt-panel' in rendered
    assert "syncSuggestionsPromptVisibility" in rendered
    assert (
        'Формат JSON, последний вопрос, финальный ответ и связанные страницы добавляются автоматически.'
        in rendered
    )
    assert "border-left-color: var(--color-info);" in rendered
    assert "border-left-width: 2px;" in rendered
    assert ".widget-rich-table td:first-child" in rendered
    assert "padding-left: 0;" in rendered
    assert 'Добавить сообщение' in rendered
    assert 'border-t border-base-300/60' not in rendered
    assert 'bg-base-200/40' not in rendered
    assert 'class="sr-only"' not in rendered
    assert 'grid gap-2 rounded border border-base-300 bg-base-100 p-3' not in rendered
    assert 'data-pinned-drag-handle' in rendered
    assert 'data-pinned-color' in rendered
    assert 'data-welcome-drag-handle' not in rendered
    assert 'data-welcome-color' not in rendered
    assert 'name="welcome_message"' not in rendered
    assert 'Footer <a href="https://vbudushee.ru/faq/">link</a>' in rendered
    assert 'data-rich-command="bold"' in rendered
    assert 'data-rich-command="link"' in rendered
    assert "[data-pinned-editor] b," in rendered
    assert "document.addEventListener('copy'" in rendered
    assert "selection.anchorNode" in rendered
    assert "selection.focusNode" in rendered
    assert "event.clipboardData.setData('text/plain', selection.toString())" in rendered
    assert "syncWelcomeRemoveButtons" in rendered
    assert "rows.length <= 1" in rendered
    assert rendered.index(">Сохранить</button>") < rendered.index(">Отмена</a>")
    assert "Состояние виджета" in rendered
    assert "Отключить виджет" in rendered
    assert "widget_disable" in rendered
    assert 'hx-confirm="Вы подтверждаете отключение виджета?"' in rendered
    assert (
        "Отключение заблокирует показ виджета на страницах где он установлен, "
        "но не заблокирует загрузку внешнего JavaScript кода. После отключения "
        "виджета его размер минимизируется, но все равно будет немного замедлять "
        "загрузку страниц сайта"
        in rendered
    )
    assert "Код вставки и удаление" in rendered
    assert (
        "Сброс кода создаст новый адрес виджета. Старый код вставки перестанет "
        "запускать чат, поэтому код на сайтах нужно будет заменить на новый."
        in rendered
    )
    assert (
        "Удаление полностью уберет виджет из админки. На страницах, где остался "
        "старый код вставки, будет загружаться только небольшой JavaScript с "
        "сообщением о том, что виджет удален."
        in rendered
    )
    assert rendered.index("Отключить виджет") < rendered.index("Сбросить код виджета")
    assert rendered.index("Код вставки и удаление") < rendered.index(
        "Сбросить код виджета"
    )


def test_widget_edit_template_shows_enable_action_when_disabled() -> None:
    templates_dir = Path(__file__).resolve().parents[1] / "vchat" / "templates"
    env = jinja2.Environment(
        loader=jinja2.ChoiceLoader(
            [
                jinja2.DictLoader(
                    {"admin.html": "{% block content %}{% endblock %}"}
                ),
                jinja2.FileSystemLoader(str(templates_dir)),
            ]
        ),
        autoescape=True,
    )
    env.globals.update(
        url=lambda name, **kwargs: URL(
            f"/actions/{kwargs['action']}/{kwargs['item_id']}"
            if name == "actions"
            else f"/integration/{kwargs['widget_id']}"
            if name == "project_widget_edit"
            else "/integration"
        ),
        csrf_token=lambda: "token",
    )
    template = env.get_template("projects/widget_edit.html")
    widget = SimpleNamespace(
        id=1,
        name="Widget",
        code="abc",
        secret="widget-secret",
        agent_name="Agent",
        is_enabled=False,
        welcome_messages=["Hello"],
        waiting_messages=["Готовлю ответ"],
        error_message="Ошибка",
        footer_text="Footer",
        system_prompt="Prompt",
        pinned_messages=[],
        suggestions_enabled=True,
        suggestions_prompt="Suggestions",
        trigger_templates=["Default {title}?"],
        public_url="https://example.com/widget.js",
    )
    form = project_forms.WidgetIntegrationEdit(
        data={
            "name": widget.name,
            "agent_name": widget.agent_name,
            "welcome_messages": widget.welcome_messages,
            "waiting_messages": widget.waiting_messages,
            "error_message": widget.error_message,
            "footer_text": widget.footer_text,
            "system_prompt": widget.system_prompt,
            "suggestions_enabled": widget.suggestions_enabled,
            "suggestions_prompt": widget.suggestions_prompt,
            "trigger_templates": widget.trigger_templates,
        },
        meta={"csrf": False},
    )

    rendered = template.render(item=widget, form=form)

    assert "Состояние виджета: отключен" in rendered
    assert "Включить виджет" in rendered
    assert "widget_enable" in rendered
    assert "Отключить виджет" not in rendered


def test_widget_integration_add_form_uses_initial_welcome_message() -> None:
    templates_dir = Path(__file__).resolve().parents[1] / "vchat" / "templates"
    env = jinja2.Environment(
        loader=jinja2.ChoiceLoader(
            [
                jinja2.DictLoader({"admin.html": "{% block content %}{% endblock %}"}),
                jinja2.FileSystemLoader(str(templates_dir)),
            ]
        ),
        autoescape=True,
    )
    env.globals.update(
        url=lambda name, **kwargs: URL(
            f"/actions/{kwargs['action']}/{kwargs['item_id']}"
            if name == "actions"
            else f"/integration/{kwargs['widget_id']}"
            if name == "project_widget_edit"
            else "/integration"
        ),
        csrf_token=lambda: "token",
    )

    rendered = env.get_template("projects/integration.html").render(
        widgets=[],
        form=project_forms.WidgetIntegrationAdd(
            meta={"csrf": False}
        ),
    )

    assert 'name="name"' in rendered
    assert 'name="agent_name"' in rendered
    assert 'value="Чат поддержки"' in rendered
    assert 'name="welcome_messages-0"' not in rendered
    assert 'name="waiting_messages-0"' not in rendered
    assert 'name="footer_text"' not in rendered
    assert 'name="system_prompt"' not in rendered


def test_widget_integration_table_shows_enabled_state() -> None:
    templates_dir = Path(__file__).resolve().parents[1] / "vchat" / "templates"
    env = jinja2.Environment(
        loader=jinja2.ChoiceLoader(
            [
                jinja2.DictLoader({"admin.html": "{% block content %}{% endblock %}"}),
                jinja2.FileSystemLoader(str(templates_dir)),
            ]
        ),
        autoescape=True,
    )
    env.globals.update(
        url=lambda name, **kwargs: URL(
            f"/integration/{kwargs['widget_id']}"
            if name == "project_widget_edit"
            else "/integration"
        ),
        csrf_token=lambda: "token",
    )

    rendered = env.get_template("projects/integration.html").render(
        widgets=[
            SimpleNamespace(
                id=1,
                name="Active",
                agent_name="Agent",
                is_enabled=True,
            ),
            SimpleNamespace(
                id=2,
                name="Paused",
                agent_name="Agent",
                is_enabled=False,
            ),
        ],
        form=project_forms.WidgetIntegrationAdd(meta={"csrf": False}),
    )

    assert "Состояние" in rendered
    assert "Включен" in rendered
    assert "Отключен" in rendered
    assert 'colspan="3"' in env.get_template("projects/integration.html").render(
        widgets=[],
        form=project_forms.WidgetIntegrationAdd(meta={"csrf": False}),
    )


def test_public_chat_template_exposes_widget_accessibility_contracts() -> None:
    templates_dir = Path(__file__).resolve().parents[1] / "vchat" / "templates"
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(templates_dir)),
        autoescape=True,
    )
    env.globals.update(csrf_token=lambda: "token")

    rendered = env.get_template("chat/chat.html").render(
        project=SimpleNamespace(title="Demo"),
        widget=SimpleNamespace(
            agent_name="Demo chat",
            pinned_messages=[],
            welcome_messages=["Hello"],
            waiting_messages=["Готовлю ответ", "Проверяю источники"],
            error_message="<strong>Не удалось получить ответ.</strong>",
            footer_text='<a href="https://vbudushee.ru/faq/">Пользовательское соглашение</a>.<br>Отправить Enter, новая строка Shift+Enter',
        ),
        support_csrf_token="token",
        initial_messages=[],
        signed_chat_id=None,
        payload="signed-payload",
    )

    assert 'id="log"' in rendered
    assert 'role="log"' in rendered
    assert 'aria-label="История сообщений"' in rendered
    assert 'aria-live="polite"' in rendered
    assert 'aria-relevant="additions text"' in rendered
    assert 'aria-label="Форма отправки сообщения"' in rendered
    assert 'id="prompt-label"' in rendered
    assert 'aria-labelledby="prompt-label"' in rendered
    assert 'aria-describedby="composer-footer"' in rendered
    assert 'id="composer-footer"' in rendered
    assert 'id="composer-limit"' not in rendered
    assert "userMessageInputMaxChars = 4050" in rendered
    assert "vchat-prompt-highlight-overflow" in rendered
    assert "Превышен лимит" not in rendered
    assert 'id="status"' not in rendered
    assert 'aria-atomic="true"' not in rendered
    assert "Контакты" not in rendered
    assert 'https://vbudushee.ru/faq/' in rendered
    assert 'Пользовательское соглашение' in rendered
    assert "const waitingMessages = (" in rendered
    assert "\\u0413\\u043e\\u0442\\u043e\\u0432\\u043b\\u044e" in rendered
    assert "\\u041f\\u0440\\u043e\\u0432\\u0435\\u0440\\u044f\\u044e" in rendered
    assert "setInterval(() =>" in rendered
    assert "}, 5000);" in rendered
    assert "vchat-waiting-text" in rendered
    assert "const defaultErrorMessage =" in rendered
    assert "\\u003cstrong\\u003e\\u041d\\u0435 \\u0443\\u0434\\u0430\\u043b\\u043e\\u0441\\u044c" in rendered
    assert "function appendErrorMsg(message, requestId = null)" in rendered
    assert "function processServerPayload(data)" in rendered
    assert "demoSystemMessagesEnabled" in rendered
    assert "vchat_demo_server_payload" in rendered
    assert "Request ID:" in rendered
    assert "data.detail" not in rendered
    assert "data.status" not in rendered
    assert "function renderBotMarkdown(bubble, rawText)" in rendered
    assert "body.classList.remove('vchat-waiting-text');" in rendered
    assert "body.removeAttribute('role');" in rendered
    assert "body.removeAttribute('aria-live');" in rendered
    assert "body.removeAttribute('data-waiting-text');" in rendered
    assert "loading-dots" not in rendered
    assert 'Отправить Enter, новая строка Shift+Enter' in rendered
    assert "setAttribute('role', 'article')" in rendered
    assert 'Сообщение ассистента' in rendered
    assert 'Ваше сообщение' in rendered
    assert "setAttribute('role', 'group')" in rendered
    assert 'Предложенные действия' in rendered
    assert "setAttribute('role', 'list')" in rendered
    assert "Связанные страницы" in rendered
    assert "Связанные страницы ответа" in rendered
    assert "vchat-source-origin" in rendered
    assert "source.source_title" in rendered
    assert "vchat-source-title-link" in rendered
    assert "vchat-source-section" in rendered
    assert "source.summary" in rendered
    assert "vchat-source-summary" in rendered
    assert '>{{ "Источники" }}</span>' not in rendered
    assert "Источники ответа" not in rendered
    assert 'aria-hidden="true"' in rendered
    assert 'Ответ полезен' in rendered
    assert 'Ответ не полезен' in rendered
    assert 'aria-label="Upvote"' not in rendered
    assert 'aria-label="Downvote"' not in rendered
    assert "bubble.textContent = text;" in rendered
    assert "bubble.textContent = String(message || defaultErrorMessage || '');" in rendered
    assert "bubble.innerHTML = String(message || defaultErrorMessage || '');" not in rendered
    assert "window.vchatRenderAssistantMarkdown(rawText)" in rendered
    assert "window.vchatRenderAssistantMarkdown(text)" in rendered
    assert "marked.parse" not in rendered
    assert "escapeHtml(source.uri)" in rendered


def test_widget_loader_template_exposes_dialog_and_iframe_accessibility() -> None:
    templates_dir = Path(__file__).resolve().parents[1] / "vchat" / "templates"
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(templates_dir)),
        autoescape=True,
    )

    rendered = env.get_template("js/widget.js").render(
        widget_code="widget-code",
        widget_chat_path="/chat/widget/widget-code",
        trigger_resolve_path="/widget/widget-code/triggers",
    )

    assert 'button.type = "button";' in rendered
    assert 'button.setAttribute("aria-label", "Открыть чат с ассистентом");' in rendered
    assert 'button.setAttribute("aria-expanded", "false");' in rendered
    assert 'button.setAttribute("aria-controls", "vchat-widget-iframe-container");' in rendered
    assert 'triggerButton.setAttribute("aria-label", "Открыть предложенный вопрос в чате");' in rendered
    assert 'iframeContainer.setAttribute("role", "dialog");' in rendered
    assert 'iframeContainer.setAttribute("aria-label", "Чат с ассистентом");' in rendered
    assert 'iframeContainer.setAttribute("aria-hidden", "true");' in rendered
    assert 'iframeEl.title = "Чат с ассистентом";' in rendered
    assert 'button.setAttribute("aria-expanded", "true");' in rendered
    assert 'iframeContainer.setAttribute("aria-hidden", "false");' in rendered
    assert 'event.key === "Escape"' in rendered
    assert 'data-demo-system-messages") === "true"' in rendered
    assert 'chatUrl.searchParams.append("demo_system_messages", "1");' in rendered
    assert 'container.getAttribute("data-user-info")' in rendered
    assert 'chatUrl.searchParams.append("user_info", userInfo);' in rendered
    assert 'chatUrl.searchParams.append("guest_uid", userUid);' in rendered
    assert 'chatUrl.searchParams.append("user_uid"' not in rendered
    assert 'chatUrl.searchParams.append("user_name"' not in rendered
    assert 'chatUrl.searchParams.append("user_email"' not in rendered


def test_signed_widget_user_info_accepts_valid_payload() -> None:
    secret = "widget-secret"
    raw = _signed_widget_user_info(secret)

    assert project_views._load_signed_widget_user_info(
        raw,
        SimpleNamespace(secret=secret),
    ) == {
        "user_uid": "user-123",
        "user_name": "Иван Иванов",
        "user_email": "ivan@example.com",
    }


def test_signed_widget_user_info_rejects_modified_payload() -> None:
    secret = "widget-secret"
    payload = json.loads(_signed_widget_user_info(secret))
    payload["user_name"] = "Mallory"

    with pytest.raises(ValueError, match="signature is invalid"):
        project_views._load_signed_widget_user_info(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            SimpleNamespace(secret=secret),
        )


def test_demo_page_exposes_system_message_controls() -> None:
    templates_dir = Path(__file__).resolve().parents[1] / "vchat" / "templates"
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(templates_dir)),
        autoescape=True,
    )

    rendered = env.get_template("demo.html").render(
        widgets=[{"id": 1, "name": "Demo widget", "code": "widget-code"}],
        trigger_pages=[
            {
                "id": 20,
                "title": "Docs page",
                "uri": "https://example.com/docs/page",
            }
        ],
        selected_widget_code="widget-code",
        selected_trigger_url="https://example.com/docs/page",
        selected_trigger_url_is_listed=True,
    )

    assert "Системные сообщения" in rendered
    assert "data-system-message-actions" in rendered
    assert 'data-demo-system-messages="true"' in rendered
    assert "Ошибка ответа провайдера" in rendered
    assert "provider_response_error" in rendered
    assert "demo-provider-response" not in rendered
    assert "generateDemoRequestId()" in rendered
    assert "Ошибка соединения" not in rendered
    assert "Внутренняя ошибка" not in rendered
    assert "Сообщение слишком длинное" not in rendered
    assert 'type: "vchat_demo_server_payload"' in rendered
    assert "openWidgetIframe()" in rendered
    assert "waitForChatListener()" in rendered
    assert 'document.getElementById("vchat-widget-iframe")' in rendered


def test_demo_page_system_messages_do_not_require_trigger_page() -> None:
    templates_dir = Path(__file__).resolve().parents[1] / "vchat" / "templates"
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(templates_dir)),
        autoescape=True,
    )

    rendered = env.get_template("demo.html").render(
        widgets=[{"id": 1, "name": "Demo widget", "code": "widget-code"}],
        trigger_pages=[],
        selected_widget_code="widget-code",
        selected_trigger_url="",
        selected_trigger_url_is_listed=False,
    )

    assert "Системные сообщения" in rendered
    assert "const demoSystemPayloads = [" in rendered
    assert "Ошибка ответа провайдера" in rendered
    assert "provider_response_error" in rendered
    assert "window.crypto.getRandomValues(bytes);" in rendered
    assert "byte.toString(16).padStart(2, \"0\")" in rendered
    assert "payload: { ...item.payload, request_id: generateDemoRequestId() }" in rendered
    assert "demo-provider-response" not in rendered
    assert "provider_connection_error" not in rendered
    assert "internal_error" not in rendered
    assert "message_too_long" not in rendered
    assert "renderSystemMessages(demoSystemPayloads);" in rendered
    assert "Выберите страницу с триггерами" not in rendered


def test_frontend_chat_citation_buttons_are_accessible() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "frontend_chat"
        / "src"
        / "safe-markdown.js"
    ).read_text()

    assert 'type="button"' in source
    assert 'aria-label="Открыть источник ${citationNumber}"' in source
    assert "const citationNumber = Number.parseInt(id, 10) + 1;" in source


def test_history_detail_template_escapes_stored_chat_html() -> None:
    templates_dir = Path(__file__).resolve().parents[1] / "vchat" / "templates"
    env = jinja2.Environment(
        loader=jinja2.ChoiceLoader(
            [
                jinja2.DictLoader(
                    {"admin.html": "{% block content %}{% endblock %}"}
                ),
                jinja2.FileSystemLoader(str(templates_dir)),
            ]
        ),
        autoescape=True,
    )
    env.globals.update(
        url=lambda name, **kwargs: URL(
            f"/history/{kwargs['chat_id']}"
            if name == "project_history_detail"
            else "/history"
        ),
    )
    now = datetime(2026, 5, 31, 15, 30, 43, tzinfo=timezone.utc)
    rendered = env.get_template("projects/history_detail.html").render(
        chat=SimpleNamespace(id="chat-1", title="Demo", user_uid="u", created_at=now),
        chat_meta={},
        messages=[
            SimpleNamespace(
                role="assistant",
                created_at=now,
                has_masked_pii=False,
                text_display="<script>alert('I am evil')</script>",
                text="<script>alert('I am evil')</script>",
                guardrail_hit=False,
                context_sources=[],
                vote=None,
            )
        ],
    )

    assert "<script>alert" not in rendered
    assert "&lt;script&gt;alert(&#39;I am evil&#39;)&lt;/script&gt;" in rendered


def test_document_pipeline_steps_returns_error_description() -> None:
    from vchat.views.projects.page_status import PageStatus, PageStatusError

    document = SimpleNamespace(
        status=PageStatus.crawler,
        status_error=PageStatusError.extraction_failed,
        meta={
            "reason": "extraction_failed",
            "error": "boom",
        },
    )

    status, status_error, msg = project_views._document_pipeline_steps(document)

    assert status == PageStatus.crawler
    assert status_error == PageStatusError.extraction_failed
    assert msg is not None
    assert "Ошибка извлечения" in msg
    assert "boom" in msg


def test_document_pipeline_steps_returns_embedder_error_description() -> None:
    from vchat.views.projects.page_status import PageStatus, PageStatusError

    document = SimpleNamespace(
        status=PageStatus.parsing,
        status_error=PageStatusError.embedder_failed,
        meta={
            "reason": "embedder_failed",
            "message": "Chunk 3 is too large for embedder",
        },
    )

    status, status_error, msg = project_views._document_pipeline_steps(document)

    assert status == PageStatus.parsing
    assert status_error == PageStatusError.embedder_failed
    assert msg is not None
    assert "Ошибка эмбеддера" in msg
    assert "Chunk 3 is too large for embedder" in msg


def test_document_uniqueness_percent_uses_boilerplate_overlap() -> None:
    content = (
        "общий текст меню подвала повторяется\n"
        "общий текст меню подвала повторяется\n"
        "уникальный раздел страницы со смыслом\n"
    )
    boilerplate = project_views.compute_trigram_hashes(
        "общий текст меню подвала повторяется"
    )

    uniqueness = project_views._document_uniqueness_percent(content, boilerplate)

    assert uniqueness is not None
    assert 0 < uniqueness < 100


def test_document_stats_summary_includes_requested_metrics() -> None:
    document = SimpleNamespace(content="слово " * 200, _length=0)
    extraction = {"word_count": 200, "table_count": 3}
    chunks = [SimpleNamespace(), SimpleNamespace()]

    summary = project_views._document_stats_summary(document, chunks, extraction, 87)

    assert "чанков" in summary
    assert "слов" in summary
    assert "таблиц" in summary
    assert "87% уникальности текста" in summary


@pytest.mark.asyncio
async def test_document_link_groups_split_mutual_incoming_and_outgoing() -> None:
    document = SimpleNamespace(id=10, uri="https://example.local/current")

    outgoing_rows = [
        (
            SimpleNamespace(
                target_page_id=21,
                target_uri="https://example.local/mutual",
            ),
            SimpleNamespace(
                id=21,
                title="Mutual page",
                uri="https://example.local/mutual",
                last_crawled_at=object(),
                status_error=None,
            ),
        ),
        (
            SimpleNamespace(
                target_page_id=22,
                target_uri="https://example.local/outgoing",
            ),
            SimpleNamespace(
                id=22,
                title="Outgoing page",
                uri="https://example.local/outgoing",
                last_crawled_at=None,
                status_error=None,
            ),
        ),
    ]
    incoming_rows = [
        (
            SimpleNamespace(source_page_id=21),
            SimpleNamespace(
                id=21,
                title="Mutual page",
                uri="https://example.local/mutual",
                last_crawled_at=object(),
                status_error=None,
            ),
        ),
        (
            SimpleNamespace(source_page_id=23),
            SimpleNamespace(
                id=23, title="Incoming page", uri="https://example.local/incoming"
            ),
        ),
    ]

    class _Res:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _Db:
        def __init__(self):
            self.calls = 0

        async def execute(self, stmt):
            _ = stmt
            self.calls += 1
            if self.calls == 1:
                return _Res(outgoing_rows)
            return _Res(incoming_rows)

    groups = await project_views._document_link_groups(_Db(), document)

    assert [item["id"] for item in groups["mutual"]] == [21]
    assert [item["id"] for item in groups["incoming"]] == [23]
    assert [item["id"] for item in groups["outgoing"]] == [22]
    assert groups["mutual"][0]["status"] == "ok"
    assert groups["outgoing"][0]["status"] == "not_indexed"


def test_document_links_graph_builds_nodes_and_bidirectional_edges() -> None:
    document = SimpleNamespace(
        id=10,
        uri="https://example.local/current",
        status="ready",
        status_error=None,
    )
    groups = {
        "mutual": [
            {
                "id": 21,
                "title": "Mutual",
                "uri": "https://other.local/mutual",
                "status": "ok",
                "status_error": None,
            }
        ],
        "incoming": [
            {
                "id": 22,
                "title": "Incoming",
                "uri": "https://example.local/incoming",
                "status": "blocked",
                "status_error": "excluded_rules",
            }
        ],
        "outgoing": [
            {
                "id": 23,
                "title": "Outgoing",
                "uri": "https://example.local/outgoing",
                "status": "not_indexed",
                "status_error": None,
            }
        ],
    }

    graph = project_views._document_links_graph(document, "Current", groups)

    assert graph["currentNodeId"] == "page-10"
    assert {node["id"] for node in graph["nodes"]} == {
        "page-10",
        "page-21",
        "page-22",
        "page-23",
    }
    node_by_id = {node["id"]: node for node in graph["nodes"]}
    assert node_by_id["page-21"]["is_external"] is True
    assert node_by_id["page-22"]["is_ignored"] is True
    assert node_by_id["page-23"]["is_external"] is False
    assert ("page-21", "page-10", "incoming") in {
        (link["source"], link["target"], link["relation"]) for link in graph["links"]
    }
    assert ("page-10", "page-21", "outgoing") in {
        (link["source"], link["target"], link["relation"]) for link in graph["links"]
    }
