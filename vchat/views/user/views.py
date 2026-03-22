import sqlalchemy as sa
from aiohttp import web
from aiohttp_session import get_session
import aiohttp_jinja2

from vchat.i18n import lazy_gettext as _
from vchat.models import User
from vchat.settings import config
from vchat.utils import login_required, meta, flash

from . import forms

__all__ = [
    "settings",
]


@meta(title=_("User settings"))
@login_required()
@aiohttp_jinja2.template("auth/settings.html")
async def settings(request):
    session = await get_session(request)
    data = await request.post()
    user: User = request["user"]
    form = forms.SettingsForm(data, data=user.asdict(), meta={"csrf_context": session})

    # Needs to access config for languages
    # Assuming core.settings.config is available
    form.language.choices = [
        (name, code) for name, code in config["lang_supported"].items()
    ]

    if request.method == "POST" and form.validate():
        await request["db"].execute(
            sa.update(User)
            .values(name=form.name.data, language=form.language.data)
            .where(User.id == user.id)
        )
        await request["db"].commit()
        await flash(request, _("Settings are saved"))
        return web.HTTPFound(request.app.router["settings"].url_for())

    return {"form": form}
