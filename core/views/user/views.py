import sqlalchemy as sa
from aiohttp import web
from aiohttp_session import get_session
import aiohttp_jinja2

from core.i18n import lazy_gettext as _
from core.models import User, Notify, NotifyRead
from core.settings import config
from core.utils import login_required, meta, flash

from . import forms

__all__ = [
    "settings",
    "billing",
    "user_projects",
    "messages",
    "user_actions",
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


@meta(title=_("Billing"))
@login_required()
@aiohttp_jinja2.template("auth/billing.html")
async def billing(request):
    return {}


@meta(title=_("Projects"))
@login_required()
@aiohttp_jinja2.template("auth/projects.html")
async def user_projects(request):
    return {}


@meta(title=_("Notifications"))
@login_required()
@aiohttp_jinja2.template("auth/messages.html")
async def messages(request):
    db = request["db"]
    user = request["user"]

    # Query notifications with read status
    # Assuming Notify has fields (id, body, created_at, etc.)
    # Assuming NotifyRead links (notify_id, user_id)

    stmt = (
        sa.select(Notify, NotifyRead.id.isnot(None).label("is_read"))
        .outerjoin(
            NotifyRead,
            sa.and_(NotifyRead.notify_id == Notify.id, NotifyRead.user_id == user.id),
        )
        .where(Notify.user_id == user.id)
        .order_by(Notify.created_at.desc())
    )

    result = await db.execute(stmt)
    # result.all() returns list of (Notify, is_read) tuples
    notifications = []
    for row in result:
        notify = row[0]
        # Attach is_read attribute dynamically for template use if needed,
        # or pass as tuple. Let's pass objects with property attached is cleaner for template
        notify.is_read_status = row[1]
        notifications.append(notify)

    return {"notifications": notifications}


@login_required()
async def user_actions(request):
    action = request.match_info["action"]
    user = request["user"]
    db = request["db"]

    if action == "mark_all_read":
        # logic to mark all as read
        # 1. Find all unread notifications ids
        subquery = sa.select(Notify.id).where(
            Notify.user_id == user.id,
            ~sa.exists().where(
                sa.and_(
                    NotifyRead.notify_id == Notify.id, NotifyRead.user_id == user.id
                )
            ),
        )

        unread_ids = (await db.execute(subquery)).scalars().all()

        if unread_ids:
            # Bulk insert
            # Postgres supports bulk insert. SQLAlchemy Core:
            values = [{"notify_id": nid, "user_id": user.id} for nid in unread_ids]
            await db.execute(sa.insert(NotifyRead), values)
            await db.commit()
            await flash(request, _("All notifications marked as read"), "success")
        else:
            await flash(request, _("No unread notifications"), "info")

        # Handle HTMX request
        if request.headers.get("HX-Request"):
            response = web.Response(text="ok")
            response.headers["HX-Refresh"] = "true"
            return response

        return web.HTTPFound(request.app.router["messages"].url_for())

    return web.HTTPNotFound()
