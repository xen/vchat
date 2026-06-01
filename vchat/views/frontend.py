import aiohttp_jinja2
import sqlalchemy as sa
from aiohttp import web


async def healthcheck(request):
    await request["db"].execute(sa.text("select 1;"))
    raise web.HTTPFound(request.app.router["project_view"].url_for())


robots = """
User-agent: *
Disallow: /

Host: chat.vbudushee.ru
"""


async def robots_txt(request):
    return web.Response(text=robots, content_type="text/plain")


async def favicon(request):
    raise web.HTTPFound("/static/favicon.ico")


async def widget_js(request):
    widget_chat_path = str(request.app.router["public_widget_chat"].url_for())
    return aiohttp_jinja2.render_template(
        "js/widget.js", request, {"widget_chat_path": widget_chat_path}
    )


@aiohttp_jinja2.template("demo.html")
async def demo_page(request):
    return {}
