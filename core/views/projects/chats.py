import asyncio
import logging

import aiohttp_jinja2
import sqlalchemy as sa
from aiohttp import web
import redis.asyncio as aioredis

from core.db import async_session_factory
from core.models import Chat, Project, ProjectUser, ChatMsg
from core.settings import config
from core.utils import login_required, meta
from core.i18n import lazy_gettext as _

logger = logging.getLogger("core.projects.chats")
REDIS_URL = config.get("redis_uri", "redis://localhost:6379/3")
redis = aioredis.from_url(REDIS_URL, decode_responses=True)


@meta(title=_("Project Chats"))
@login_required()
@aiohttp_jinja2.template("projects/chats.html")
async def chats_list(request):
    project_id = request.match_info["project_id"]

    # Verify project access
    project = await request["db"].scalar(
        sa.select(Project).where(
            Project.short_id == project_id,
            ProjectUser.project_id == Project.id,
            ProjectUser.user_id == request["user"].id,
        )
    )
    if not project:
        raise web.HTTPNotFound()

    # Get active chats from Redis
    active_chat_ids = await redis.smembers(f"project:{project.id}:active_chats")
    active_chats = []

    if active_chat_ids:
        # Fetch chat details from DB
        stmt = sa.select(Chat).where(Chat.id.in_([int(cid) for cid in active_chat_ids]))
        async with async_session_factory() as db:
            result = await db.execute(stmt)
            chats = result.scalars().all()

            for chat in chats:
                active_chats.append(chat)

    return {
        "project": project,
        "active_chats": active_chats,
    }


async def forward_redis_to_ws(ws, chat_id):
    pubsub = redis.pubsub()
    await pubsub.subscribe(f"chat_monitor:{chat_id}")
    try:
        async for msg in pubsub.listen():
            if msg["type"] == "message":
                await ws.send_str(msg["data"])
    except Exception as e:
        logger.error(f"Redis listener error: {e}")
    finally:
        await pubsub.unsubscribe(f"chat_monitor:{chat_id}")


@login_required()
async def chat_monitor_ws(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    chat_short_id = request.match_info["chat_id"]

    # Resolve short_id to int id
    async with async_session_factory() as db:
        chat_id = await db.scalar(
            sa.select(Chat.id).where(Chat.short_id == chat_short_id)
        )

    if not chat_id:
        await ws.close()
        return ws

    task = asyncio.create_task(forward_redis_to_ws(ws, chat_id))
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.ERROR:
                break
            # We can handle "ping" from client if needed, but aiohttp handles pings automatically usually.

    finally:
        task.cancel()
        await ws.close()

    return ws


@meta(title=_("Project History"))
@login_required()
@aiohttp_jinja2.template("projects/history.html")
async def history_list(request):
    project_id = request.match_info["project_id"]

    # Verify project access
    project = await request["db"].scalar(
        sa.select(Project).where(
            Project.short_id == project_id,
            ProjectUser.project_id == Project.id,
            ProjectUser.user_id == request["user"].id,
        )
    )
    if not project:
        raise web.HTTPNotFound()

    try:
        page = int(request.query.get("page", "1"))
    except ValueError:
        page = 1
    if page < 1:
        page = 1

    per_page = 20

    total_stmt = (
        sa.select(sa.func.count())
        .select_from(Chat)
        .where(Chat.project_id == project.id)
    )
    total = await request["db"].scalar(total_stmt) or 0

    total_pages = (total + per_page - 1) // per_page if total else 0
    if total_pages and page > total_pages:
        page = total_pages
    if not total_pages:
        page = 1

    offset = (page - 1) * per_page if total else 0

    stmt = (
        sa.select(
            Chat,
            sa.func.count(sa.case((ChatMsg.vote == 1, 1))).label("upvotes"),
            sa.func.count(sa.case((ChatMsg.vote == -1, 1))).label("downvotes"),
            sa.func.count(ChatMsg.vote_comment).label("comments"),
        )
        .outerjoin(
            ChatMsg, sa.and_(Chat.id == ChatMsg.chat_id, ChatMsg.role == "assistant")
        )
        .where(Chat.project_id == project.id)
        .group_by(Chat.id)
        .order_by(Chat.created_at.desc())
        .offset(offset)
        .limit(per_page)
    )
    result = await request["db"].execute(stmt)
    rows = result.all()

    chats = []
    for row in rows:
        chat = row.Chat
        chat.upvotes = row.upvotes
        chat.downvotes = row.downvotes
        chat.comments = row.comments
        chats.append(chat)

    range_start = offset + 1 if chats else 0
    range_end = offset + len(chats) if chats else 0

    def _query_for_page(target_page: int):
        if target_page <= 1:
            return None
        return {"page": str(target_page)}

    has_prev = total_pages > 0 and page > 1
    has_next = total_pages > 0 and page < total_pages

    if total_pages <= 7 and total_pages > 0:
        page_numbers = list(range(1, total_pages + 1))
    elif total_pages > 0:
        page_numbers = [1]
        if page - 2 > 2:
            page_numbers.append(None)
        for number in range(max(2, page - 2), min(total_pages - 1, page + 2) + 1):
            page_numbers.append(number)
        if total_pages - (page + 2) > 1:
            page_numbers.append(None)
        if total_pages > 1:
            page_numbers.append(total_pages)
    else:
        page_numbers = []

    pagination_pages: list[dict] = []
    for number in page_numbers:
        if number is None:
            pagination_pages.append({"number": None})
            continue
        pagination_pages.append(
            {
                "number": number,
                "is_current": number == page,
                "query": _query_for_page(number),
            }
        )

    pagination = {
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "has_prev": has_prev,
        "has_next": has_next,
        "prev_query": _query_for_page(page - 1) if has_prev else None,
        "next_query": _query_for_page(page + 1) if has_next else None,
        "pages": pagination_pages,
        "range_start": range_start,
        "range_end": range_end,
    }

    return {
        "project": project,
        "chats": chats,
        "pagination": pagination,
    }


@meta(title=_("Chat History"))
@login_required()
@aiohttp_jinja2.template("projects/history_detail.html")
async def history_detail(request):
    project_id = request.match_info["project_id"]
    chat_id = request.match_info["chat_id"]

    # Verify project access
    project = await request["db"].scalar(
        sa.select(Project).where(
            Project.short_id == project_id,
            ProjectUser.project_id == Project.id,
            ProjectUser.user_id == request["user"].id,
        )
    )
    if not project:
        raise web.HTTPNotFound()

    chat = await request["db"].scalar(sa.select(Chat).where(Chat.short_id == chat_id))
    if not chat or chat.project_id != project.id:
        raise web.HTTPNotFound()

    stmt = (
        sa.select(ChatMsg)
        .where(ChatMsg.chat_id == chat.id)
        .order_by(ChatMsg.created_at.asc())
    )
    result = await request["db"].execute(stmt)
    messages = result.scalars().all()

    return {
        "project": project,
        "chat": chat,
        "messages": messages,
    }
