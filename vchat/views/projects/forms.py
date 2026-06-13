from datetime import timedelta
from urllib.parse import urlsplit, urlunsplit

from wtforms import (
    BooleanField,
    Form,
    IntegerField,
    SelectField,
    StringField,
    TextAreaField,
    validators,
)
from wtforms.csrf.session import SessionCSRF

from vchat.views.chat.ai import (
    get_default_model_id,
    get_default_provider_id,
    get_model_choices,
    get_provider_choices,
)
from jobs.crawler.source_settings import (
    DEFAULT_CRAWLER_CONCURRENT_REQUESTS,
    DEFAULT_CRAWLER_DOWNLOAD_DELAY,
    DEFAULT_CRAWLER_DOWNLOAD_TIMEOUT,
    is_manual_reindex,
    normalize_reindex_cron,
    validate_reindex_cron,
)
from vchat.settings import config

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

Сгенерируй 2-3 коротких следующих вопроса или действия от лица пользователя.
Подсказки должны быть напрямую связаны с последним вопросом, финальным ответом ассистента и использованными источниками.
Не повторяй уже отвеченный вопрос. Не придумывай факты, которых нет в ответе или источниках.
Пиши на языке последнего вопроса пользователя.
"""

DEFAULT_WIDGET_FOOTER_TEXT = "Отправить Enter, новая строка Shift+Enter"


def normalize_source_origin(value: str) -> str:
    split = urlsplit((value or "").strip())
    return urlunsplit((split.scheme.lower(), split.netloc.lower(), "", "", ""))


class WorkspaceForm(Form):
    class Meta:
        csrf = True  # Enable CSRF
        csrf_secret = config["secret_key"]
        csrf_class = SessionCSRF
        csrf_time_limit = timedelta(minutes=20)

    title = StringField(
        "Название пространства",
        validators=[
            validators.DataRequired(),
            validators.Length(max=100, message="Длина до 100 символов"),
        ],
        render_kw={"class": "form-control"},
        description="Название пространства. Рекомендуемый размер — до 70 символов",
    )

    system_prompt = TextAreaField(
        "Системный промпт",
        validators=[
            validators.Optional(),
            validators.Length(max=2000, message="Длина до 2000 символов"),
        ],
        render_kw={"class": "textarea textarea-bordered w-full", "rows": "5"},
        default=DEFAULT_SYSTEM_PROMPT,
        description="Пользовательский системный промпт ИИ-агента",
    )

    agent_style = StringField(
        "Стиль агента",
        validators=[
            validators.Optional(),
            validators.Length(max=100, message="Длина до 100 символов"),
        ],
        render_kw={"class": "input input-bordered w-full"},
        description="Стиль общения агента",
    )

    provider = SelectField(
        "AI-провайдер",
        validators=[validators.DataRequired()],
        choices=[],
        render_kw={"class": "select select-bordered w-full"},
        description="Провайдер для ответов в чате",
    )

    model = SelectField(
        "Модель",
        validators=[validators.DataRequired()],
        choices=[],
        render_kw={"class": "select select-bordered w-full"},
        description="Модель для чатов проекта",
    )

    agent_name = StringField(
        "Имя агента",
        validators=[
            validators.Optional(),
            validators.Length(max=100, message="Длина до 100 символов"),
        ],
        render_kw={"class": "input input-bordered w-full"},
        description="Имя, которое пользователи видят в чат-виджете",
    )

    welcome_message = TextAreaField(
        "Приветственное сообщение",
        validators=[
            validators.Optional(),
            validators.Length(max=2000, message="Длина до 2000 символов"),
        ],
        render_kw={"class": "textarea textarea-bordered w-full", "rows": "3"},
        description="Сообщение при открытии чата",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        provider_choices = get_provider_choices()
        if not provider_choices:
            provider_choices = [(get_default_provider_id(), "OpenAI")]
        self.provider.choices = provider_choices
        if not self.provider.data:
            self.provider.data = get_default_provider_id()

        model_choices = get_model_choices(self.provider.data)
        if not model_choices:
            default_model = get_default_model_id(self.provider.data)
            model_choices = [(default_model, default_model)]
        self.model.choices = model_choices
        valid_models = {choice[0] for choice in model_choices}
        if not self.model.data or self.model.data not in valid_models:
            self.model.data = get_default_model_id(self.provider.data)


class TriggerSettingsForm(Form):
    class Meta:
        csrf = True
        csrf_secret = config["secret_key"]
        csrf_class = SessionCSRF
        csrf_time_limit = timedelta(minutes=20)

    default_templates = TextAreaField(
        "Стандартные триггеры",
        validators=[
            validators.Optional(),
            validators.Length(max=4000, message="Длина до 4000 символов"),
        ],
        render_kw={"class": "textarea textarea-bordered w-full", "rows": "8"},
    )


class SourceForm(Form):
    class Meta:
        csrf = True
        csrf_secret = config["secret_key"]
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
        field.data = normalize_source_origin(field.data)

    enable_triggers = BooleanField(
        "Разрешить пользовательские триггеры",
        default=False,
        render_kw={"class": "checkbox checkbox-primary"},
    )


class InviteUserForm(Form):
    class Meta:
        csrf = True
        csrf_secret = config["secret_key"]
        csrf_class = SessionCSRF
        csrf_time_limit = timedelta(minutes=20)

    email = StringField(
        "Email",
        validators=[
            validators.DataRequired(),
            validators.Email(message="Некорректный email"),
        ],
        render_kw={"class": "form-control"},
    )


class SourceCrawlerSettingsForm(Form):
    class Meta:
        csrf = True
        csrf_secret = config["secret_key"]
        csrf_class = SessionCSRF
        csrf_time_limit = timedelta(minutes=20)

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


class SourceSettingsForm(SourceForm):
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


class OnboardingForm(Form):
    class Meta:
        csrf = True
        csrf_secret = config["secret_key"]
        csrf_class = SessionCSRF
        csrf_time_limit = timedelta(minutes=20)

    project_title = StringField(
        "Название пространства",
        validators=[
            validators.DataRequired(),
            validators.Length(max=100, message="Длина до 100 символов"),
        ],
        render_kw={"class": "form-control"},
    )

    system_prompt = TextAreaField(
        "Системный промпт",
        validators=[
            validators.Optional(),
            validators.Length(max=2000, message="Длина до 2000 символов"),
        ],
        render_kw={"class": "textarea textarea-bordered w-full", "rows": "5"},
        default=DEFAULT_SYSTEM_PROMPT,
    )

    source_url = StringField(
        "URL источника",
        validators=[
            validators.DataRequired(),
            validators.URL(message="Некорректный URL"),
        ],
        render_kw={"class": "form-control"},
    )

    source_title = StringField(
        "Название источника",
        validators=[
            validators.Length(
                max=255,
                message="Длина до 255 символов",
            ),
        ],
        render_kw={"class": "form-control"},
    )
