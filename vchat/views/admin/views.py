from types import SimpleNamespace

import aiohttp_jinja2
import sqlalchemy as sa
from aiohttp_session import get_session

from vchat.settings import cfg
from vchat.models import AdminEvent, ApiClient, Source, User
from vchat.models.data import api_client_source
from vchat.utils import login_required, meta, paginator
from . import forms


async def _get_api_client_sources(db_session) -> list[SimpleNamespace]:
    rows = (
        await db_session.execute(
            sa.select(Source.id, Source.title, Source.uri).order_by(
                Source.title.asc(),
                Source.id.asc(),
            )
        )
    ).all()
    return [
        SimpleNamespace(id=source_id, title=title, uri=uri)
        for source_id, title, uri in rows
    ]


@meta(title="Журнал действий")
@login_required()
@aiohttp_jinja2.template("admin/event_list.html")
async def event_list(request):
    per_page = 50
    try:
        page = int(request.rel_url.query.get("page", "1"))
    except (TypeError, ValueError):
        page = 1

    total_items = (
        await request["db"].scalar(sa.select(sa.func.count(AdminEvent.id))) or 0
    )

    def _href_for_page(target_page: int) -> str:
        if target_page <= 1:
            return request.path
        return str(request.rel_url.with_query({"page": str(target_page)}))

    pagination = paginator(
        total_items,
        page=page,
        per_page=per_page,
        href_factory=_href_for_page,
    )
    page = pagination["page"]

    offset = (page - 1) * per_page
    events = (
        (
            await request["db"].execute(
                sa.select(AdminEvent)
                .order_by(AdminEvent.created_at.desc(), AdminEvent.id.desc())
                .limit(per_page)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return {
        "events": events,
        "page": page,
        "total_items": total_items,
        "pagination": pagination,
    }


@meta(title="Список пользователей")
@login_required()
@aiohttp_jinja2.template("admin/user_list.html")
async def user_list(request):
    session = await get_session(request)
    add_form = forms.UserAdd(meta={"csrf_context": session})
    users = (
        (await request["db"].execute(sa.select(User).order_by(User.id.desc())))
        .scalars()
        .all()
    )
    return {
        "users": users,
        "add_form": add_form,
        "total_users": len(users),
        "current_user_id": request["user"].id,
        "admin_user_create_enabled": cfg.admin_user_create_enabled,
    }


@meta(title="API-клиенты")
@login_required()
@aiohttp_jinja2.template("admin/api_client_list.html")
async def api_client_list(request):
    session = await get_session(request)
    add_form = forms.ApiClientAdd(meta={"csrf_context": session})
    clients = (
        (
            await request["db"].execute(
                sa.select(ApiClient).order_by(ApiClient.id.desc())
            )
        )
        .scalars()
        .all()
    )
    if clients:
        client_ids = [client.id for client in clients]
        source_rows = (
            await request["db"].execute(
                sa.select(
                    api_client_source.c.api_client_id,
                    Source.id,
                    Source.title,
                    Source.uri,
                )
                .join(Source, Source.id == api_client_source.c.source_id)
                .where(api_client_source.c.api_client_id.in_(client_ids))
                .order_by(Source.title.asc(), Source.id.asc())
            )
        ).all()
        sources_by_client: dict[int, list[SimpleNamespace]] = {}
        for client_id, source_id, title, uri in source_rows:
            sources_by_client.setdefault(client_id, []).append(
                SimpleNamespace(id=source_id, title=title, uri=uri)
            )

        for client in clients:
            client.sources = sources_by_client.get(client.id, [])
            client.masked_secret = "vchatsec-..."

    return {
        "clients": clients,
        "sources": await _get_api_client_sources(request["db"]),
        "add_form": add_form,
        "new_credentials": None,
        "selected_source_ids": set(),
    }
