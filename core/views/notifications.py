import logging
import asyncio
import json

import sqlalchemy as sa
from aiohttp import web, WSMsgType

from core.app_keys import REDIS_KEY
from core.i18n import gettext
from core.models import ProjectUser, Notify, NotifyRead
from core.utils import login_required

logger = logging.getLogger(__name__)


@login_required()
async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    user = request["user"]
    logger.info("WS connection for user %s", user.id)

    # Subscribe to Redis channel
    redis = request.app[REDIS_KEY]
    channel_name = f"user_{user.id}"
    pubsub = redis.pubsub()
    await pubsub.subscribe(channel_name)

    # Get user project IDs
    project_ids_query = sa.select(ProjectUser.project_id).where(
        ProjectUser.user_id == user.id
    )
    result = await request["db"].execute(project_ids_query)
    project_ids = result.scalars().all()

    # 1. Fetch existing unread notifications
    # Cond: (user_id match OR project_id in user's projects)
    # AND NOT EXISTS in NotifyRead

    stmt = (
        sa.select(Notify)
        .outerjoin(
            NotifyRead,
            sa.and_(NotifyRead.notify_id == Notify.id, NotifyRead.user_id == user.id),
        )
        .where(
            sa.and_(
                sa.or_(
                    Notify.user_id == user.id,
                    Notify.project_id.in_(project_ids) if project_ids else sa.false(),
                ),
                NotifyRead.id.is_(None),  # Ensure no matching read record
            )
        )
        .order_by(Notify.created_at.desc())
    )

    result = await request["db"].execute(stmt)
    notifications = result.scalars().all()

    for note in notifications:
        translated_body = gettext(note.body).format(**note.params)

        msg = {
            "id": note.id,
            "body": translated_body,
            "task_name": note.task_name,
            "created_at": note.created_at.isoformat() if note.created_at else "",
            "project_id": note.project_id,
            "read": False,
        }
        await ws.send_json(msg)

    # 2. Fetch pending toast flashes (for redirect persistence)
    key = f"flash_toast_{user.id}"
    try:
        pending = await redis.lrange(key, 0, -1)
        if pending:
            await redis.delete(key)
            for p in pending:
                try:
                    p_data = json.loads(p)
                    await ws.send_json(p_data)
                except Exception:
                    pass
    except Exception as e:
        logger.error("Error fetching pending flashes: %s", e)

    # 3. Listen loop
    try:

        async def listen_redis():
            async for message in pubsub.listen():
                if message["type"] == "message":
                    data = message["data"]
                    try:
                        # Assuming data handles ID
                        try:
                            # Try processing as JSON first (for flash messages)
                            try:
                                msg_data = json.loads(data)
                                if (
                                    isinstance(msg_data, dict)
                                    and msg_data.get("type") == "flash"
                                ):
                                    await ws.send_json(msg_data)
                                    continue
                            except (ValueError, TypeError, AttributeError):
                                pass

                            notify_id = int(data)
                            # Check read status again to be safe
                            stmt = (
                                sa.select(Notify)
                                .outerjoin(
                                    NotifyRead,
                                    sa.and_(
                                        NotifyRead.notify_id == Notify.id,
                                        NotifyRead.user_id == user.id,
                                    ),
                                )
                                .where(Notify.id == notify_id)
                            )

                            res = await request["db"].execute(stmt)
                            note = res.scalar()

                            if note:
                                # Check if read (NotifyRead.id should be None checked if joined, but scalar returns Notify)
                                # Actually simpler to just check if `note` was returned, but query above returns Notify regardless of outerjoin match unless filtered.
                                # Let's filter in query.

                                stmt = (
                                    sa.select(Notify)
                                    .outerjoin(
                                        NotifyRead,
                                        sa.and_(
                                            NotifyRead.notify_id == Notify.id,
                                            NotifyRead.user_id == user.id,
                                        ),
                                    )
                                    .where(
                                        Notify.id == notify_id, NotifyRead.id.is_(None)
                                    )
                                )
                                res = await request["db"].execute(stmt)
                                note = res.scalar()

                                if note:
                                    is_for_user = note.user_id == user.id
                                    is_for_project = (
                                        (note.project_id in project_ids)
                                        if project_ids
                                        else False
                                    )

                                    if is_for_user or is_for_project:
                                        translated_body = gettext(note.body).format(
                                            **note.params
                                        )
                                        msg = {
                                            "id": note.id,
                                            "body": translated_body,
                                            "task_name": note.task_name,
                                            "created_at": note.created_at.isoformat(),
                                            "project_id": note.project_id,
                                            "read": False,
                                        }
                                        await ws.send_json(msg)
                        except ValueError:
                            pass
                    except Exception as e:
                        logger.error("Error processing redis message: %s", e)

        redis_task = asyncio.create_task(listen_redis())

        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                # Optionally handle "mark read"
                pass
            elif msg.type == WSMsgType.ERROR:
                logger.error("ws connection closed with exception %s", ws.exception())

        redis_task.cancel()
        try:
            await redis_task
        except asyncio.CancelledError:
            pass

    finally:
        await pubsub.unsubscribe(channel_name)
        logger.info("WS connection closed for user %s", user.id)

    return ws
