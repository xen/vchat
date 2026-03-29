from wtforms import StringField, validators, Form
from wtforms.csrf.session import SessionCSRF
from datetime import timedelta
from vchat.settings import config
from vchat.text import _


class BaseForm(Form):
    class Meta:
        csrf = True
        csrf_secret = config["secret_key"]
        csrf_class = SessionCSRF
        csrf_time_limit = timedelta(minutes=20)


class SettingsForm(BaseForm):
    name = StringField(_("Name"), [validators.Length(min=4, max=25)])
