from datetime import timedelta

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

from vchat.ai_providers import (
    get_default_model_id,
    get_default_provider_id,
    get_model_choices,
    get_provider_choices,
)
from vchat.i18n import _
from vchat.source_settings import (
    DEFAULT_CRAWLER_CONCURRENT_REQUESTS,
    DEFAULT_CRAWLER_DOWNLOAD_DELAY,
    DEFAULT_CRAWLER_DOWNLOAD_TIMEOUT,
    is_manual_reindex,
    normalize_reindex_cron,
    validate_reindex_cron,
)
from vchat.settings import config

DEFAULT_SYSTEM_PROMPT = _(
    "Ты дружелюбный ИИ-ассистент, который помогает людям достигать их целей. "
    "Отвечай кратко, проактивно предлагай следующие шаги и задавай уточняющие "
    "вопросы, когда не хватает информации."
)


class WorkspaceForm(Form):
    class Meta:
        csrf = True  # Enable CSRF
        csrf_secret = config["secret_key"]
        csrf_class = SessionCSRF
        csrf_time_limit = timedelta(minutes=20)

    title = StringField(
        _("Workspace Title"),
        validators=[
            validators.DataRequired(),
            validators.Length(max=100, message=_("Length up to 100 characters")),
        ],
        render_kw={"class": "form-control"},
        description=_("Workspace title. Recommended size is 70 characters"),
    )

    system_prompt = TextAreaField(
        _("System Prompt"),
        validators=[
            validators.Optional(),
            validators.Length(max=2000, message=_("Length up to 2000 characters")),
        ],
        render_kw={"class": "textarea textarea-bordered w-full", "rows": "5"},
        default=DEFAULT_SYSTEM_PROMPT,
        description=_("Custom system prompt for the AI agent"),
    )

    agent_style = StringField(
        _("Agent Style"),
        validators=[
            validators.Optional(),
            validators.Length(max=100, message=_("Length up to 100 characters")),
        ],
        render_kw={"class": "input input-bordered w-full"},
        description=_("Communication style for the agent"),
    )

    provider = SelectField(
        _("AI Provider"),
        validators=[validators.DataRequired()],
        choices=[],
        render_kw={"class": "select select-bordered w-full"},
        description=_("Provider used for chat responses"),
    )

    model = SelectField(
        _("Model"),
        validators=[validators.DataRequired()],
        choices=[],
        render_kw={"class": "select select-bordered w-full"},
        description=_("Model that will be used for this project's chats"),
    )

    agent_name = StringField(
        _("Agent Name"),
        validators=[
            validators.Optional(),
            validators.Length(max=100, message=_("Length up to 100 characters")),
        ],
        render_kw={"class": "input input-bordered w-full"},
        description=_("Name shown to users in the chat widget"),
    )

    welcome_message = TextAreaField(
        _("Welcome Message"),
        validators=[
            validators.Optional(),
            validators.Length(max=2000, message=_("Length up to 2000 characters")),
        ],
        render_kw={"class": "textarea textarea-bordered w-full", "rows": "3"},
        description=_("Message shown to users when the chat opens"),
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
        _("Стандартные триггеры"),
        validators=[
            validators.Optional(),
            validators.Length(max=4000, message=_("Length up to 4000 characters")),
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
        _("URL"),
        validators=[
            validators.DataRequired(),
            validators.URL(message=_("Invalid URL")),
        ],
        render_kw={"class": "form-control"},
    )
    title = StringField(
        _("Title"),
        validators=[
            validators.Length(max=255, message=_("Length up to 255 characters")),
        ],
        render_kw={"class": "form-control"},
        description=_("Source title. If empty, domain will be used."),
    )
    reindex_cron = StringField(
        _("Reindexing Cron"),
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
                _(
                    "Invalid cron expression. Use 5 fields: minute hour day month weekday"
                )
            )


class InviteUserForm(Form):
    class Meta:
        csrf = True
        csrf_secret = config["secret_key"]
        csrf_class = SessionCSRF
        csrf_time_limit = timedelta(minutes=20)

    email = StringField(
        _("Email"),
        validators=[
            validators.DataRequired(),
            validators.Email(message=_("Invalid Email")),
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
        _("Reindexing Cron"),
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
                _(
                    "Invalid cron expression. Use 5 fields: minute hour day month weekday"
                )
            )

    concurrent_requests = IntegerField(
        _("Параллельные запросы (CONCURRENT_REQUESTS)"),
        validators=[validators.Optional(), validators.NumberRange(min=1, max=256)],
        default=DEFAULT_CRAWLER_CONCURRENT_REQUESTS,
        render_kw={"class": "input input-bordered w-full"},
    )
    download_delay = IntegerField(
        _("Задержка между запросами (DOWNLOAD_DELAY)"),
        validators=[validators.Optional(), validators.NumberRange(min=0, max=120)],
        default=DEFAULT_CRAWLER_DOWNLOAD_DELAY,
        render_kw={"class": "input input-bordered w-full"},
    )
    download_timeout = IntegerField(
        _("Таймаут запроса (DOWNLOAD_TIMEOUT, сек)"),
        validators=[validators.Optional(), validators.NumberRange(min=1, max=300)],
        default=DEFAULT_CRAWLER_DOWNLOAD_TIMEOUT,
        render_kw={"class": "input input-bordered w-full"},
    )
    ignore_robots_txt = BooleanField(
        _("Игнорировать robots.txt"),
        default=False,
        render_kw={"class": "checkbox checkbox-primary"},
    )


class SourceSettingsForm(SourceForm):
    concurrent_requests = IntegerField(
        _("Параллельные запросы (CONCURRENT_REQUESTS)"),
        validators=[validators.Optional(), validators.NumberRange(min=1, max=256)],
        default=DEFAULT_CRAWLER_CONCURRENT_REQUESTS,
        render_kw={"class": "input input-bordered w-full"},
    )
    download_delay = IntegerField(
        _("Задержка между запросами (DOWNLOAD_DELAY)"),
        validators=[validators.Optional(), validators.NumberRange(min=0, max=120)],
        default=DEFAULT_CRAWLER_DOWNLOAD_DELAY,
        render_kw={"class": "input input-bordered w-full"},
    )
    download_timeout = IntegerField(
        _("Таймаут запроса (DOWNLOAD_TIMEOUT, сек)"),
        validators=[validators.Optional(), validators.NumberRange(min=1, max=300)],
        default=DEFAULT_CRAWLER_DOWNLOAD_TIMEOUT,
        render_kw={"class": "input input-bordered w-full"},
    )
    ignore_robots_txt = BooleanField(
        _("Игнорировать robots.txt"),
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
        _("Workspace Title"),
        validators=[
            validators.DataRequired(),
            validators.Length(max=100, message=_("Length up to 100 characters")),
        ],
        render_kw={"class": "form-control"},
    )

    system_prompt = TextAreaField(
        _("System Prompt"),
        validators=[
            validators.Optional(),
            validators.Length(max=2000, message=_("Length up to 2000 characters")),
        ],
        render_kw={"class": "textarea textarea-bordered w-full", "rows": "5"},
        default=DEFAULT_SYSTEM_PROMPT,
    )

    source_url = StringField(
        _("Source URL"),
        validators=[
            validators.DataRequired(),
            validators.URL(message=_("Invalid URL")),
        ],
        render_kw={"class": "form-control"},
    )

    source_title = StringField(
        _("Source Title"),
        validators=[
            validators.Length(
                max=255,
                message=_("Length up to 255 characters"),
            ),
        ],
        render_kw={"class": "form-control"},
    )
