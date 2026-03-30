import aiohttp_jinja2
import sqlalchemy as sa
from aiohttp_session import get_session

from vchat.i18n import _
from vchat.models import AdminEvent, User
from vchat.utils import login_required, meta

from . import forms


async def _get_users(db_session) -> list[User]:
    return (
        await db_session.execute(sa.select(User).order_by(User.id.desc()))
    ).scalars().all()


@meta(title=_("Action Log"))
@login_required()
@aiohttp_jinja2.template("admin/event_list.html")
async def event_list(request):
    per_page = 50
    try:
        page = int(request.rel_url.query.get("page", "1"))
    except (TypeError, ValueError):
        page = 1
    page = max(page, 1)

    total_items = await request["db"].scalar(sa.select(sa.func.count(AdminEvent.id))) or 0
    total_pages = max((total_items + per_page - 1) // per_page, 1)
    if page > total_pages:
        page = total_pages

    offset = (page - 1) * per_page
    events = (
        await request["db"].execute(
            sa.select(AdminEvent)
            .order_by(AdminEvent.created_at.desc(), AdminEvent.id.desc())
            .limit(per_page)
            .offset(offset)
        )
    ).scalars().all()
    return {
        "events": events,
        "page": page,
        "total_pages": total_pages,
        "total_items": total_items,
        "has_prev": page > 1,
        "has_next": page < total_pages,
    }


@meta(title=_("User List"))
@login_required()
@aiohttp_jinja2.template("admin/user_list.html")
async def user_list(request):
    session = await get_session(request)
    add_form = forms.CreateUserForm(meta={"csrf_context": session})
    users = await _get_users(request["db"])
    return {
        "users": users,
        "add_form": add_form,
        "total_users": len(users),
        "current_user_id": request["user"].id,
    }
