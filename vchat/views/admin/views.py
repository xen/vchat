from datetime import timedelta
from types import SimpleNamespace

import aiohttp_jinja2
import sqlalchemy as sa
from aiohttp_session import get_session
from wtforms import Form, PasswordField, StringField, validators
from wtforms.csrf.session import SessionCSRF

from vchat.app_keys import CONFIG_KEY
from vchat.i18n import _
from vchat.settings import config
from vchat.models import AdminEvent, ApiClient, Source, User
from vchat.models.data import api_client_source
from vchat.utils import login_required, meta, paginator
from vchat.views.api.views import decrypt_client_secret


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


class ApiClientForm(BaseForm):
    name = StringField(
        _("Name"),
        [
            validators.Length(
                min=1, max=128, message=_("Length from 1 to 128 characters")
            ),
            validators.DataRequired(message=_("Required field")),
        ],
        render_kw={"placeholder": _("Client name")},
    )


async def _get_users(db_session) -> list[User]:
    return (
        (await db_session.execute(sa.select(User).order_by(User.id.desc())))
        .scalars()
        .all()
    )


def _masked_api_client_secret(encrypted_secret: str, secret_key: str) -> str:
    secret = decrypt_client_secret(encrypted_secret, secret_key)
    return f"vchatsec-...{secret[-4:]}"


async def _get_api_clients(db_session, secret_key: str) -> list[ApiClient]:
    clients = (
        (await db_session.execute(sa.select(ApiClient).order_by(ApiClient.id.desc())))
        .scalars()
        .all()
    )
    if not clients:
        return []

    client_ids = [client.id for client in clients]
    source_rows = (
        await db_session.execute(
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
        client.masked_secret = _masked_api_client_secret(
            client.encrypted_secret,
            secret_key,
        )
    return clients


async def _get_api_client_sources(db_session) -> list[Source]:
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


@meta(title=_("Action Log"))
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


@meta(title=_("User List"))
@login_required()
@aiohttp_jinja2.template("admin/user_list.html")
async def user_list(request):
    session = await get_session(request)
    add_form = CreateUserForm(meta={"csrf_context": session})
    users = await _get_users(request["db"])
    return {
        "users": users,
        "add_form": add_form,
        "total_users": len(users),
        "current_user_id": request["user"].id,
    }


@meta(title=_("API Clients"))
@login_required()
@aiohttp_jinja2.template("admin/api_client_list.html")
async def api_client_list(request):
    session = await get_session(request)
    add_form = ApiClientForm(meta={"csrf_context": session})
    return {
        "clients": await _get_api_clients(
            request["db"],
            request.app[CONFIG_KEY]["secret_key"],
        ),
        "sources": await _get_api_client_sources(request["db"]),
        "add_form": add_form,
        "new_credentials": None,
        "selected_source_ids": set(),
    }
