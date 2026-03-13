import re
from datetime import timedelta

from wtforms import (
    BooleanField,
    FileField,
    Form,
    StringField,
    TextAreaField,
    validators,
)
from wtforms.csrf.session import SessionCSRF

from core.i18n import lazy_gettext as _
from core.settings import config


class PostForm(Form):
    class Meta:
        csrf = True  # Enable CSRF
        csrf_secret = config["secret_key"]
        csrf_class = SessionCSRF
        csrf_time_limit = timedelta(minutes=20)

    title = StringField(
        _("Post Title"),
        validators=[
            validators.DataRequired(),
            validators.Length(max=100, message=_("Length up to 100 characters")),
        ],
        render_kw={"class": "form-control"},
        description=_("Post title. Recommended size is 70 characters"),
    )
    slug = StringField(
        "URL Slug",
        validators=[
            validators.Regexp(
                r"^[a-zA-Z0-9_.-]+$",
                message=_(
                    "Invalid Slug format: only letters, numbers, and -_. are allowed."
                ),
            )
        ],
        render_kw={"class": "form-control"},
    )
    lead = TextAreaField(
        _("Short Description"),
        render_kw={"class": "form-control", "rows": 5},
        validators=[
            validators.DataRequired(),
            validators.Length(
                max=160,
                message=_("Search engine requirements from 25 to 160 characters"),
            ),
        ],
        description=_(
            "Short description of the post. Recommended size up to 160 characters"
        ),
    )
    body = TextAreaField(
        _("Text"),
        validators=[validators.DataRequired()],
        render_kw={"class": "form-control", "rows": 10},
    )
    picture = FileField(
        _("Image"), render_kw={"class": "form-control", "accept": "image/*"}
    )

    category = StringField(
        _("Category"),
        description=_(
            "Select a category or add your own tag by typing it in this line and pressing the <kbd>Enter</kbd> key"
        ),
    )

    show_toc = BooleanField(
        _("Show Table of Contents"),
        render_kw={"class": "form-control"},
        default="checked",
    )


class PostSearchForm(Form):
    class Meta:
        csrf = True  # Enable CSRF
        csrf_secret = config["secret_key"]
        csrf_class = SessionCSRF
        csrf_time_limit = timedelta(minutes=20)

    q = StringField(
        _("Enter the title of the article:"),
        render_kw={
            "placeholder": _("article title"),
            "onchange": "document.getElementById('limit_change').submit()",
            "class": "form-control form-control-sm",
        },
        description=_("Search the list of articles by title"),
    )


url_name = re.compile(
    r"^[a-z](?:[a-z\d]|-(?=[a-z\d])|_(?=[a-z\d])){0,39}$", re.IGNORECASE
)


class CategoryForm(Form):
    class Meta:
        csrf = True  # Enable CSRF
        csrf_secret = config["secret_key"]
        csrf_class = SessionCSRF
        csrf_time_limit = timedelta(minutes=20)

    title = StringField(
        _("Category Title"),
        validators=[validators.DataRequired()],
        render_kw={"class": "form-control"},
    )
    slug = StringField(
        "URL slug",
        validators=[
            validators.DataRequired(),
            validators.Regexp(
                url_name, _("Allowed characters are letters, numbers, and '-' and '_'.")
            ),
        ],
        render_kw={"class": "form-control"},
    )
    description = TextAreaField(
        _("Description"), validators=[], render_kw={"class": "form-control", "rows": 10}
    )
    is_tag = BooleanField(
        _("Tag?"), render_kw={"class": "form-control"}, default="checked"
    )
    is_term = BooleanField(
        _("Dictionary Article?"), render_kw={"class": "form-control"}, default="checked"
    )
