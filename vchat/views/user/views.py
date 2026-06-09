import asyncio
import contextlib
import logging

from typing import Any

from aiohttp import web
from redis.exceptions import RedisError

from vchat.settings import REDIS_KEY
from vchat.utils import login_required

__all__ = [
    "notify_ws",
]

logger = logging.getLogger(__name__)


async def _forward_notifications(
    ws: web.WebSocketResponse, request: web.Request
) -> None:
    user_id = request["user"].id
    channel = f"user_{user_id}"
    key = f"flash_toast_{user_id}"
    redis = request.app[REDIS_KEY]
    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(channel)
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            data = message.get("data")
            if isinstance(data, bytes):
                data = data.decode("utf-8", errors="ignore")
            if not data:
                continue
            await ws.send_str(data)
            with contextlib.suppress(RedisError):
                await redis.lrem(key, 1, data)  # type: ignore
    except RedisError as exc:
        logger.warning("Notifications stream unavailable for %s: %s", channel, exc)
        if not getattr(ws, "closed", False):
            await ws.close()
    finally:
        with contextlib.suppress(RedisError):
            await pubsub.unsubscribe(channel)
        with contextlib.suppress(RedisError):
            await pubsub.close()


@login_required()
async def notify_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    redis = request.app[REDIS_KEY]
    key = f"flash_toast_{request['user'].id}"

    # Drain pending flash messages for redirects/page reloads.
    try:
        pending_messages: list[Any] = await redis.lrange(key, 0, -1)  # type: ignore
        await redis.delete(key)
    except RedisError as exc:
        logger.warning("Pending notifications unavailable for %s: %s", key, exc)
        pending_messages = []
    for payload in pending_messages:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", errors="ignore")
        if payload:
            await ws.send_str(payload)

    forward_task = asyncio.create_task(_forward_notifications(ws, request))
    try:
        async for message in ws:
            if message.type in (
                web.WSMsgType.CLOSE,
                web.WSMsgType.CLOSED,
                web.WSMsgType.ERROR,
            ):
                break
    finally:
        forward_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, RedisError):
            await forward_task
        await ws.close()

    return ws
