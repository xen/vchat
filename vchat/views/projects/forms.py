from datetime import timedelta

from wtforms import (
    Form,
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
from vchat.text import _
from vchat.settings import config

DEFAULT_SYSTEM_PROMPT = _(
    "You are a friendly AI agent that helps people achieve their goals. "
    "Answer concisely, proactively suggest next steps, and ask clarifying "
    "questions whenever information is missing."
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
    type = SelectField(
        _("Type"),
        choices=[
            ("", ""),
            ("site", "Site"),
            ("sitemap", "Sitemap"),
            ("list", "List"),
            ("s3", "S3"),
            ("google_drive", "Google Drive"),
        ],
        validators=[
            validators.DataRequired(),
        ],
        render_kw={"class": "select border w-full"},
    )

    # S3 specific fields
    aws_access_key_id = StringField(
        _("AWS Access Key ID"),
        validators=[validators.Optional()],
        render_kw={"class": "input input-bordered w-full"},
    )
    aws_secret_access_key = StringField(
        _("AWS Secret Access Key"),
        validators=[validators.Optional()],
        render_kw={"class": "input input-bordered w-full", "type": "password"},
    )
    bucket_name = StringField(
        _("Bucket Name"),
        validators=[validators.Optional()],
        render_kw={"class": "input input-bordered w-full"},
    )
    endpoint_url = StringField(
        _("Endpoint URL"),
        validators=[validators.Optional()],
        render_kw={
            "class": "input input-bordered w-full",
            "placeholder": "https://s3.amazonaws.com",
        },
        default="https://s3.amazonaws.com",
        description=_(
            "S3 endpoint URL (leave default for AWS, change for MinIO, DigitalOcean Spaces, etc.)"
        ),
    )
    region = StringField(
        _("Region"),
        validators=[validators.Optional()],
        render_kw={"class": "input input-bordered w-full"},
        default="us-east-1",
    )
    prefix = StringField(
        _("Prefix"),
        validators=[validators.Optional()],
        render_kw={"class": "input input-bordered w-full"},
        description=_("Optional path prefix in bucket"),
    )

    # Google Drive specific fields
    google_drive_folder_id = StringField(
        _("Folder ID"),
        validators=[validators.Optional()],
        render_kw={"class": "input input-bordered w-full", "readonly": True},
    )
    google_drive_folder_name = StringField(
        _("Folder Name"),
        validators=[validators.Optional()],
        render_kw={"class": "input input-bordered w-full", "readonly": True},
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


class TopicsForm(Form):
    class Meta:
        csrf = True
        csrf_secret = config["secret_key"]
        csrf_class = SessionCSRF
        csrf_time_limit = timedelta(minutes=20)

    topics = TextAreaField(
        _("Topics"),
        validators=[validators.Optional()],
        render_kw={
            "class": "textarea textarea-bordered w-full font-mono text-sm",
            "rows": "10",
        },
        description=_("List of project topics (one per line)"),
    )

    intents = TextAreaField(
        _("User Intents"),
        validators=[validators.Optional()],
        render_kw={
            "class": "textarea textarea-bordered w-full font-mono text-sm",
            "rows": "10",
        },
        description=_("List of potential user intents (one per line)"),
    )
