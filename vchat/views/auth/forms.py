from datetime import timedelta
from typing import Any

from wtforms import Form, PasswordField, StringField, validators
from wtforms.csrf.session import SessionCSRF

from vchat.settings import cfg


class Login(Form):
    class Meta:
        csrf = True
        csrf_secret = cfg.csrf_secret
        csrf_class = SessionCSRF
        csrf_time_limit = timedelta(minutes=20)

    email = StringField(
        "Ваш email",
        [
            validators.Length(
                min=6,
                max=254,
                message="Длина от 6 до 254 символов",
            ),
            validators.Email(message="Введите корректный email"),
            validators.DataRequired(message="Обязательное поле"),
        ],
        render_kw={"placeholder": "name@company.com"},
    )
    password = PasswordField(
        "Ваш пароль",
        [
            validators.Length(
                max=128,
                message="Длина до 128 символов",
            ),
            validators.DataRequired(message="Обязательное поле"),
        ],
        render_kw={"placeholder": "Пароль"},
    )

    def add_email_error(self, message: str) -> None:
        email_field: Any = self.email
        email_field.errors = [*self.email.errors, message]


class PasswordChange(Form):
    class Meta:
        csrf = True
        csrf_secret = cfg.csrf_secret
        csrf_class = SessionCSRF
        csrf_time_limit = timedelta(minutes=20)

    current_password = PasswordField(
        "Текущий пароль",
        [
            validators.Length(
                max=128,
                message="Длина до 128 символов",
            ),
            validators.DataRequired(message="Обязательное поле"),
        ],
        render_kw={"placeholder": "Текущий пароль"},
    )
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
