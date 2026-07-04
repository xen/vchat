from datetime import timedelta

from wtforms import Form, PasswordField, StringField, validators
from wtforms.csrf.session import SessionCSRF

from vchat.settings import cfg


class AdminCSRFBase(Form):
    class Meta:
        csrf = True
        csrf_secret = cfg.csrf_secret
        csrf_class = SessionCSRF
        csrf_time_limit = timedelta(minutes=20)


class UserAdd(AdminCSRFBase):
    email = StringField(
        "Email",
        [
            validators.Length(min=6, max=254, message="Длина от 6 до 254 символов"),
            validators.Email(message="Введите корректный email"),
            validators.DataRequired(message="Обязательное поле"),
        ],
        render_kw={"placeholder": "name@company.com"},
    )
    password = PasswordField(
        "Пароль",
        [
            validators.Length(
                min=12,
                max=128,
                message="Длина от 12 до 128 символов",
            ),
            validators.DataRequired(message="Обязательное поле"),
        ],
        render_kw={"placeholder": "Пароль"},
    )


class UserPasswordEdit(AdminCSRFBase):
    password = PasswordField(
        "Новый пароль",
        [
            validators.Length(
                min=12,
                max=128,
                message="Длина от 12 до 128 символов",
            ),
            validators.EqualTo("confirm", message="Пароли должны совпадать"),
            validators.DataRequired(message="Обязательное поле"),
        ],
        render_kw={"placeholder": "Новый пароль"},
    )
    confirm = PasswordField(
        "Подтвердите пароль",
        render_kw={"placeholder": "Подтвердите пароль"},
    )


class ApiClientAdd(AdminCSRFBase):
    name = StringField(
        "Имя",
        [
            validators.Length(
                min=1, max=128, message="Length from 1 to 128 characters"
            ),
            validators.DataRequired(message="Обязательное поле"),
        ],
        render_kw={"placeholder": "Client name"},
    )


class ApiClientEdit(AdminCSRFBase):
    name = StringField(
        "Имя",
        [
            validators.Length(
                min=1, max=128, message="Length from 1 to 128 characters"
            ),
            validators.DataRequired(message="Обязательное поле"),
        ],
        render_kw={"placeholder": "Client name"},
    )
