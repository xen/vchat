from datetime import timedelta

from wtforms import Form, PasswordField, StringField, validators
from wtforms.csrf.session import SessionCSRF

from vchat.i18n import _
from vchat.settings import config


class BaseForm(Form):
    class Meta:
        csrf = True
        csrf_secret = config["secret_key"]
        csrf_class = SessionCSRF
        csrf_time_limit = timedelta(minutes=20)


class CreateUserForm(BaseForm):
    email = StringField(
        _("Email"),
        [
            validators.Length(
                min=6, max=254, message=_("Length from 6 to 254 characters")
            ),
            validators.Email(message=_("Enter a valid email")),
            validators.DataRequired(message=_("Required field")),
        ],
        render_kw={"placeholder": _("name@company.com")},
    )
    password = PasswordField(
        _("Password"),
        [
            validators.Length(
                min=6, max=35, message=_("Length from 6 to 35 characters")
            ),
            validators.DataRequired(message=_("Required field")),
        ],
        render_kw={"placeholder": _("Password")},
    )


class UserPasswordForm(BaseForm):
    password = PasswordField(
        _("New password"),
        [
            validators.Length(
                min=6, max=35, message=_("Length from 6 to 35 characters")
            ),
            validators.EqualTo("confirm", message=_("Passwords must match")),
            validators.DataRequired(message=_("Required field")),
        ],
        render_kw={"placeholder": _("New password")},
    )
    confirm = PasswordField(
        _("Confirm password"),
        render_kw={"placeholder": _("Confirm password")},
    )
