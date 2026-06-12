import sqlalchemy as sa
from aiohttp import web

from vchat.settings import REDIS_KEY


async def live(request: web.Request) -> web.Response:
    del request
    return web.json_response({"status": "ok"})


async def ready(request: web.Request) -> web.Response:
    await request["db"].execute(sa.text("select 1"))
    await request.app[REDIS_KEY].ping()
    return web.json_response({"status": "ok"})
