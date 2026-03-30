from datetime import timedelta

from wtforms import (
    Form,
    PasswordField,
    StringField,
    validators,
)
from wtforms.csrf.session import SessionCSRF

from vchat.i18n import _
from vchat.settings import config

# Email length: https://stackoverflow.com/a/574698/85739


class LoginForm(Form):
    class Meta:
        csrf = True  # Enable CSRF
        csrf_secret = config["secret_key"]
        csrf_class = SessionCSRF
        csrf_time_limit = timedelta(minutes=20)

    email = StringField(
        _("Your email"),
        [
            validators.Length(
                min=6,
                max=254,
                message=_("Length from 6 to 254 characters"),
            ),
            validators.Email(message=_("Enter a valid email")),
            validators.DataRequired(message=_("Required field")),
        ],
        render_kw={"placeholder": _("name@company.com")},
    )
    password = PasswordField(
        _("Your password"),
        [
            validators.Length(
                min=4, max=35, message=_("Length from 4 to 35 characters")
            ),
            validators.DataRequired(message=_("Required field")),
        ],
        render_kw={"placeholder": _("Password")},
    )
    # remember = BooleanField(_("Remember me"))
