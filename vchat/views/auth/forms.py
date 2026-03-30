from datetime import timedelta

from wtforms import (
    BooleanField,
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


class RegisterForm(Form):
    class Meta:
        csrf = True  # Enable CSRF
        csrf_secret = config["secret_key"]
        csrf_class = SessionCSRF
        csrf_time_limit = timedelta(minutes=20)

    name = StringField(
        _("Name"),
        [
            validators.Length(
                min=4, max=100, message=_("Length from 4 to 100 characters")
            )
        ],
        render_kw={"placeholder": _("Name")},
    )
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
                min=6, max=35, message=_("Length from 6 to 35 characters")
            ),
            validators.EqualTo("confirm", message=_("Passwords must match")),
        ],
        render_kw={"placeholder": _("Password")},
    )
    confirm = PasswordField(
        _("Confirm password"), render_kw={"placeholder": _("Password confirmation")}
    )
    accept_rules = BooleanField(
        _("Accept site terms and conditions"),
        [
            validators.InputRequired(
                message=_("You must accept the terms of use of the site")
            )
        ],
    )


class RecoverForm(Form):
    """Password recovery form"""

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


class UpdatePasswordForm(Form):
    """Password update form"""

    class Meta:
        csrf = True  # Enable CSRF
        csrf_secret = config["secret_key"]
        csrf_class = SessionCSRF
        csrf_time_limit = timedelta(minutes=20)

    password = PasswordField(
        _("New password"),
        [
            validators.Length(
                min=6, max=35, message=_("Length from 6 to 35 characters")
            ),
            validators.EqualTo("confirm", message=_("Passwords must match")),
        ],
        render_kw={"placeholder": _("New password")},
    )
    confirm = PasswordField(
        _("New password (repeat)"),
        render_kw={"placeholder": _("Confirm new password")},
    )


class ResetForm(Form):
    """Password reset form"""

    class Meta:
        csrf = True  # Enable CSRF
        csrf_secret = config["secret_key"]
        csrf_class = SessionCSRF
        csrf_time_limit = timedelta(minutes=20)

    password = PasswordField(
        _("New password"),
        [
            validators.Length(
                min=6, max=35, message=_("Length from 6 to 35 characters")
            ),
            validators.EqualTo("confirm", message=_("Passwords must match")),
        ],
        render_kw={"placeholder": _("New password")},
    )
    confirm = PasswordField(
        _("New password (repeat)"),
        render_kw={"placeholder": _("Confirm new password")},
    )


class PasswordForm(Form):
    class Meta:
        csrf = True  # Enable CSRF
        csrf_secret = config["secret_key"]
        csrf_class = SessionCSRF
        csrf_time_limit = timedelta(minutes=20)

    password_old = PasswordField(
        _("Current password"),
        [validators.Length(min=6, max=35, message=_("Length from 6 to 35 characters"))],
        render_kw={
            "placeholder": _("Current password"),
            "style": "width: 400px;",
        },
    )
    password = PasswordField(
        _("New password"),
        [
            validators.Length(
                min=6, max=35, message=_("Length from 6 to 35 characters")
            ),
            validators.EqualTo("confirm", message=_("Passwords must match")),
        ],
        render_kw={
            "placeholder": _("New password"),
            "style": "width: 400px;",
        },
    )
    confirm = PasswordField(
        _("New password (repeat)"),
        render_kw={
            "placeholder": _("Confirm new password"),
            "style": "width: 400px;",
        },
    )


class UpdateEmailForm(Form):
    class Meta:
        csrf = True  # Enable CSRF
        csrf_secret = config["secret_key"]
        csrf_class = SessionCSRF
        csrf_time_limit = timedelta(minutes=20)

    password = PasswordField(
        _("Current password"),
        [validators.Length(min=6, max=35, message=_("Length from 6 to 35 characters"))],
        render_kw={"placeholder": _("Current password")},
    )
    email = StringField(
        _("Your email"),
        [
            validators.Length(
                min=6,
                max=254,
                message=_("Length from 6 to 254 characters"),
            ),
            validators.Email(message=_("Enter a valid email")),
            validators.EqualTo(
                "confirm", message=_("Email address must match the confirmation")
            ),
        ],
        render_kw={"placeholder": _("name@company.com")},
    )

    confirm = StringField(
        _("Repeat email"),
        [
            validators.Length(
                min=6,
                max=254,
                message=_("Length from 6 to 254 characters"),
            ),
            validators.Email(message=_("Enter a valid email")),
        ],
        render_kw={"placeholder": _("Repeat email")},
    )
