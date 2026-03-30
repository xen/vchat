import asyncio
import contextlib
import sqlalchemy as sa
from aiohttp import web
from aiohttp_session import get_session
import aiohttp_jinja2

from vchat.app_keys import REDIS_KEY
from vchat.i18n import _
from vchat.models import User
from vchat.utils import login_required, meta, flash

from . import forms

__all__ = [
    "settings",
    "notify_ws",
]


@meta(title=_("User settings"))
@login_required()
@aiohttp_jinja2.template("auth/settings.html")
async def settings(request):
    session = await get_session(request)
    data = await request.post()
    user: User = request["user"]
    form = forms.SettingsForm(data, data=user.asdict(), meta={"csrf_context": session})

    if request.method == "POST" and form.validate():
        await request["db"].execute(
            sa.update(User)
            .values(name=form.name.data)
            .where(User.id == user.id)
        )
        await request["db"].commit()
        await flash(request, _("Settings are saved"))
        return web.HTTPFound(request.app.router["settings"].url_for())

    return {"form": form}


async def _forward_notifications(ws: web.WebSocketResponse, request: web.Request) -> None:
    user = request["user"]
    channel = f"user_{user.id}"
    pubsub = request.app[REDIS_KEY].pubsub()
    await pubsub.subscribe(channel)
    try:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            data = message.get("data")
            if isinstance(data, bytes):
                data = data.decode("utf-8", errors="ignore")
            if not data:
                continue
            await ws.send_str(data)
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.close()


@login_required()
async def notify_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    user = request["user"]
    redis = request.app[REDIS_KEY]
    key = f"flash_toast_{user.id}"

    # Drain pending flash messages for redirects/page reloads.
    pending_messages = await redis.lrange(key, 0, -1)
    await redis.delete(key)
    for payload in pending_messages:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", errors="ignore")
        if payload:
            await ws.send_str(payload)

    forward_task = asyncio.create_task(_forward_notifications(ws, request))
    try:
        async for message in ws:
            if message.type in (web.WSMsgType.CLOSE, web.WSMsgType.CLOSED, web.WSMsgType.ERROR):
                break
    finally:
        forward_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await forward_task
        await ws.close()

    return ws
