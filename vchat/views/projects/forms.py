from datetime import timedelta
from urllib.parse import urlsplit, urlunsplit

from wtforms import (
    BooleanField,
    FieldList,
    Form,
    FormField,
    IntegerField,
    SelectField,
    StringField,
    TextAreaField,
    validators,
)
from wtforms.csrf.session import SessionCSRF

from jobs.crawler.source_settings import (
    DEFAULT_CRAWLER_CONCURRENT_REQUESTS,
    DEFAULT_CRAWLER_DOWNLOAD_DELAY,
    DEFAULT_CRAWLER_DOWNLOAD_TIMEOUT,
    is_manual_reindex,
    normalize_reindex_cron,
    validate_reindex_cron,
)
from vchat.settings import cfg
from vchat.utils import SafeHTML
from vchat.views.triggers.rules import DEFAULT_TRIGGER_TEMPLATES

DEFAULT_SYSTEM_PROMPT = (
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

DEFAULT_SUGGESTIONS_PROMPT = """Ты генерируешь подсказки для продолжения диалога в чат-виджете.

Сгенерируй ровно 3 коротких следующих вопроса или действия от лица пользователя.
Подсказки должны быть напрямую связаны с последним вопросом, финальным ответом ассистента и использованными источниками.
Не повторяй уже отвеченный вопрос и не предлагай вопрос, на который финальный ответ уже дал ответ.
Каждая подсказка должна вести к новому следующему шагу: уточнить детали, сравнить варианты, открыть источник, посмотреть связанные программы или условия.
Не придумывай факты, которых нет в ответе или источниках.
Пиши на языке последнего вопроса пользователя.
"""

WIDGET_AGENT_NAME = "Чат поддержки"
WIDGET_FOOTER_TEXT = "Отправить Enter, новая строка Shift+Enter"
WIDGET_WELCOME_MESSAGES = ["Добро пожаловать в чат, задавайте вопросы"]
WIDGET_WAITING_MESSAGES = ["Готовлю ответ"]
WIDGET_ERROR_MESSAGE = (
    "Извините, сейчас не удалось получить ответ. Попробуйте отправить сообщение позже."
)
WIDGET_ALLOWED_PINNED_COLORS = {
    "primary",
    "secondary",
    "accent",
    "neutral",
    "info",
    "success",
    "warning",
}


class PinnedMessageForm(Form):
    text = StringField(
        "Сообщение",
        validators=[
            validators.Optional(),
            validators.Length(max=4000),
            SafeHTML(max_text_length=400),
        ],
        default="",
    )
    color = SelectField(
        "Цвет",
        choices=[(color, color) for color in sorted(WIDGET_ALLOWED_PINNED_COLORS)],
        default="neutral",
    )


class WidgetIntegrationAdd(Form):
    class Meta:
        csrf = True
        csrf_secret = cfg.csrf_secret
        csrf_class = SessionCSRF
        csrf_time_limit = timedelta(minutes=20)

    name = StringField(
        "Название",
        validators=[
            validators.DataRequired(message="Название обязательно"),
            validators.Length(max=128, message="Длина до 128 символов"),
        ],
        description="Внутреннее название, нужно только для удобства в админке.",
    )
    agent_name = StringField(
        "Название чата",
        validators=[
            validators.DataRequired(message="Название чата обязательно"),
            validators.Length(max=100),
        ],
        default=WIDGET_AGENT_NAME,
        description="Показывается пользователю в окне чата.",
    )
    suggestions_prompt = TextAreaField(
        "Промпт подсказок",
        default=DEFAULT_SUGGESTIONS_PROMPT,
    )


class WidgetIntegrationEdit(Form):
    class Meta:
        csrf = True
        csrf_secret = cfg.csrf_secret
        csrf_class = SessionCSRF
        csrf_time_limit = timedelta(minutes=20)

    name = StringField(
        "Название",
        validators=[
            validators.DataRequired(message="Название обязательно"),
            validators.Length(max=128, message="Длина до 128 символов"),
        ],
    )
    agent_name = StringField(
        "Название чата",
        validators=[
            validators.DataRequired(message="Название чата обязательно"),
            validators.Length(max=100),
        ],
        default=WIDGET_AGENT_NAME,
    )
    system_prompt = TextAreaField(
        "Системный промпт",
        validators=[
            validators.DataRequired(message="Системный промпт обязателен"),
            validators.Length(max=12000),
        ],
        default=DEFAULT_SYSTEM_PROMPT,
    )
    suggestions_enabled = BooleanField("Показывать подсказки", default=False)
    suggestions_prompt = TextAreaField(
        "Промпт подсказок",
        validators=[
            validators.DataRequired(message="Промпт подсказок обязателен"),
            validators.Length(max=8000),
        ],
        default=DEFAULT_SUGGESTIONS_PROMPT,
    )
    welcome_messages = FieldList(
        StringField(
            "Приветственное сообщение",
            validators=[
                validators.Optional(),
                validators.Length(max=4000),
                SafeHTML(max_text_length=2000),
            ],
            default="",
        ),
        "Приветственные сообщения",
        min_entries=1,
        default=WIDGET_WELCOME_MESSAGES,
    )
    waiting_messages = FieldList(
        StringField(
            "Сообщение ожидания",
            filters=[str.strip],
            validators=[validators.Optional(), validators.Length(max=120)],
            default="",
        ),
        "Сообщения ожидания",
        min_entries=1,
        default=WIDGET_WAITING_MESSAGES,
    )
    trigger_templates = FieldList(
        StringField(
            "Стандартный триггер",
            filters=[str.strip],
            validators=[validators.Optional(), validators.Length(max=256)],
            default="",
        ),
        "Стандартные триггеры",
        min_entries=1,
        default=DEFAULT_TRIGGER_TEMPLATES,
    )
    pinned_messages = FieldList(
        FormField(PinnedMessageForm),
        "Закрепленные сообщения",
        min_entries=0,
        max_entries=3,
    )
    footer_text = StringField(
        "Текст под полем ввода",
        validators=[
            validators.Optional(),
            validators.Length(max=1000),
            SafeHTML(max_text_length=600),
        ],
        default=WIDGET_FOOTER_TEXT,
    )
    error_message = StringField(
        "Сообщение при ошибке",
        validators=[
            validators.DataRequired(message="Сообщение при ошибке обязательно"),
            validators.Length(max=4000),
            SafeHTML(max_text_length=2000),
        ],
        default=WIDGET_ERROR_MESSAGE,
    )

    def validate_welcome_messages(self, field) -> None:
        messages = [entry.data for entry in field.entries if entry.data]
        self.cleaned_welcome_messages = messages or list(WIDGET_WELCOME_MESSAGES)
        if not messages and field.entries:
            field.entries[0].data = self.cleaned_welcome_messages[0]

    def validate_waiting_messages(self, field) -> None:
        messages = [entry.data for entry in field.entries if entry.data]
        self.cleaned_waiting_messages = messages or list(WIDGET_WAITING_MESSAGES)
        if not messages and field.entries:
            field.entries[0].data = self.cleaned_waiting_messages[0]

    def validate_trigger_templates(self, field) -> None:
        templates = []
        seen = set()
        for entry in field.entries:
            value = (entry.data or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            templates.append(value)
        self.cleaned_trigger_templates = templates or list(DEFAULT_TRIGGER_TEMPLATES)
        if not templates and field.entries:
            field.entries[0].data = self.cleaned_trigger_templates[0]

    def validate_pinned_messages(self, field) -> None:
        messages = []
        for entry in field.entries[:3]:
            if not entry.form.text.data:
                continue
            messages.append(
                {
                    "text": entry.form.text.data,
                    "color": entry.form.color.data,
                }
            )
        self.cleaned_pinned_messages = messages


class TriggerEdit(Form):
    class Meta:
        csrf = True
        csrf_secret = cfg.csrf_secret
        csrf_class = SessionCSRF
        csrf_time_limit = timedelta(minutes=20)


class SourceAdd(Form):
    class Meta:
        csrf = True
        csrf_secret = cfg.csrf_secret
        csrf_class = SessionCSRF
        csrf_time_limit = timedelta(minutes=20)

    url = StringField(
        "URL",
        validators=[
            validators.DataRequired(),
            validators.URL(message="Некорректный URL"),
        ],
        render_kw={"class": "form-control"},
    )
    title = StringField(
        "Заголовок",
        validators=[
            validators.Length(max=255, message="Длина до 255 символов"),
        ],
        render_kw={"class": "form-control"},
        description="Название источника. Если оставить пустым, будет использован домен.",
    )
    reindex_cron = StringField(
        "Cron переиндексации",
        validators=[validators.Optional(), validators.Length(max=100)],
        default="",
        render_kw={
            "class": "input input-bordered w-full",
            "placeholder": "0 3 * * 1",
        },
    )

    def validate_reindex_cron(self, field):
        field.data = normalize_reindex_cron(field.data)
        if is_manual_reindex(field.data):
            return
        if not validate_reindex_cron(field.data):
            raise validators.ValidationError(
                "Некорректное cron-выражение. Используйте 5 полей: минута час день месяц день-недели"
            )

    def validate_url(self, field):
        split = urlsplit(field.data.strip())
        field.data = urlunsplit(
            (split.scheme.lower(), split.netloc.lower(), "", "", "")
        )

    enable_triggers = BooleanField(
        "Разрешить пользовательские триггеры",
        default=False,
        render_kw={"class": "checkbox checkbox-primary"},
    )


class SourceSettingsEdit(SourceAdd):
    concurrent_requests = IntegerField(
        "Параллельные запросы (CONCURRENT_REQUESTS)",
        validators=[validators.Optional(), validators.NumberRange(min=1, max=256)],
        default=DEFAULT_CRAWLER_CONCURRENT_REQUESTS,
        render_kw={"class": "input input-bordered w-full"},
    )
    download_delay = IntegerField(
        "Задержка между запросами (DOWNLOAD_DELAY)",
        validators=[validators.Optional(), validators.NumberRange(min=0, max=120)],
        default=DEFAULT_CRAWLER_DOWNLOAD_DELAY,
        render_kw={"class": "input input-bordered w-full"},
    )
    download_timeout = IntegerField(
        "Таймаут запроса (DOWNLOAD_TIMEOUT, сек)",
        validators=[validators.Optional(), validators.NumberRange(min=1, max=300)],
        default=DEFAULT_CRAWLER_DOWNLOAD_TIMEOUT,
        render_kw={"class": "input input-bordered w-full"},
    )
    ignore_robots_txt = BooleanField(
        "Игнорировать robots.txt",
        default=False,
        render_kw={"class": "checkbox checkbox-primary"},
    )
    enable_triggers = BooleanField(
        "Разрешить пользовательские триггеры",
        default=False,
        render_kw={"class": "checkbox checkbox-primary"},
    )
