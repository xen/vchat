import logging
from datetime import datetime, timedelta

import aiohttp_jinja2
import sqlalchemy as sa
from aiohttp import web
from aiohttp_session import new_session
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from vchat.text import _
from vchat.models import Chat, ChatMsg, Chunk, Document, User
from vchat.settings import config
from vchat.utils import flash, login_required, meta

STEP = 50


def nav_links(request, name, **kwargs):
    offset = int(request.query.get("offset")) if request.query.get("offset") else 0
    prev_ = request.query.copy()
    prev_["offset"] = max(offset - STEP, 0)
    next_ = request.query.copy()
    next_["offset"] = offset + STEP
    kwargs.pop("query_", None)
    prev_link = request.app.router[name].url_for(**kwargs).with_query(**prev_)
    next_link = request.app.router[name].url_for(**kwargs).with_query(**next_)
    return offset, prev_link, next_link


@meta(title=_("Admin Dashboard"))
@login_required(allowed_roles=["admin"])
@aiohttp_jinja2.template("admin/dashboard.html")
async def dashboard(request):
    db = request["db"]

    totals_query = {
        "projects": sa.select(sa.literal(1)),
        "documents": sa.select(sa.func.count()).select_from(Document),
        "chunks": sa.select(sa.func.count()).select_from(Chunk),
        "pending_embeddings": sa.select(sa.func.count())
        .select_from(Chunk)
        .where(Chunk.embedding.is_(None)),
        "users": sa.select(sa.func.count()).select_from(User),
    }

    totals = {}
    for key, query in totals_query.items():
        totals[key] = await db.scalar(query) or 0

    start_date = datetime.utcnow() - timedelta(days=30)
    labels = [(start_date + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(31)]

    chats_per_day = {label: 0 for label in labels}
    likes_per_day = {label: 0 for label in labels}
    dislikes_per_day = {label: 0 for label in labels}
    new_projects_per_day = {label: 0 for label in labels}
    new_users_per_day = {label: 0 for label in labels}

    chats_query = (
        sa.select(
            sa.func.date_trunc("day", Chat.created_at).label("day"),
            sa.func.count(Chat.id).label("count"),
        )
        .where(Chat.created_at >= start_date)
        .group_by(sa.text("1"))
        .order_by(sa.text("1"))
    )

    votes_query = (
        sa.select(
            sa.func.date_trunc("day", ChatMsg.created_at).label("day"),
            sa.func.coalesce(
                sa.func.sum(sa.case((ChatMsg.vote == 1, 1), else_=0)), 0
            ).label("likes"),
            sa.func.coalesce(
                sa.func.sum(sa.case((ChatMsg.vote == -1, 1), else_=0)), 0
            ).label("dislikes"),
        )
        .where(ChatMsg.created_at >= start_date, ChatMsg.role == "assistant")
        .group_by(sa.text("1"))
        .order_by(sa.text("1"))
    )

    users_query = (
        sa.select(
            sa.func.date_trunc("day", User.created_at).label("day"),
            sa.func.count(User.id).label("count"),
        )
        .where(User.created_at >= start_date)
        .group_by(sa.text("1"))
        .order_by(sa.text("1"))
    )

    chats_rows = (await db.execute(chats_query)).all()
    votes_rows = (await db.execute(votes_query)).all()
    users_rows = (await db.execute(users_query)).all()

    for row in chats_rows:
        key = row.day.strftime("%Y-%m-%d")
        if key in chats_per_day:
            chats_per_day[key] = row.count

    for row in votes_rows:
        key = row.day.strftime("%Y-%m-%d")
        if key in likes_per_day:
            likes_per_day[key] = row.likes
            dislikes_per_day[key] = row.dislikes

    for row in users_rows:
        key = row.day.strftime("%Y-%m-%d")
        if key in new_users_per_day:
            new_users_per_day[key] = row.count

    return {
        "totals": totals,
        "labels": labels,
        "data_chats": [chats_per_day[label] for label in labels],
        "data_likes": [likes_per_day[label] for label in labels],
        "data_dislikes": [dislikes_per_day[label] for label in labels],
        "data_new_projects": [new_projects_per_day[label] for label in labels],
        "data_new_users": [new_users_per_day[label] for label in labels],
    }


@meta(title=_("User List"))
@login_required(allowed_roles=["admin"])
@aiohttp_jinja2.template("admin/user_list.html")
async def user_list(request):
    _get = request.query.get
    search = _get("search", "").lower()
    offset, prev_link, next_link = nav_links(request, "admin_users")

    query = sa.select(User.id, User.email, User.name, User.role, User.is_active)

    if search:
        term = f"%{search}%"
        query = query.where(sa.or_(User.email.ilike(term), User.name.ilike(term)))

    # order here
    query = query.order_by(User.id.desc())
    records = await request["db"].execute(query.limit(STEP).offset(offset))
    users = records.fetchall()

    return {"users": users, "prev_link": prev_link, "next_link": next_link}


@login_required(allowed_roles=["admin"])
async def login_as(request):
    code = request.query.get("user_id", None)
    s = URLSafeTimedSerializer(config["secret_key"])
    try:
        user_id = int(s.loads(code, max_age=300))
    except (ValueError, BadSignature, SignatureExpired) as e:
        logging.error("Error code %s: %s", code, e)
        await flash(request, _("Invalid code"), "error")
        return web.HTTPFound(location=request.app.router["admin_users"].url_for())
    except Exception as e:
        logging.error("Unexpected error with code %s: %s", code, e)
        await flash(request, _("Invalid code"), "error")
        return web.HTTPFound(location=request.app.router["admin_users"].url_for())

    record = await request["db"].execute(sa.select(User).where(User.id == user_id))
    user = record.scalar()

    session = await new_session(request)
    session["staff_id"] = user.id
    session["role"] = user.role.value

    return web.HTTPFound(location=request.app.router["admin_dashboard"].url_for())
